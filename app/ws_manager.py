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

from app.auth import filter_snapshot

log = logging.getLogger("dispatcher.ws")


class ConnectionManager:
    def __init__(self) -> None:
        # Each socket is remembered with the permissions of the staff session
        # that opened it, so the fan-out can send an Accountant the penalty
        # figures without also sending them the bay-by-bay lot state.
        self._connections: dict[WebSocket, frozenset[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, permissions: frozenset[str] | None = None) -> None:
        await ws.accept()
        async with self._lock:
            self._connections[ws] = permissions or frozenset()
        log.info("dashboard client connected (%d active)", self.count)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.pop(ws, None)
        log.info("dashboard client disconnected (%d active)", self.count)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Push one tick to every client, filtered to what each may see.

        Sockets are grouped by permission set so the filter runs once per
        distinct role on screen, not once per connection.
        """
        async with self._lock:
            targets = list(self._connections.items())

        by_permissions: dict[frozenset[str], list[WebSocket]] = {}
        for ws, permissions in targets:
            by_permissions.setdefault(permissions, []).append(ws)

        dead: list[WebSocket] = []
        for permissions, sockets in by_permissions.items():
            view = filter_snapshot(payload, permissions)
            for ws in sockets:
                try:
                    await ws.send_json(view)
                except Exception:  # noqa: BLE001 - a broken socket must not affect other clients
                    dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.pop(ws, None)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
