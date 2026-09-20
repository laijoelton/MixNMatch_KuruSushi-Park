"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Load .env before any setting is read.
#
# Without this the file is decorative: os.environ never sees it, every value
# silently falls back to the defaults below, and edits appear to do nothing --
# including flipping AUTOPILOT to true. Real environment variables still win,
# so container and CI config override the file rather than fighting it.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:  # pragma: no cover - dotenv ships with uvicorn[standard]
    pass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _simulator_game_speed(default: float = 1.0) -> float:
    """Read GameSpeedMultiplier out of the simulator's own settings.json.

    Our waits (settling before charging, waiting for payment) are wall-clock,
    but the simulator's patience -- a driver gives up after five minutes -- is
    simulated time. Raise the multiplier and those five minutes elapse in a
    fraction of the wall-clock time, while our fixed 2s does not shrink, so we
    silently consume a much larger share of the budget and cars start escaping.

    Reading it here lets the timings scale automatically instead of needing a
    second place to remember. GAME_SPEED_MULTIPLIER overrides it.
    """
    override = os.environ.get("GAME_SPEED_MULTIPLIER")
    if override not in (None, ""):
        try:
            return max(0.1, float(override))
        except ValueError:
            pass

    candidates = [
        Path("ParkingSimulator-win-x64/ParkingSimulator-win-x64/settings/settings.json"),
        Path("settings/settings.json"),
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            import json

            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            value = float(raw.get("GameSpeedMultiplier", default))
            return max(0.1, value)
        except (OSError, ValueError, TypeError):
            continue
    return default


@dataclass(frozen=True)
class Settings:
    simulator_base_url: str
    simulator_email: str
    simulator_password: str
    webhook_secret: str
    webhook_signature_mode: str
    webhook_signature_recipe: str
    webhook_port: int
    request_timeout_s: float
    max_processed_events: int
    parking_rate_per_minute: float
    minimum_charge: float
    electric_multiplier: float
    electric_split_charging: bool
    autopilot: bool
    database_path: str
    entry_gate: str
    co_fan_on_threshold: float
    reservation_ttl_s: float
    en_route_grace_factor: float
    seed_from_level: str
    unknown_car_minutes: float
    game_speed: float
    billing_rounding: str
    billing_basis: str
    exit_charge_delay_s: float
    # Gates: every gate starts closed and zone gates open per car. The main
    # gate is left to the operator. Exit gates close this long after the car
    # leaves the exit sensor, since no sensor reports it clearing the gate.
    main_gate: str
    payment_retry_max: int
    tampered_payment_retry_max: int
    payment_retry_delay_s: float
    gate_close_delay_s: float
    # Send a car to its zone's entry sensor first and open that zone's gate
    # only when it is waiting there, instead of opening it at ENTRY1.
    zone_gate_at_sensor: bool
    # A zone entry gate closes this long after its car leaves the sensor box in
    # front of it: the sensor sits ~140 px before the gate, and cars were seen
    # covering 230-870 px/s, so ~0.6 s at worst plus margin.
    entry_gate_close_delay_s: float
    # The simulator console, tee'd to a file by START.bat; its "Load Game"
    # line is our only signal that a level was (re)loaded. Empty disables it.
    simulator_log: str
    # Dashboard: how often the operator HUD is pushed over the WebSocket.
    broadcast_interval_s: float
    webhook_debug: bool

    # --- Level 2: environmental control hysteresis ---
    # Below this the fan switches off; above co_fan_on_threshold it switches
    # on. Keeping the two apart stops the fan flapping on/off every reading
    # while the CO level hovers around a single threshold.
    co_fan_off_threshold: float

    # --- Level 2: multi-zone wear tracking ---
    wear_cycle_threshold: int
    # A bay is repaired preventively after this many parks since its last
    # repair (4.24). Bays were seen to break after 13-19 parks.
    spot_preventive_parks: int
    # Gates (4.42): there is no cap on how many gates may be in repair at once.
    # This is only the floor that stops a repair loop - a gate with fewer opens
    # than this since its last repair is never queued again, whatever the
    # predictor says (1 = skip only gates nothing has driven through).
    gate_repair_min_opens: int
    # A gate is repaired preventively once app.ml_agent.gate_failure_prediction
    # puts the chance of the next open breaking it at or above this (4.42).
    gate_failure_probability: float
    # Cold start and fallback for that predictor: the number of opens a gate is
    # expected to survive after a repair. 12 of 12 clean samples on 20 September
    # 2026 broke at 10 opens; samples spanning level reloads broke at 13-43.
    gate_expected_break_opens: int
    # The ML repair sweep (4.22) stays off: its model predicted ~99% failure for
    # every component and queued every bay and fan at once (4.31).
    ml_predictive_repairs: bool
    # A gate repair not finished after this many simulated seconds is treated as
    # stuck: it is dropped from its zone's maintenance so the zone can reopen,
    # and flagged for staff (4.35). Real repairs take 50-160 s.
    gate_repair_stuck_s: float
    # Night lights (4.29): a zone stays lit this many simulated seconds after
    # its last moving car. 0 = dark as soon as the last car parks (4.30).
    light_hold_s: float
    wear_runtime_threshold_s: float

    # --- Level 2: vehicle-class billing multipliers (on top of the existing
    # per-minute rate; kept distinct from electric_multiplier, which bills the
    # electricity line, not the parking line). ---
    class_multiplier_sedan: float
    class_multiplier_suv: float
    class_multiplier_ev: float

    # --- Level 2: reporting ---
    # Assumed wattage per light for the "energy conserved" estimate in
    # /api/reports/daily. Explicitly an estimate - there is no real meter.
    light_watts_estimate: float

    # --- Level 2: environmental control loop cadence ---
    environment_loop_interval_s: float
    # Hour-of-day (0-23, from ServerDateTime) at/after which lights go OFF for
    # day, and at/after which they go back ON for night.
    day_start_hour: int
    night_start_hour: int
    min_dwell_time_s: float
    orphan_timeout_s: float
    entry_max_attempts: int
    # Zone lifecycle (4.41): cascade to the highest-priority zone under this
    # ratio, spread load once every zone reaches it.
    zone_balance_ceiling: float
    # Cross-allocation is forbidden by default (4.42): an Electric/Accessible
    # car only ever gets its own category of bay, never falls back to "Any".
    # Standard cars never got an Electric or Accessible bay either way -
    # `available_spots("Any")` only returns Any-tagged bays.
    # Known trade-off, not a hidden one: 4.39 measured this exact rule turning
    # away every Electric/Accessible car on Level 3 when the reachable half of
    # the site had no bay of its type at all. Stage 3 (4.41) makes that a
    # clean, logged turn-away rather than silent chaos, but it is still a
    # turn-away. Set to false as an escape hatch if that shows up live.
    strict_category_filtering: bool
    dashboard_admin_password: str
    dashboard_operator_password: str
    dashboard_auditor_password: str
    dashboard_technician_password: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # The simulator's ListenAddress in settings.json is port 9898.
            simulator_base_url=os.environ.get(
                "SIMULATOR_BASE_URL", "http://127.0.0.1:9898"
            ).rstrip("/"),
            simulator_email=os.environ.get("SIMULATOR_EMAIL", "admin"),
            simulator_password=os.environ.get("SIMULATOR_PASSWORD", "admin"),

            # Fixed protocol: always enforce the specified MD5 recipe.
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            webhook_signature_mode="enforce",
            webhook_signature_recipe="md5:pipe:alpha",

            webhook_port=_env_int("WEBHOOK_PORT", 8080),
            request_timeout_s=_env_float("SIMULATOR_TIMEOUT_S", 10.0),
            max_processed_events=_env_int("MAX_PROCESSED_EVENTS", 5000),

            # Docs: "parking cost = total minutes spent parking", i.e. 1/minute,
            # "multiplied by 2 if car is electric".
            parking_rate_per_minute=_env_float("PARKING_RATE_PER_MINUTE", 1.0),
            minimum_charge=_env_float("MINIMUM_CHARGE", 0.0),
            electric_multiplier=_env_float("ELECTRIC_MULTIPLIER", 2.0),
            electric_split_charging=_env_bool("ELECTRIC_SPLIT_CHARGING", True),

            # False => log intended commands without sending them.
            autopilot=_env_bool("AUTOPILOT", False),

            database_path=os.environ.get("DATABASE_PATH", "data/park.db"),
            entry_gate=os.environ.get("ENTRY_GATE", "gateA"),
            co_fan_on_threshold=_env_float("CO_FAN_ON_THRESHOLD", 50.0),

            # How long a spot stays promised to a car that has not arrived.
            # Cars that are neglected at the entry drive off, and their
            # reservation must not hold the spot forever.
            reservation_ttl_s=_env_float("RESERVATION_TTL_S", 120.0),
            # A car that has left the entrance is on its way; on a Level 3 site
            # that drive is long. Its bay is held this many times longer.
            en_route_grace_factor=_env_float("EN_ROUTE_GRACE_FACTOR", 3.0),

            # Offline dev only: load the park from settings/<level>.json when
            # the simulator is not running. Empty disables it.
            seed_from_level=os.environ.get("SEED_FROM_LEVEL", "lvl1"),

            # A car that reaches the exit without us ever seeing it park (it
            # was already in the lot at startup, or survived a restart) has no
            # measurable duration. Charging 0 reads as "not charged" and earns
            # Penalty_CarShouldBeChargedAtExit, so bill a plausible estimate
            # instead: the midpoint of the simulator's MinParkingTime (1) and
            # MaxParkingTime (5) from settings.json.
            unknown_car_minutes=_env_float("UNKNOWN_CAR_MINUTES", 3.0),

            # Read from the simulator's settings.json so our wall-clock waits
            # stay proportional to the simulator's own sense of time.
            game_speed=_simulator_game_speed(),

            # How measured minutes become a billable figure: "round" (nearest,
            # the default), "ceil" (a started minute is charged), or "exact"
            # (send the raw fraction). The simulator draws planned durations as
            # whole minutes (MinParkingTime..MaxParkingTime), so our measured
            # 1.02 should bill as 1, not 2 - an off-by-one here means the car
            # refuses to pay and escapes. Tune with live evidence.
            billing_rounding=os.environ.get("BILLING_ROUNDING", "round").strip().lower(),

            # What the charge is actually based on.
            #
            # "planned"  - PlannedParkingDurationInMinutes, the duration the
            #              driver booked. Measured against 65 explicit
            #              corrections from the simulator, this is what it
            #              bills: planned 3 -> wants 3.00 (26 cases),
            #              planned 4 -> wants 4.00 (24 cases).
            # "measured" - our own wall-clock observation. Kept as a fallback
            #              and for cars whose planned duration we never saw.
            billing_basis=os.environ.get("BILLING_BASIS", "planned").strip().lower(),

            # The ExitSpot CarIn sensor fires when the car ENTERS the exit
            # area, not when it is settled and waiting for an invoice. Charging
            # on the event itself is rejected by the simulator with "Car is not
            # waiting at the exit" -- while still returning 201, so the failure
            # is invisible to us. Wait for the car to settle first.
            exit_charge_delay_s=_env_float("EXIT_CHARGE_DELAY_S", 2.0),
            main_gate=os.environ.get("MAIN_GATE", "gate7").strip(),
            gate_close_delay_s=_env_float("GATE_CLOSE_DELAY_S", 3.0),
            zone_gate_at_sensor=_env_bool("ZONE_GATE_AT_SENSOR", False),
            entry_gate_close_delay_s=_env_float("ENTRY_GATE_CLOSE_DELAY_S", 1.5),
            # 0 = never re-invoice automatically. Measured on the live binary:
            # charging a car that the simulator considers paid is
            # "Car has already paid for parking." at RM50 a time, and a wrong
            # amount still counts as paid. Staff re-request it per car instead
            # (POST /api/sessions/{plate}/request-payment). Set >0 only if a
            # build is confirmed not to fine it.
            payment_retry_max=int(_env_float("PAYMENT_RETRY_MAX", 0)),
            # A fake Level 3 receipt has an invalid signature and therefore did
            # not settle the simulator's invoice. It is safe (and required) to
            # ask again, unlike a correctly signed receipt with a wrong amount.
            tampered_payment_retry_max=int(_env_float("TAMPERED_PAYMENT_RETRY_MAX", 2)),
            payment_retry_delay_s=_env_float("PAYMENT_RETRY_DELAY_S", 2.0),
            simulator_log=os.environ.get("SIMULATOR_LOG", "data/simulator.log").strip(),

            broadcast_interval_s=_env_float("BROADCAST_INTERVAL_S", 1.0),
            webhook_debug=_env_bool("WEBHOOK_DEBUG", False),

            co_fan_off_threshold=_env_float("CO_FAN_OFF_THRESHOLD", 15.0),

            wear_cycle_threshold=_env_int("WEAR_CYCLE_THRESHOLD", 500),
            spot_preventive_parks=_env_int("SPOT_PREVENTIVE_PARKS", 9),
            gate_repair_min_opens=_env_int("GATE_REPAIR_MIN_OPENS", 1),
            gate_failure_probability=_env_float("GATE_FAILURE_PROBABILITY", 0.6),
            gate_expected_break_opens=_env_int("GATE_EXPECTED_BREAK_OPENS", 10),
            ml_predictive_repairs=_env_bool("ML_PREDICTIVE_REPAIRS", False),
            gate_repair_stuck_s=_env_float("GATE_REPAIR_STUCK_S", 400.0),
            light_hold_s=_env_float("LIGHT_HOLD_S", 0.0),
            wear_runtime_threshold_s=_env_float("WEAR_RUNTIME_THRESHOLD_S", 36000.0),

            class_multiplier_sedan=_env_float("CLASS_MULTIPLIER_SEDAN", 1.0),
            class_multiplier_suv=_env_float("CLASS_MULTIPLIER_SUV", 1.25),
            class_multiplier_ev=_env_float("CLASS_MULTIPLIER_EV", 1.1),

            light_watts_estimate=_env_float("LIGHT_WATTS_ESTIMATE", 60.0),

            environment_loop_interval_s=_env_float("ENVIRONMENT_LOOP_INTERVAL_S", 15.0),
            day_start_hour=_env_int("DAY_START_HOUR", 7),
            night_start_hour=_env_int("NIGHT_START_HOUR", 19),
            min_dwell_time_s=_env_float("MIN_DWELL_TIME_S", 5.0),
            orphan_timeout_s=_env_float("ORPHAN_TIMEOUT_S", 300.0),
            entry_max_attempts=_env_int("ENTRY_MAX_ATTEMPTS", 12),
            zone_balance_ceiling=_env_float("ZONE_BALANCE_CEILING", 0.40),
            strict_category_filtering=_env_bool("STRICT_CATEGORY_FILTERING", True),
            dashboard_admin_password=os.environ.get("DASHBOARD_ADMIN_PASSWORD", "admin123"),
            dashboard_operator_password=os.environ.get("DASHBOARD_OPERATOR_PASSWORD", "operator123"),
            dashboard_auditor_password=os.environ.get("DASHBOARD_AUDITOR_PASSWORD", "auditor123"),
            dashboard_technician_password=os.environ.get("DASHBOARD_TECHNICIAN_PASSWORD", "technician123"),
        )


settings = Settings.from_env()
