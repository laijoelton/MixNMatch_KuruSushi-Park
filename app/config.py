"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    simulator_base_url: str
    simulator_email: str
    simulator_password: str
    webhook_secret: str
    webhook_hash_algo: str
    webhook_port: int
    request_timeout_s: float
    max_processed_events: int
    parking_rate_per_minute: float
    minimum_charge: float
    broadcast_interval_s: float
    webhook_debug: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            simulator_base_url=os.environ.get("SIMULATOR_BASE_URL", "http://127.0.0.1:9898").rstrip("/"),
            simulator_email=os.environ.get("SIMULATOR_EMAIL", ""),
            simulator_password=os.environ.get("SIMULATOR_PASSWORD", ""),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            webhook_hash_algo=os.environ.get("WEBHOOK_HASH_ALGO", "sha256"),
            webhook_port=_env_int("WEBHOOK_PORT", 8080),
            request_timeout_s=_env_float("SIMULATOR_TIMEOUT_S", 10.0),
            max_processed_events=_env_int("MAX_PROCESSED_EVENTS", 5000),
            parking_rate_per_minute=_env_float("PARKING_RATE_PER_MINUTE", 0.20),
            minimum_charge=_env_float("MINIMUM_CHARGE", 1.00),
            broadcast_interval_s=_env_float("BROADCAST_INTERVAL_S", 1.0),
            webhook_debug=_env_bool("WEBHOOK_DEBUG", False),
        )


settings = Settings.from_env()
