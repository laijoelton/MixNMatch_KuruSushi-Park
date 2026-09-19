"""Async REST client for the Grand Park Auto simulator (``/api/v1``)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from app.config import settings

log = logging.getLogger("dispatcher.client")


class SimulatorClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=settings.simulator_base_url, timeout=settings.request_timeout_s)
        self._token: Optional[str] = None
        self._token_obtained_at: float = 0.0
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    async def login(self) -> str:
        async with self._login_lock:
            response = await self._http.post(
                "/api/v1/auth/login",
                json={"email": settings.simulator_email, "password": settings.simulator_password},
            )
            response.raise_for_status()
            token = response.json()["token"]
            self._token = token
            self._token_obtained_at = time.monotonic()
            log.info("simulator auth: token acquired")
            return token

    async def _headers(self) -> dict[str, str]:
        if self._token is None:
            await self.login()
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                       json_body: Optional[dict] = None, retry: bool = True) -> httpx.Response:
        headers = await self._headers()
        response = await self._http.request(method, path, headers=headers, params=params, json=json_body)
        if response.status_code == 401 and retry:
            log.warning("simulator auth: token rejected, re-authenticating")
            self._token = None
            return await self._request(method, path, params=params, json_body=json_body, retry=False)
        response.raise_for_status()
        return response

    # ------------------------------------------------------------------ #
    # Startup sync (called ONCE at boot / reconnect - never polled)
    # ------------------------------------------------------------------ #
    async def list_parking_spots(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/list-parking-spots")).json()

    async def list_barriers(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/list-barriers")).json()

    async def list_lights(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/list-lights")).json()

    async def list_exhaust_fans(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/list-exhaust-fans")).json()

    async def list_alarms(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/list-alarms")).json()

    async def list_zones(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/list-zones")).json()

    async def trigger_test_webhook(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/v1/test")
        return {"raw": response.text}

    # ------------------------------------------------------------------ #
    # Vehicle commands
    # ------------------------------------------------------------------ #
    async def car_goto(self, plate: str, destination: str) -> None:
        await self._request("POST", f"/api/v1/car/{plate}/goto/{destination}")

    async def car_charge(self, plate: str, parking_cost: float, charging_cost: float = 0.0) -> None:
        await self._request(
            "POST", f"/api/v1/car/{plate}/charge",
            params={"parkingCost": parking_cost, "chargingCost": charging_cost},
        )

    # ------------------------------------------------------------------ #
    # Barrier gates
    # ------------------------------------------------------------------ #
    async def barrier_open(self, name: str) -> None:
        await self._request("POST", f"/api/v1/barrier-gates/{name}/open")

    async def barrier_close(self, name: str) -> None:
        await self._request("POST", f"/api/v1/barrier-gates/{name}/close")

    async def barrier_repair(self, name: str) -> None:
        await self._request("POST", f"/api/v1/barrier-gates/{name}/repair")

    # ------------------------------------------------------------------ #
    # Lights
    # ------------------------------------------------------------------ #
    async def light_on(self, name: str) -> None:
        await self._request("POST", f"/api/v1/lights/{name}/on")

    async def light_off(self, name: str) -> None:
        await self._request("POST", f"/api/v1/lights/{name}/off")

    async def light_group_on(self, group: str) -> None:
        await self._request("POST", f"/api/v1/lights/group/{group}/on")

    async def light_group_off(self, group: str) -> None:
        await self._request("POST", f"/api/v1/lights/group/{group}/off")

    # ------------------------------------------------------------------ #
    # Exhaust fans
    # ------------------------------------------------------------------ #
    async def fan_on(self, name: str) -> None:
        await self._request("POST", f"/api/v1/exhaust-fans/{name}/on")

    async def fan_off(self, name: str) -> None:
        await self._request("POST", f"/api/v1/exhaust-fans/{name}/off")

    async def fan_repair(self, name: str) -> None:
        await self._request("POST", f"/api/v1/exhaust-fans/{name}/repair")

    # ------------------------------------------------------------------ #
    # Parking spots
    # ------------------------------------------------------------------ #
    async def spot_repair(self, name: str) -> None:
        await self._request("POST", f"/api/v1/parking-spots/{name}/repair")


client = SimulatorClient()
