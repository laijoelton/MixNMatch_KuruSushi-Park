"""Non-blocking background worker: a priority-ordered maintenance/repair queue.

Webhook handlers must return fast - the organizer's simulator does not wait
for slow HTTP round-trips before sending the next event, and a stalled
handler risks missing the next webhook's processing window. Anything that
is not on the critical dispatch path (repairs, exhaust-fan control) is
queued here and drained by a single background asyncio task, so a slow or
momentarily failing simulator call never blocks the webhook receiver and
never crashes the process.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from datetime import datetime
from app.config import settings

log = logging.getLogger("dispatcher.queue")


@dataclass(order=True)
class _Task:
    priority: int
    sequence: int
    label: str = field(compare=False)
    action: Callable[[], Awaitable[None]] = field(compare=False)
    attempts: int = field(default=0, compare=False)


class PriorityTaskQueue:
    """Bounded-retry priority work queue drained by a single background worker.

    Lower ``priority`` values run first (0 = most urgent). Equal-priority
    tasks run in submission order (a monotonic sequence number breaks ties,
    since ``heapq`` alone is not FIFO-stable across equal keys). A failing
    task is retried with linear backoff up to ``max_attempts`` times before
    being dropped with a logged error - a stuck retry must never stall the
    rest of the queue or the event loop.
    """

    def __init__(self, max_attempts: int = 5, retry_delay_s: float = 2.0) -> None:
        self._heap: list[_Task] = []
        self._counter = itertools.count()
        self._condition = asyncio.Condition()
        self._max_attempts = max_attempts
        self._retry_delay_s = retry_delay_s
        self._worker: Optional[asyncio.Task] = None
        self._stopping = False
        self._completed = 0
        self._dropped = 0

    def start(self) -> None:
        if self._worker is None:
            self._stopping = False
            self._worker = asyncio.create_task(self._run(), name="dispatcher-maintenance-queue")
            log.info("maintenance queue worker started")

    async def stop(self) -> None:
        self._stopping = True
        async with self._condition:
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def submit(self, label: str, action: Callable[[], Awaitable[None]], priority: int = 50) -> None:
        async with self._condition:
            heapq.heappush(self._heap, _Task(priority=priority, sequence=next(self._counter),
                                             label=label, action=action))
            self._condition.notify()

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def stats(self) -> dict[str, int]:
        return {"pending": len(self._heap), "completed": self._completed, "dropped": self._dropped}

    async def _run(self) -> None:
        while not self._stopping:
            async with self._condition:
                while not self._heap and not self._stopping:
                    await self._condition.wait()
                if self._stopping:
                    return
                task = heapq.heappop(self._heap)
            try:
                await task.action()
                self._completed += 1
                log.info("maintenance task completed: %s", task.label)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failing task must never kill the worker
                task.attempts += 1
                log.warning("maintenance task failed (%d/%d): %s - %s",
                           task.attempts, self._max_attempts, task.label, exc)
                if task.attempts < self._max_attempts:
                    await asyncio.sleep(self._retry_delay_s / max(settings.game_speed, 0.1))
                    async with self._condition:
                        heapq.heappush(self._heap, task)
                        self._condition.notify()
                else:
                    self._dropped += 1
                    log.error("maintenance task dropped after %d attempts: %s", task.attempts, task.label)


maintenance_queue = PriorityTaskQueue()


def server_hour(raw: str) -> Optional[int]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).hour
    except (TypeError, ValueError):
        return None


async def schedule_lights(raw: str, parking_state, simulator, act) -> None:
    hour = server_hour(raw)
    if hour is None:
        return
    from app import db
    desired = not (settings.day_start_hour <= hour < settings.night_start_hour)
    for light in list(parking_state.lights.values()):
        if light.is_on == desired:
            continue
        action = simulator.light_on if desired else simulator.light_off
        if await act(f"light {light.name} {'ON' if desired else 'OFF'} (server hour {hour})",
                     lambda n=light.name: action(n)):
            cycles, runtime = parking_state.set_light_on(light.name, desired)
            db.sync_component_wear(light.name, "Light", cycles, runtime)
            db.record_component_event(light.name, "Light", "on" if desired else "off")
