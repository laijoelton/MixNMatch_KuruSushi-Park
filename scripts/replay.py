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
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

TARGET = os.environ.get("TARGET", "http://127.0.0.1:8080/webhooks/simulator")
PLATE = os.environ.get("PLATE", "WCT 759")
AMOUNT = os.environ.get("AMOUNT")

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
    time.sleep(0.3)

    # Default pays exactly what a ~0.05 minute stay costs at 1/minute.
    amount = AMOUNT if AMOUNT is not None else "0.05"
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
