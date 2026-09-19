"""Test setup: point the app at a throwaway database before anything imports it."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pg-tests-"))
os.environ["DATABASE_PATH"] = str(_TMP / "test.db")
os.environ["AUTOPILOT"] = "false"
os.environ["SEED_FROM_LEVEL"] = ""
os.environ["DASHBOARD_ADMIN_PASSWORD"] = "admin123"
os.environ["DASHBOARD_OPERATOR_PASSWORD"] = "operator123"
