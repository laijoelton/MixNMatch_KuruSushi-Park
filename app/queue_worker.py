"""Background worker pools for webhook dispatch and maintenance actions."""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from datetime import datetime
from app.config import settings

log = logging.getLogger("dispatcher.queue")


class WebhookWorkerPool:
    """Bounded, in-memory ingress queue with concurrent keyed consumers.

    Event IDs are reserved before enqueueing, making duplicate detection an
    O(1) operation on the request path.  A failed event releases its ID so a
    simulator redelivery can be accepted.  Events for the same vehicle or
    component share a stripe lock; unrelated airport traffic is processed in
    parallel without allowing one vehicle's lifecycle to overtake itself.
    """

    def __init__(self, maxsize: int = 10_000, workers: int = 16, stripes: int = 256) -> None:
        self._maxsize = maxsize
        self._stripe_count = stripes
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._worker_count = workers
        self._tasks: list[asyncio.Task] = []
        self._handler: Optional[Callable[[dict[str, Any]], Awaitable[bool]]] = None
        self._accepted: set[str] = set()
        self._stripes = [asyncio.Lock() for _ in range(stripes)]
        self._stopping = False

    def start(self, handler: Callable[[dict[str, Any]], Awaitable[bool]],
              known_event_ids: tuple[str, ...] = ()) -> None:
        if self._tasks:
            return
        self.queue = asyncio.Queue(maxsize=self._maxsize)
        self._stripes = [asyncio.Lock() for _ in range(self._stripe_count)]
        self._handler = handler
        self._accepted.update(known_event_ids)
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._run(index), name=f"webhook-worker-{index}")
            for index in range(self._worker_count)
        ]
        log.info("webhook worker pool started (%d consumers, capacity=%d)",
                 self._worker_count, self.queue.maxsize)

    def enqueue(self, payload: dict[str, Any]) -> str:
        event_id = str(payload.get("EventId") or "")
        if event_id and event_id in self._accepted:
            return "duplicate"
        if self.queue.full():
            return "full"
        if event_id:
            self._accepted.add(event_id)
        self.queue.put_nowait(payload)
        return "enqueued"

    def release(self, event_id: Optional[str]) -> None:
        if event_id:
            self._accepted.discard(str(event_id))

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        return str(payload.get("CarPlateNumber") or payload.get("PlateNumber")
                   or payload.get("ComponentName") or payload.get("SpotName")
                   or payload.get("EventId") or "global")

    async def _run(self, index: int) -> None:
        while True:
            payload = await self.queue.get()
            try:
                handler = self._handler
                if handler is None:
                    self.release(payload.get("EventId"))
                    continue
                lock = self._stripes[hash(self._key(payload)) % len(self._stripes)]
                async with lock:
                    succeeded = await handler(payload)
                if not succeeded:
                    self.release(payload.get("EventId"))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.release(payload.get("EventId"))
                log.exception("webhook worker %d failed", index)
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stopping = True
        await self.queue.join()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._handler = None

    @property
    def stats(self) -> dict[str, int]:
        return {"pending": self.queue.qsize(), "workers": len(self._tasks),
                "capacity": self.queue.maxsize}


webhook_queue = WebhookWorkerPool()


@dataclass(order=True)
class _Task:
    priority: int
    sequence: int
    label: str = field(compare=False)
    action: Callable[[], Awaitable[None]] = field(compare=False)
    attempts: int = field(default=0, compare=False)
    on_drop: Optional[Callable[[], Awaitable[None]]] = field(default=None, compare=False)


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
        self._retries: set[asyncio.Task] = set()

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
        for retry in self._retries:
            retry.cancel()
        await asyncio.gather(*self._retries, return_exceptions=True)
        self._retries.clear()

    async def submit(self, label: str, action: Callable[[], Awaitable[None]], priority: int = 50,
                     on_drop: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        async with self._condition:
            heapq.heappush(self._heap, _Task(priority=priority, sequence=next(self._counter),
                                             label=label, action=action, on_drop=on_drop))
            self._condition.notify()

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def stats(self) -> dict[str, int]:
        return {"pending": len(self._heap) + len(self._retries), "completed": self._completed, "dropped": self._dropped}

    async def _retry_later(self, task: _Task) -> None:
        await asyncio.sleep(self._retry_delay_s / max(settings.game_speed, 0.1))
        async with self._condition:
            if not self._stopping:
                heapq.heappush(self._heap, task)
                self._condition.notify()

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
                    retry = asyncio.create_task(self._retry_later(task))
                    self._retries.add(retry)
                    retry.add_done_callback(self._retries.discard)
                else:
                    self._dropped += 1
                    log.error("maintenance task dropped after %d attempts: %s", task.attempts, task.label)
                    if task.on_drop:
                        try:
                            await task.on_drop()
                        except Exception:
                            log.exception("maintenance drop callback failed: %s", task.label)


maintenance_queue = PriorityTaskQueue()


def server_hour(raw: str) -> Optional[int]:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).hour
    except (TypeError, ValueError):
        return None


async def schedule_lights(raw: str, parking_state, simulator, act, lit_zones=None) -> None:
    """Day: every light off. Night: every light on - or, given ``lit_zones``,
    only the lights of those zones (4.29: zones with a car moving in them)."""
    hour = server_hour(raw)
    if hour is None:
        return
    from app import db
    night = not (settings.day_start_hour <= hour < settings.night_start_hour)
    for light in list(parking_state.lights.values()):
        if lit_zones is None:
            desired = night
        else:
            desired = night and (light.zone_parent in lit_zones if light.zone_parent else bool(lit_zones))
        if light.is_on == desired or light.broken or light.under_maintenance:
            continue
        action = simulator.light_on if desired else simulator.light_off
        if await act(f"light {light.name} {'ON' if desired else 'OFF'} (server hour {hour})",
                     lambda n=light.name, fn=action: fn(n)):
            cycles, runtime = parking_state.set_light_on(light.name, desired)
            db.sync_component_wear(light.name, "Light", cycles, runtime)
            db.record_component_event(light.name, "Light", "on" if desired else "off")
