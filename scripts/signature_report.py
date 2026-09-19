"""Report which webhook signature recipe the simulator actually uses.

    python -m scripts.signature_report

Run after the dispatcher has taken a few minutes of live traffic. A recipe at
100% over a few hundred events is the answer -- pin it in .env and switch to
enforce mode.
"""
from __future__ import annotations

import json

from app import db

attempts = db.signature_attempts()
rows = db.signature_trials()

print(f"\nEvents evaluated: {attempts}\n")

if attempts == 0:
    print("No events seen yet. Start the dispatcher and let the simulator run.")
    raise SystemExit(0)

if not rows:
    print("No candidate recipe matched any event.")
    print("\nThe signature likely covers a field or shared secret we cannot see.")
    print("Next steps:")
    print("  1. Check a raw payload for fields the docs omit (below).")
    print("  2. Ask the organisers for the algorithm and exact field set.")
    print("  3. Keep WEBHOOK_SIGNATURE_MODE=observe until resolved.")
    print("     Dropping real events costs far more than accepting unverified ones.")

    sample = db.query("SELECT payload FROM events WHERE signature IS NOT NULL LIMIT 1")
    if sample:
        payload = json.loads(sample[0]["payload"])
        print("\nSample payload keys:")
        print("  " + ", ".join(payload.keys()))
    raise SystemExit(0)

print("recipe (algo:separator:key-order)        matches  attempts    rate")
print("-" * 68)
for row in rows:
    print(
        f"{row['recipe']:<40}{row['matches']:>8}{row['attempts']:>10}{row['pct']:>7}%"
    )

winner = rows[0]
if winner["pct"] == 100:
    print("\nPin this in .env:")
    print(f"  WEBHOOK_SIGNATURE_RECIPE={winner['recipe']}")
    print("  WEBHOOK_SIGNATURE_MODE=enforce")
else:
    print(
        f"\nBest so far is {winner['recipe']} at {winner['pct']}% - "
        "collect more events before pinning."
    )
