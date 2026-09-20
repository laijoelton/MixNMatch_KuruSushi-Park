"""Level-load detection from the simulator's own console output.

The simulator sends no webhook when a level is loaded - the first thing we
would otherwise hear is the first car at the entrance. Its console does print
``Load Game./settings/lvl2.json`` the moment a level is clicked. START.bat
tees that console into a log file; this module follows the file.

Reading a local file makes no simulator API call, so it does not break the
docs' rule to use the list-* endpoints "ONLY once per level loading".

PowerShell 5.1's ``Tee-Object`` writes UTF-16LE with a BOM, so the encoding is
sniffed from the file's first bytes and decoded incrementally (a write can end
mid-character).
"""
from __future__ import annotations

import asyncio
import codecs
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger("dispatcher.simlog")

_LEVEL_LOAD = re.compile(r"Load Game\.?/settings/(lvl\d+)\.json", re.IGNORECASE)
_NO_PARK = re.compile(r"Car \((?P<plate>[^)]+)\) Wont park, going to any exit", re.IGNORECASE)
POLL_INTERVAL_S = 0.5   # local file I/O cadence, not simulated time


def level_loaded(line: str) -> Optional[str]:
    match = _LEVEL_LOAD.search(line)
    return match.group(1).lower() if match else None


def no_park_plate(line: str) -> Optional[str]:
    """Return the plate from the simulator's explicit no-parking decision."""
    match = _NO_PARK.search(line)
    return match.group("plate").strip() if match else None


class LogFollower:
    """Yields complete new lines appended to ``path``.

    Content already in the file when following starts is skipped (those loads
    happened before this dispatcher was running). A file that shrinks was
    rewritten by a relaunched simulator and is read again from the top.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._pos: Optional[int] = None
        self._skip_existing = True
        self._decoder = None
        self._pending = ""

    def _reset(self, head: bytes, size: int) -> None:
        if head[:2] == b"\xff\xfe":
            encoding, bom = "utf-16-le", 2
        elif head[:3] == b"\xef\xbb\xbf":
            encoding, bom = "utf-8", 3
        else:
            encoding, bom = "utf-8", 0
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self._pending = ""
        self._pos = size if self._skip_existing else bom
        self._skip_existing = False

    def read_new_lines(self) -> list[str]:
        try:
            with open(self.path, "rb") as f:
                head = f.read(3)
                size = f.seek(0, 2)
                if self._pos is None or size < self._pos:
                    self._reset(head, size)
                f.seek(self._pos)
                data = f.read()
        except FileNotFoundError:
            self._skip_existing = False   # created later: everything in it is new
            self._pos = None
            return []
        except OSError:
            return []
        self._pos += len(data)
        text = self._pending + self._decoder.decode(data)
        *lines, self._pending = text.split("\n")
        return [line.rstrip("\r") for line in lines]


async def follow(path: Path | str, on_level: Callable[[str], Awaitable[None]],
                 on_no_park: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
    follower = LogFollower(path)
    log.info("watching %s for simulator level loads", follower.path)
    while True:
        for line in follower.read_new_lines():
            level = level_loaded(line)
            if level:
                log.info("simulator loaded %s", level)
                try:
                    await on_level(level)
                except Exception:  # noqa: BLE001 - a failed reset must not stop the watcher
                    log.exception("level-load handling failed for %s", level)
            plate = no_park_plate(line)
            if plate and on_no_park:
                try:
                    await on_no_park(plate)
                except Exception:  # noqa: BLE001 - one bad line must not stop the watcher
                    log.exception("no-park handling failed for %s", plate)
        await asyncio.sleep(POLL_INTERVAL_S)
