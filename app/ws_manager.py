"""WebSocket fan-out for the live operator dashboard and gate picker.

A single background broadcaster (started in ``app/main.py``'s lifespan)
pushes one state snapshot per tick to every connected client. Connections
are tracked in a plain set guarded by an ``asyncio.Lock`` - a client that
disconnects mid-broadcast is dropped silently rather than raising into the
broadcaster loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("dispatcher.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        log.info("dashboard client connected (%d active)", self.count)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)
        log.info("dashboard client disconnected (%d active)", self.count)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._connections)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 - a broken socket must not affect other clients
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
