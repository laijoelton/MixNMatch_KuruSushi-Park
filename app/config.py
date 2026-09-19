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
    seed_from_level: str
    unknown_car_minutes: float
    billing_rounding: str
    exit_charge_delay_s: float
    payment_wait_s: float
    charge_max_attempts: int
    # Dashboard: how often the operator HUD is pushed over the WebSocket.
    broadcast_interval_s: float
    webhook_debug: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # The simulator's ListenAddress in settings.json is port 9898.
            simulator_base_url=os.environ.get(
                "SIMULATOR_BASE_URL", "http://127.0.0.1:9898"
            ).rstrip("/"),
            simulator_email=os.environ.get("SIMULATOR_EMAIL", "admin"),
            simulator_password=os.environ.get("SIMULATOR_PASSWORD", "admin"),

            # Signature handling. `observe` logs and calibrates but never
            # rejects; `enforce` requires a match. Start on observe -- the
            # documented recipe does not reproduce the docs' own samples.
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            webhook_signature_mode=os.environ.get("WEBHOOK_SIGNATURE_MODE", "observe"),
            webhook_signature_recipe=os.environ.get("WEBHOOK_SIGNATURE_RECIPE", ""),

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

            # How measured minutes become a billable figure: "round" (nearest,
            # the default), "ceil" (a started minute is charged), or "exact"
            # (send the raw fraction). The simulator draws planned durations as
            # whole minutes (MinParkingTime..MaxParkingTime), so our measured
            # 1.02 should bill as 1, not 2 - an off-by-one here means the car
            # refuses to pay and escapes. Tune with live evidence.
            billing_rounding=os.environ.get("BILLING_ROUNDING", "round").strip().lower(),

            # The ExitSpot CarIn sensor fires when the car ENTERS the exit
            # area, not when it is settled and waiting for an invoice. Charging
            # on the event itself is rejected by the simulator with "Car is not
            # waiting at the exit" -- while still returning 201, so the failure
            # is invisible to us. Wait for the car to settle first.
            exit_charge_delay_s=_env_float("EXIT_CHARGE_DELAY_S", 2.0),

            # How long to wait for payment_made before charging again. Drivers
            # give up and escape after 5 minutes, so there is room for retries.
            payment_wait_s=_env_float("PAYMENT_WAIT_S", 25.0),
            charge_max_attempts=_env_int("CHARGE_MAX_ATTEMPTS", 3),

            broadcast_interval_s=_env_float("BROADCAST_INTERVAL_S", 1.0),
            webhook_debug=_env_bool("WEBHOOK_DEBUG", False),
        )


settings = Settings.from_env()
