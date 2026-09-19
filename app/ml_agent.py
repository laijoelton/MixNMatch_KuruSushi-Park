"""Predictive layer on top of the deterministic Level 2 controls.

Every function here is additive: it either sharpens a decision the
dispatcher already makes deterministically (fan hysteresis, the 85%
duty-cycle repair trigger, the ghost-car fallback invoice) or runs a
background sweep that can act earlier than the existing hard trigger, never
instead of it. Training data comes exclusively from our own SQLite history
(app.db) - nothing here polls the simulator, so it does not violate the
"never poll" rule in ENGINEERING_LOG.md.

scikit-learn/numpy are optional at runtime: every public function has a
deterministic fallback (documented inline) so a cold start - or a box where
the ML deps failed to install - degrades to the existing Level 2 behaviour
instead of raising.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional, Sequence

from app import db, tariffs
from app.config import settings
from app.state import normalize_car_type

log = logging.getLogger("dispatcher.ml_agent")

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when the optional dep is absent
    _NUMPY_AVAILABLE = False

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import IsolationForest
    _SKLEARN_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when the optional dep is absent
    _SKLEARN_AVAILABLE = False

MIN_REPAIR_TRAINING_SAMPLES = 6
MIN_GHOST_CAR_SAMPLES_FOR_ISOLATION_FOREST = 8
REPAIR_FAILURE_PROBABILITY_THRESHOLD = 0.90

_repair_model: Optional["LogisticRegression"] = None


# --------------------------------------------------------------------- #
# 1.1 Proactive energy & CO ventilation
# --------------------------------------------------------------------- #

def _co_on_threshold(zone_ratio: float) -> float:
    """Dynamic ON edge: a busier zone reacts earlier than an empty one."""
    return max(30.0, 50.0 - 20.0 * zone_ratio)


def co_off_threshold(zone_ratio: float) -> float:
    """Dynamic OFF edge, kept below the ON edge so hysteresis still holds."""
    return max(15.0, 30.0 - 15.0 * zone_ratio)


def _forecast_co(history: Sequence[tuple[float, float]], horizon_s: float) -> float:
    """Project the CO trend `horizon_s` seconds past the last reading.

    `history` is a sequence of (monotonic_timestamp, co_level) pairs. With
    fewer than 3 points there is nothing to fit a trend to, so the last
    known reading is returned unchanged (flat forecast).
    """
    points = list(history)
    if len(points) < 3:
        return points[-1][1] if points else 0.0
    if _NUMPY_AVAILABLE:
        arr = np.array(points, dtype=float)
        x, y = arr[:, 0] - arr[0, 0], arr[:, 1]
        try:
            slope, intercept = np.polyfit(x, y, 1)
        except Exception:  # noqa: BLE001 - degenerate fit must not crash a fan decision
            return float(y[-1])
        return float(slope * (x[-1] + horizon_s) + intercept)
    # No numpy: rolling average rate of change over the trailing window.
    t0, v0 = points[0]
    t1, v1 = points[-1]
    rate = (v1 - v0) / max(t1 - t0, 1e-6)
    return v1 + rate * horizon_s


def co_ventilation_analysis(current_co: float, zone_ratio: float, historical_traffic: list) -> bool:
    """True => switch the zone's exhaust fans on now, ahead of the static edge.

    `zone_ratio` is the zone's current occupancy ratio R (0..1, see
    ``app.state.ParkingState.occupancy_ratio``); a fuller zone lowers the
    reactive edge (dynamic fallback below) and shortens how much forecast
    headroom is needed before acting. `historical_traffic` is the zone's
    trailing CO reading history as (monotonic_timestamp, co_level) pairs.
    """
    ratio = max(0.0, min(1.0, zone_ratio))
    if current_co >= _co_on_threshold(ratio):
        return True
    # 10 simulated minutes of look-ahead, scaled by game speed like every
    # other wall-clock wait in this project.
    horizon_s = 600.0 / max(settings.game_speed, 0.1)
    forecast = _forecast_co(historical_traffic, horizon_s)
    return forecast >= 50.0


# --------------------------------------------------------------------- #
# 1.2 System health & proactive maintenance
# --------------------------------------------------------------------- #

def _wear_ratio(cycle_count: int, runtime_seconds: int) -> float:
    return max(
        cycle_count / max(settings.wear_cycle_threshold, 1),
        runtime_seconds / max(settings.wear_runtime_threshold_s, 1.0),
    )


def _repair_training_rows() -> list[tuple[float, int]]:
    """(wear_ratio_at_outcome, failed) pairs from real `component_events` history.

    We don't retain a wear-ratio time series per component (component_wear
    only holds the live counters), so each historical outcome is anchored at
    the ratio it's known to have occurred at: 1.0 for an actual `broken`
    event, and the 0.85 trigger point for a `repair_triggered_proactive`
    that was then fixed before breaking. Small and coarse, but grounded in
    this park's own history rather than synthetic data.
    """
    rows = db.query(
        "SELECT event FROM component_events WHERE event IN ('broken', 'fixed_proactive') "
        "ORDER BY occurred_at"
    )
    samples: list[tuple[float, int]] = []
    for row in rows:
        if row["event"] == "broken":
            samples.append((1.0, 1))
        else:
            samples.append((0.85, 0))
    return samples


def _train_repair_model() -> Optional["LogisticRegression"]:
    if not (_SKLEARN_AVAILABLE and _NUMPY_AVAILABLE):
        return None
    samples = _repair_training_rows()
    if len(samples) < MIN_REPAIR_TRAINING_SAMPLES:
        return None
    labels = {label for _, label in samples}
    if len(labels) < 2:
        return None  # LogisticRegression needs both classes represented
    x = np.array([[ratio] for ratio, _ in samples])
    y = np.array([label for _, label in samples])
    model = LogisticRegression()
    try:
        model.fit(x, y)
    except Exception:  # noqa: BLE001 - a bad fit must fall back, not crash the sweep
        log.exception("repair model training failed")
        return None
    return model


def repair_period_prediction(component_id: str, cycle_count: int, runtime_seconds: int) -> float:
    """Failure probability in [0, 1] for the given component's current wear.

    With a trained model this is a real predicted probability. Cold start
    (not enough history yet, or scikit-learn/numpy unavailable) falls back
    to the deterministic rule already in `app.main.check_wear`: certain
    (1.0) at/above 85% of rated duty cycle, scaled linearly below it.
    """
    ratio = _wear_ratio(cycle_count, runtime_seconds)
    if _repair_model is not None:
        try:
            return float(_repair_model.predict_proba([[ratio]])[0][1])
        except Exception:  # noqa: BLE001 - a stale/bad model must not block maintenance
            log.exception("repair_period_prediction: model inference failed for %s", component_id)
    return 1.0 if ratio >= 0.85 else round(ratio / 0.85, 4)


# --------------------------------------------------------------------- #
# 1.3 Automated ghost-car fee estimation
# --------------------------------------------------------------------- #

def _median_dwell_minutes_for_type(car_type: str) -> Optional[float]:
    key = normalize_car_type(car_type)
    rows = db.query(
        "SELECT minutes FROM sessions WHERE car_type IS NOT NULL AND minutes IS NOT NULL "
        "AND lower(car_type) = ? ORDER BY minutes",
        (key,),
    )
    values = [float(r["minutes"]) for r in rows if r["minutes"] is not None]
    if not values:
        return None
    if _SKLEARN_AVAILABLE and _NUMPY_AVAILABLE and len(values) >= MIN_GHOST_CAR_SAMPLES_FOR_ISOLATION_FOREST:
        try:
            arr = np.array(values, dtype=float).reshape(-1, 1)
            inliers = IsolationForest(contamination=0.1, random_state=0).fit_predict(arr) == 1
            filtered = arr[inliers].flatten().tolist()
            if filtered:
                values = filtered
        except Exception:  # noqa: BLE001 - a bad fit must fall back to the raw median
            log.exception("ghost_car_anomaly_imputation: isolation forest failed for %s", car_type)
    values.sort()
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0


def ghost_car_anomaly_imputation(car_plate: str, car_type: str) -> float:
    """Impute a fallback parking fee for an unregistered vehicle at the exit.

    This ONLY estimates the figure shown on the operator's ghost-car review
    (app.main._handle_ghost_car) - it does not charge the simulator, mark a
    payment, or open the exit barrier. Auto-releasing an unauthenticated
    vehicle on a statistical guess would undo the control that flow was
    built for (ENGINEERING_LOG.md 4.7: "Only a validated payment releases
    the car"); that gate stays with staff. Logs the imputation to
    `audit_logs` for traceability, separate from any real charge/payment
    audit trail.
    """
    minutes = _median_dwell_minutes_for_type(car_type)
    if minutes is None:
        minutes = settings.unknown_car_minutes
    tariff = tariffs.effective()
    key = normalize_car_type(car_type)
    multiplier = float(tariff.get(f"class_multiplier_{key}", 1.0))
    base = round(max(tariff["minimum_charge"], minutes * tariff["parking_rate_per_minute"]) * multiplier, 2)
    fee = round(base * tariff["electric_multiplier"], 2) if key == "ev" else base
    try:
        db.record_audit_log(
            None, "ml_agent", "ghost_car_fee_imputed",
            json.dumps({"plate": car_plate, "car_type": car_type, "imputed_minutes": minutes, "fee": fee}),
        )
    except Exception:  # noqa: BLE001 - an audit-log failure must not block the estimate
        log.exception("ghost_car_anomaly_imputation: failed to log imputation for %s", car_plate)
    return fee


# --------------------------------------------------------------------- #
# Background training/sweep loop
# --------------------------------------------------------------------- #


async def predictive_sweep_once(
    wear_snapshot: Callable[[], list[dict[str, Any]]],
    queue_repair: Callable[[str, str], Awaitable[None]],
) -> None:
    """One retrain + sweep. Queues nothing unless ML_PREDICTIVE_REPAIRS is on:
    trained on 107 breakdowns against 1 preventive repair, the model gave
    ~99% failure for every component at any wear and queued them all (4.31)."""
    global _repair_model
    if not settings.ml_predictive_repairs:
        return
    _repair_model = _train_repair_model()
    for row in wear_snapshot():
        # Gates are scheduled one at a time by app.main._schedule_gate_repairs (4.28).
        if row["broken"] or row["under_maintenance"] or row["type"] in ("Light", "BarrierGate"):
            continue
        probability = repair_period_prediction(row["name"], row["cycle_count"], row["runtime_seconds"])
        if probability <= REPAIR_FAILURE_PROBABILITY_THRESHOLD:
            continue
        name, component_type = row["name"], row["type"]
        if db.get_meta(f"pending_proactive_repair:{name}") == "1":
            continue
        if settings.autopilot:
            db.set_meta(f"pending_proactive_repair:{name}", "1")
            db.record_component_event(name, component_type, "repair_triggered_predictive")
        log.info("ml_agent: %s %s predicted failure probability %.2f - queuing early repair",
                 component_type, name, probability)
        await queue_repair(component_type, name)

async def run_predictive_loop(
    *,
    wear_snapshot: Callable[[], list[dict[str, Any]]],
    queue_repair: Callable[[str, str], Awaitable[None]],
    interval_s: float = 60.0,
) -> None:
    """Retrain the repair model from local history, then sweep live wear.

    Reads only `app.db` (local SQLite) and the live `wear_snapshot` callback
    passed in from `app.main` - never the simulator - so this does not
    violate the "never poll" rule. Any component whose predicted failure
    probability crosses 90% is queued for repair via `queue_repair`
    (`app.main._queue_repair`, which already defers repair on an occupied
    spot and gates every simulator call through `act()`), strictly ahead of
    the existing hard 85%-duty-cycle trigger in `app.main.check_wear`, which
    keeps running unchanged as the reactive safety net.
    """
    while True:
        try:
            await predictive_sweep_once(wear_snapshot, queue_repair)
        except Exception:  # noqa: BLE001 - a bad sweep must not kill the background task
            log.exception("ml_agent predictive loop tick failed")
        await asyncio.sleep(interval_s / max(settings.game_speed, 0.1))
