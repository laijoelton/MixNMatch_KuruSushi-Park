"""Predictive layer on top of the deterministic Level 2 controls.

Every function here is additive: it either sharpens a decision the
dispatcher already makes deterministically (fan hysteresis, the 85%
duty-cycle repair trigger, the ghost-car fallback invoice) or runs a
background sweep that can act earlier than the existing hard trigger, never
instead of it. Training data comes exclusively from our own SQLite history
(app.db) - nothing here polls the simulator, so it does not violate the
"never poll" rule in ENGINEERING_LOG.md.

Each public function returns a small telemetry dict (not just a bool/float)
so the dashboard can show *why* - a countdown, a probability, a confidence
score - not just the resulting action. `latest_insights()` is a lightweight
cache of the most recently computed telemetry, read by app.main's broadcast
loop and merged into the live snapshot as `ml_insights`.

scikit-learn/numpy are optional at runtime: every public function has a
deterministic fallback (documented inline) so a cold start - or a box where
the ML deps failed to install - degrades to the existing Level 2 behaviour
instead of raising.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
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
CO_BREACH_PPM = 50.0
CO_HARD_ON_CEILING = 45.0  # guardrail vs Penalty_ZonePollutedWithHighCO: never wait past this to turn on
CO_WARNING_MINUTES = 10
NO_BREACH_PREDICTED_MINUTES = 9999
MAINTENANCE_WARNING_DAYS = 3
DAYS_TO_FAILURE_FALLBACK_WINDOW = 14  # heuristic span when no repair-history timestamp exists yet
CONFIDENCE_FULL_SAMPLE_SIZE = 20

_repair_model: Optional["LogisticRegression"] = None
_latest_ventilation: dict[str, dict[str, Any]] = {}
_latest_components: list[dict[str, Any]] = []
_warned_components: set[str] = set()


# --------------------------------------------------------------------- #
# 1.1 Proactive energy & CO ventilation
# --------------------------------------------------------------------- #

def _co_on_threshold(zone_ratio: float) -> float:
    """Dynamic ON edge: a busier zone reacts earlier than an empty one.

    Clamped to CO_HARD_ON_CEILING regardless of R - a hard guardrail so the
    fan is never left waiting past 45ppm, independent of how the dynamic
    formula reads at low occupancy.
    """
    return min(CO_HARD_ON_CEILING, max(30.0, CO_BREACH_PPM - 20.0 * zone_ratio))


def co_off_threshold(zone_ratio: float) -> float:
    """Dynamic OFF edge, kept below the ON edge so hysteresis still holds."""
    return max(15.0, 30.0 - 15.0 * zone_ratio)


def _co_trend(history: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """(rate_ppm_per_wall_second, last_known_reading).

    `history` is a sequence of (monotonic_timestamp, co_level) pairs. With
    fewer than 3 points there is nothing to fit a trend to, so the rate is
    flat (0.0).
    """
    points = list(history)
    if len(points) < 3:
        return 0.0, (points[-1][1] if points else 0.0)
    if _NUMPY_AVAILABLE:
        arr = np.array(points, dtype=float)
        x, y = arr[:, 0] - arr[0, 0], arr[:, 1]
        if x[-1] <= 1e-6:  # readings arrived within the same tick - nothing to fit a trend to
            return 0.0, float(y[-1])
        try:
            slope, _intercept = np.polyfit(x, y, 1)
        except Exception:  # noqa: BLE001 - a degenerate fit must not crash a fan decision
            return 0.0, float(y[-1])
        return float(slope), float(y[-1])
    t0, v0 = points[0]
    t1, v1 = points[-1]
    rate = (v1 - v0) / max(t1 - t0, 1e-6)
    return rate, v1


def co_ventilation_analysis(current_co: float, zone_ratio: float, historical_traffic: list) -> dict[str, Any]:
    """Ventilation telemetry for a zone, forecast-aware.

    Returns ``{"trigger_fan": bool, "minutes_to_threshold": int, "predicted_ppm": float}``.
    `zone_ratio` is the zone's current occupancy ratio R (0..1, see
    ``app.state.ParkingState.occupancy_ratio``) - a fuller zone lowers the
    reactive ON edge. `historical_traffic` is the zone's trailing CO reading
    history as (monotonic_timestamp, co_level) pairs.

    `minutes_to_threshold` is *simulated* minutes until `CO_BREACH_PPM` is
    reached at the current trend: 0 if already breached,
    `NO_BREACH_PREDICTED_MINUTES` if the trend isn't rising toward it.
    `predicted_ppm` is the forecast reading 10 simulated minutes out.
    """
    ratio = max(0.0, min(1.0, zone_ratio))
    on_threshold = _co_on_threshold(ratio)
    rate, _last = _co_trend(historical_traffic)
    # 10 simulated minutes of look-ahead, scaled by game speed like every
    # other wall-clock wait in this project.
    horizon_s = 600.0 / max(settings.game_speed, 0.1)
    predicted_ppm = round(current_co + rate * horizon_s, 2)

    if current_co >= CO_BREACH_PPM:
        minutes_to_threshold = 0
    elif rate <= 0:
        minutes_to_threshold = NO_BREACH_PREDICTED_MINUTES
    else:
        seconds_needed = (CO_BREACH_PPM - current_co) / rate
        minutes_to_threshold = max(0, round(seconds_needed * settings.game_speed / 60.0))

    trigger_fan = current_co >= on_threshold or minutes_to_threshold <= CO_WARNING_MINUTES
    return {
        "trigger_fan": trigger_fan,
        "minutes_to_threshold": int(minutes_to_threshold),
        "predicted_ppm": float(predicted_ppm),
    }


def record_ventilation_insight(zone_name: str, result: dict[str, Any]) -> None:
    """Cache the latest per-zone ventilation telemetry for `latest_insights()`."""
    _latest_ventilation[zone_name] = {"zone": zone_name, **result}


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


def _days_to_failure(component_id: str, ratio: float, probability: float) -> int:
    """Rough time-to-failure in days.

    If the component has a `last_repaired_at` timestamp, projects the
    observed wear-accumulation rate (ratio / elapsed days since repair)
    forward to `ratio == 1.0`. Otherwise falls back to a simple probability
    heuristic over a fixed window - there's no time-based history yet to
    project from (see the "known limitation" note on `repair_period_prediction`).
    """
    rows = db.query("SELECT last_repaired_at FROM component_wear WHERE name = ?", (component_id,))
    last_repaired_at = rows[0]["last_repaired_at"] if rows else None
    if last_repaired_at and ratio > 0:
        try:
            repaired = datetime.fromisoformat(last_repaired_at)
            if repaired.tzinfo is None:
                repaired = repaired.replace(tzinfo=timezone.utc)
            elapsed_days = max((datetime.now(timezone.utc) - repaired).total_seconds() / 86400.0, 1e-6)
            rate_per_day = ratio / elapsed_days
            if rate_per_day > 0:
                remaining = max(0.0, 1.0 - ratio)
                return max(0, round(remaining / rate_per_day))
        except (ValueError, TypeError):
            pass
    return max(0, round((1.0 - probability) * DAYS_TO_FAILURE_FALLBACK_WINDOW))


def repair_period_prediction(component_id: str, cycle_count: int, runtime_seconds: int) -> dict[str, Any]:
    """Failure telemetry for the given component's current wear.

    Returns ``{"needs_repair": bool, "days_to_failure": int, "failure_probability": float}``.
    With a trained model, `failure_probability` is a real predicted
    probability. Cold start (not enough history yet, or scikit-learn/numpy
    unavailable) falls back to the deterministic rule already in
    `app.main.check_wear`: certain (1.0) at/above 85% of rated duty cycle,
    scaled linearly below it. `needs_repair` mirrors the same 90% dispatch
    threshold `app.ml_agent.run_predictive_loop` acts on.

    **Known limitation** (see ENGINEERING_LOG.md 4.16): there's no persisted
    wear-ratio time series, so the trained model has only two anchor points
    to separate on and rarely clears 90% before the reactive 85% trigger in
    `check_wear` fires anyway - not a regression, just a ceiling on how much
    earlier this can currently catch a failure.
    """
    ratio = _wear_ratio(cycle_count, runtime_seconds)
    probability: Optional[float] = None
    if _repair_model is not None:
        try:
            probability = float(_repair_model.predict_proba([[ratio]])[0][1])
        except Exception:  # noqa: BLE001 - a stale/bad model must not block maintenance
            log.exception("repair_period_prediction: model inference failed for %s", component_id)
    if probability is None:
        probability = 1.0 if ratio >= 0.85 else round(ratio / 0.85, 4)
    days = _days_to_failure(component_id, ratio, probability)
    return {
        "needs_repair": probability > REPAIR_FAILURE_PROBABILITY_THRESHOLD,
        "days_to_failure": days,
        "failure_probability": round(probability, 4),
    }


def record_component_insights(rows: list[dict[str, Any]]) -> None:
    """Cache the latest component-health sweep for `latest_insights()`."""
    _latest_components[:] = rows


# --------------------------------------------------------------------- #
# 1.3 Automated ghost-car fee estimation
# --------------------------------------------------------------------- #

def _dwell_minutes_stats_for_type(car_type: str) -> tuple[Optional[float], int]:
    key = normalize_car_type(car_type)
    rows = db.query(
        "SELECT minutes FROM sessions WHERE car_type IS NOT NULL AND minutes IS NOT NULL "
        "AND lower(car_type) = ? ORDER BY minutes",
        (key,),
    )
    values = [float(r["minutes"]) for r in rows if r["minutes"] is not None]
    if not values:
        return None, 0
    sample_count = len(values)
    if _SKLEARN_AVAILABLE and _NUMPY_AVAILABLE and sample_count >= MIN_GHOST_CAR_SAMPLES_FOR_ISOLATION_FOREST:
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
    median = values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0
    return median, sample_count


def ghost_car_anomaly_imputation(car_plate: str, car_type: str) -> dict[str, Any]:
    """Impute a fallback parking fee for an unregistered vehicle at the exit.

    Returns ``{"imputed_fee": float, "confidence_score": float, "median_duration": int}``.
    `confidence_score` (0..1) is a plain function of how many same-car-type
    historical sessions the median is based on - not a statistical
    confidence interval, just enough to show staff "this is a guess" vs.
    "this is well-supported."

    This ONLY estimates the figure shown on the operator's ghost-car review
    (app.main._handle_ghost_car) - it does not charge the simulator, mark a
    payment, or open the exit barrier. Auto-releasing an unauthenticated
    vehicle on a statistical guess would undo the control that flow was
    built for (ENGINEERING_LOG.md 4.7: "Only a validated payment releases
    the car"); that gate stays with staff. Logs the imputation to
    `audit_logs` for traceability, separate from any real charge/payment
    audit trail.
    """
    minutes, sample_count = _dwell_minutes_stats_for_type(car_type)
    if minutes is None:
        minutes = settings.unknown_car_minutes
        confidence = 0.0
    else:
        confidence = round(min(1.0, sample_count / CONFIDENCE_FULL_SAMPLE_SIZE), 2)
    tariff = tariffs.effective()
    key = normalize_car_type(car_type)
    multiplier = float(tariff.get(f"class_multiplier_{key}", 1.0))
    base = round(max(tariff["minimum_charge"], minutes * tariff["parking_rate_per_minute"]) * multiplier, 2)
    fee = round(base * tariff["electric_multiplier"], 2) if key == "ev" else base
    result = {"imputed_fee": fee, "confidence_score": confidence, "median_duration": int(round(minutes))}
    try:
        db.record_audit_log(
            None, "ml_agent", "ghost_car_fee_imputed",
            json.dumps({"plate": car_plate, "car_type": car_type, **result}),
        )
    except Exception:  # noqa: BLE001 - an audit-log failure must not block the estimate
        log.exception("ghost_car_anomaly_imputation: failed to log imputation for %s", car_plate)
    return result


# --------------------------------------------------------------------- #
# Aggregated telemetry for the dashboard
# --------------------------------------------------------------------- #

def latest_insights() -> dict[str, Any]:
    """Snapshot of the most recently computed ML telemetry, for the
    dashboard's ML Insights sidebar (merged into the live snapshot as
    `ml_insights` by `app.main._broadcast_loop`).

    Ventilation/components are cached from their own event-driven/periodic
    triggers (cheap dict copy); anomalies are read fresh since the query is
    bounded and cheap. Role-based redaction (financial fields, maintenance
    detail) happens downstream in `app.policy.project_snapshot`, same as the
    rest of the snapshot.
    """
    return {
        "ventilation": list(_latest_ventilation.values()),
        "components": list(_latest_components),
        "anomalies": db.query(
            "SELECT plate, gate, fallback_charge, resolved_at FROM ghost_car_events "
            "WHERE resolved = 1 ORDER BY resolved_at DESC LIMIT 10"
        ),
    }


# --------------------------------------------------------------------- #
# Background training/sweep loop
# --------------------------------------------------------------------- #


async def predictive_sweep_once(
    wear_snapshot: Callable[[], list[dict[str, Any]]],
    queue_repair: Callable[[str, str], Awaitable[None]],
    broadcast: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
) -> None:
    """One retrain + sweep: records insights and early warnings (main, 4.22),
    but only queues repairs when ML_PREDICTIVE_REPAIRS is on. Trained on 107
    breakdowns against 1 preventive repair, the model gave ~99% failure for
    every component at any wear and queued them all (4.31). Gates are never
    queued here: app.main._schedule_gate_repairs owns them (4.28)."""
    global _repair_model
    _repair_model = _train_repair_model()
    component_rows: list[dict[str, Any]] = []
    for row in wear_snapshot():
        if row["broken"] or row["under_maintenance"] or row["type"] == "Light":
            _warned_components.discard(row["name"])
            continue
        name, component_type = row["name"], row["type"]
        prediction = repair_period_prediction(name, row["cycle_count"], row["runtime_seconds"])
        component_rows.append({"name": name, "type": component_type, **prediction})

        if prediction["days_to_failure"] <= MAINTENANCE_WARNING_DAYS:
            if broadcast is not None and name not in _warned_components:
                _warned_components.add(name)
                try:
                    await broadcast({
                        "type": "alert", "alert_type": "PREDICTIVE_MAINTENANCE_WARNING",
                        "component": name, "component_type": component_type,
                        "days_to_failure": prediction["days_to_failure"],
                        "failure_probability": prediction["failure_probability"],
                    })
                except Exception:  # noqa: BLE001 - a broadcast failure must not lose the sweep
                    log.exception("failed to broadcast maintenance warning for %s", name)
        else:
            _warned_components.discard(name)

        if (not settings.ml_predictive_repairs or component_type == "BarrierGate"
                or not prediction["needs_repair"]):
            continue
        if db.get_meta(f"pending_proactive_repair:{name}") == "1":
            continue
        if settings.autopilot:
            db.set_meta(f"pending_proactive_repair:{name}", "1")
            db.record_component_event(name, component_type, "repair_triggered_predictive")
        log.info("ml_agent: %s %s predicted failure probability %.2f - queuing early repair",
                 component_type, name, prediction["failure_probability"])
        await queue_repair(component_type, name)

    component_rows.sort(key=lambda c: c["days_to_failure"])
    record_component_insights(component_rows)


async def run_predictive_loop(
    *,
    wear_snapshot: Callable[[], list[dict[str, Any]]],
    queue_repair: Callable[[str, str], Awaitable[None]],
    broadcast: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
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

    If `broadcast` is given (`app.ws_manager.manager.broadcast`), also fires
    a `PREDICTIVE_MAINTENANCE_WARNING` alert the first time a component
    crosses `MAINTENANCE_WARNING_DAYS`, clearing the warned-state once it
    recovers so it can fire again on a future decline instead of going
    silent forever.
    """
    while True:
        try:
            await predictive_sweep_once(wear_snapshot, queue_repair, broadcast)
        except Exception:  # noqa: BLE001 - a bad sweep must not kill the background task
            log.exception("ml_agent predictive loop tick failed")
        await asyncio.sleep(interval_s / max(settings.game_speed, 0.1))
