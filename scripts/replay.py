"""Drive a synthetic car lifecycle at the dispatcher, with no simulator running.

    python -m scripts.replay

Exercises the full path plus the guards that protect the score: duplicate
delivery, sequence gaps, penalties, CO events and a fake payment.

    AMOUNT=0.01 python -m scripts.replay     # watch fraud detection trip
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

TARGET = os.environ.get("TARGET", "http://127.0.0.1:8080/webhooks/simulator")
BASE = TARGET.rsplit("/webhooks/", 1)[0]
PLATE = os.environ.get("PLATE", "WCT 759")
AMOUNT = os.environ.get("AMOUNT")

# The read-only APIs are staff-only since the Admin/Operator auth landed. The
# webhook itself stays open -- the simulator has no cookie to send us.
STAFF_USER = os.environ.get("STAFF_USER", "operator")
STAFF_PASS = os.environ.get("STAFF_PASS", "operator123")

_cookie: str | None = None


def login() -> None:
    """Grab a staff session cookie so the invoice check below can read state."""
    global _cookie
    body = urllib.parse.urlencode({"username": STAFF_USER, "password": STAFF_PASS}).encode()
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(f"{BASE}/login", data=body, method="POST")
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {BASE}: {exc.reason} - is the dispatcher running?")
    raw = response.headers.get("set-cookie", "")
    _cookie = raw.split(";", 1)[0] if "kuru_session=" in raw else None
    print("staff login:", "ok" if _cookie else f"FAILED as {STAFF_USER} - invoice check will be skipped")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


def get_json(path: str):
    """Authenticated GET against the dispatcher, or None if it is not readable."""
    request = urllib.request.Request(f"{BASE}{path}")
    if _cookie:
        request.add_header("cookie", _cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None

_seq = int(os.environ.get("START_SEQ", "9000"))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def send(event: dict[str, Any], *, event_id: str | None = None) -> str:
    global _seq
    body = {
        **event,
        "EventId": event_id or str(uuid.uuid4()),
        "SequenceId": _seq,
        "ServerDateTime": _stamp(),
    }
    _seq += 1

    request = urllib.request.Request(
        TARGET,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        text, status = exc.read().decode(), exc.code
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {TARGET}: {exc.reason}\nis the dispatcher running?")

    print(f"{body['SequenceId']:<6} {body['EventClass']:<22} -> {status} {text}")
    return text


def car(spot: str, spot_type: str, direction: str, **extra: Any) -> dict[str, Any]:
    return {
        "EventClass": "car_spot_action",
        "CarPlateNumber": PLATE,
        "SpotName": spot,
        "SpotType": spot_type,
        "CarType": os.environ.get("CAR_TYPE", "Normal"),
        "Direction": direction,
        "PlannedParkingDurationInMinutes": "0",
        **extra,
    }


def main() -> None:
    print(f"replaying a full lifecycle for {PLATE} against {TARGET}\n")

    login()

    send({"EventClass": "test_webhook"})

    send(car("ENTRY1", "EntrySpot", "CarIn"))
    time.sleep(0.3)
    send(car("ENTRY1", "EntrySpot", "CarOut"))
    time.sleep(0.3)

    send(car("S3", "Park", "CarIn", PlannedParkingDurationInMinutes="2"))
    print("\n... parked, waiting 3s to accrue billable time ...\n")
    time.sleep(3)

    send(car("S3", "Park", "CarOut", PlannedParkingDurationInMinutes="2"))
    time.sleep(0.3)
    send(car("EXIT_EXIT", "ExitSpot", "CarIn"))

    # The dispatcher deliberately waits for the car to settle before charging
    # (the CarIn sensor fires too early for the simulator to accept a charge),
    # so poll until the invoice exists rather than guessing the delay.
    print()
    print("... waiting for the dispatcher to issue the invoice ...")
    expected = None
    for _ in range(20):
        time.sleep(0.5)
        snapshot = get_json("/api/state")
        for c in (snapshot or {}).get("sessions", []):
            if c["plate"] == PLATE and c.get("expected_amount") is not None:
                expected = c["expected_amount"]
                break
        if expected is not None:
            break
    if expected is None:
        print("  no invoice issued - the charge path did not run")
    else:
        print(f"  invoice issued: {expected}")

    amount = AMOUNT if AMOUNT is not None else (str(expected) if expected is not None else "1.0")
    send({
        "EventClass": "payment_made",
        "CarPlateNumber": PLATE,
        "Amount": amount,
        "Reason": "Car Payment",
    })
    time.sleep(0.3)
    send(car("EXIT_EXIT", "ExitSpot", "CarOut"))

    print("\n--- component + penalty events ---")
    send({"EventClass": "component_broken", "Type": "BarrierGate",
          "Name": "gateA", "FineAmount": "10.00"})
    send({"EventClass": "penalty",
          "Reason": "BarrierGate: Cannot Operate if it is Broken or under Maintenance.",
          "FineAmount": "10", "Type": "BarrierGate", "ComponentName": "gateA"})
    send({"EventClass": "carbon_monoxide_event", "ZoneName": "ZONE1",
          "CarbonMonoxideLevel": 63.564693, "DangerLevel": "Mid"})

    print("\n--- duplicate EventId (second must be rejected) ---")
    fixed = "fixed-id-for-dupe-test"
    send({"EventClass": "gate_action", "Name": "gateA", "Action": "Open"}, event_id=fixed)
    send({"EventClass": "gate_action", "Name": "gateA", "Action": "Open"}, event_id=fixed)

    print("\n--- sequence gap (must be recorded) ---")
    global _seq
    _seq += 25
    send({"EventClass": "gate_action", "Name": "gateA", "Action": "Closed"})

    print("\nnow check:  curl http://127.0.0.1:8080/healthz")


if __name__ == "__main__":
    main()
