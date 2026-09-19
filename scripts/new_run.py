"""Start a clean run: archive the database, then empty the run-scoped tables.

    python -m scripts.new_run           # called by START.bat before the dispatcher
    python -m scripts.new_run --keep 10 # how many archives to keep (default 5)

The database outlives the simulator, and that is deliberate: the dashboard
accounts, the tariff table, component wear and the component history the
maintenance predictor learns from all have to survive a restart. What must not
survive is the description of one run - events, sessions, payments, penalties,
neglected cars, sequence gaps. Left in place they come back as live alerts and
as revenue figures on the next launch: the panel opened on a fresh Level 2 with
100 neglected cars and RM15,350 of fines from the night before (log 4.x).

Nothing is deleted outright. The whole file is copied to ``data/archive/`` first,
so a run can still be read back afterwards with any SQLite client.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import db


def archive(keep: int) -> Path | None:
    """Copy the live database into data/archive/, pruning to the newest ``keep``."""
    source = Path(db._DB_PATH)
    if not source.exists() or not any(db.counters().values()):
        return None
    # WAL holds writes outside the .db file; without this the copy loses them.
    with db._lock:
        db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    folder = source.parent / "archive"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{source.stem}-{stamp}{source.suffix}"
    shutil.copy2(source, target)
    for old in sorted(folder.glob(f"{source.stem}-*{source.suffix}"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=5, help="archived databases to retain")
    args = parser.parse_args()

    try:
        saved = archive(args.keep)
        removed = db.start_new_run()
    except sqlite3.Error as exc:  # a locked or corrupt file must not stop the launcher
        print(f"        [i] could not reset the database ({exc}); continuing with it as it is")
        return 0

    total = sum(removed.values())
    if saved:
        try:
            shown = saved.relative_to(Path.cwd())
        except ValueError:           # DATABASE_PATH may point outside the project
            shown = saved
        print(f"        previous run archived to {shown}")
    print(f"        cleared {total} rows from the last run; users, tariffs and component "
          f"wear kept" if total else "        database was already clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
