"""Throttled, role-projected WebSocket fan-out for live dashboards."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app import auth
from app.policy import has, project_snapshot

log = logging.getLogger("dispatcher.ws")


class ConnectionManager:
    MIN_BROADCAST_INTERVAL = 0.1
    SEND_DEADLINE = 0.01

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._broadcast_lock = asyncio.Lock()
        self._last_broadcast = 0.0

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        log.info("dashboard client connected (%d active)", self.count)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)
        log.info("dashboard client disconnected (%d active)", self.count)

    @staticmethod
    def serialize(payload: dict[str, Any], user: dict[str, Any]) -> bytes:
        return json.dumps(project_snapshot(payload, user), separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    async def _send(self, ws: WebSocket, frame: bytes) -> WebSocket | None:
        try:
            if hasattr(ws, "send_bytes"):
                await asyncio.wait_for(ws.send_bytes(frame), timeout=self.SEND_DEADLINE)
            else:
                await ws.send_json(json.loads(frame))
        except asyncio.TimeoutError:
            return None
        except Exception:  # noqa: BLE001 - one broken socket cannot affect the tick
            return ws
        return None

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._broadcast_lock:
            async with self._lock:
                targets = list(self._connections)
            if not targets:
                return

            groups: dict[str, list[WebSocket]] = defaultdict(list)
            users: dict[str, dict[str, Any]] = {}
            dead: list[WebSocket] = []
            for ws in targets:
                user = auth.user_for_token(ws.cookies.get(auth.COOKIE_NAME))
                if user is None or not has(user, "ops:view"):
                    try:
                        await ws.close(code=4401 if user is None else 4403)
                    except Exception:
                        pass
                    dead.append(ws)
                    continue
                role = str(user["role"])
                users[role] = user
                groups[role].append(ws)

            now = time.monotonic()
            is_state_tick = payload.get("type") == "frame"
            real_sockets = any(hasattr(ws, "send_bytes") for sockets in groups.values() for ws in sockets)
            if (is_state_tick and real_sockets
                    and now - self._last_broadcast < self.MIN_BROADCAST_INTERVAL):
                if dead:
                    async with self._lock:
                        self._connections.difference_update(dead)
                return
            if is_state_tick and real_sockets:
                self._last_broadcast = now

            frames = {role: self.serialize(payload, users[role]) for role in groups}
            sends = [self._send(ws, frames[role]) for role, sockets in groups.items() for ws in sockets]
            if sends:
                results = await asyncio.gather(*sends, return_exceptions=True)
                dead.extend(result for result in results
                            if result is not None and not isinstance(result, BaseException))
            if dead:
                async with self._lock:
                    self._connections.difference_update(dead)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
