"""CARMA Box — Charge Amps EV charger adapter.

Svensson (Sandkilsvägen 5): Charge Amps Halo. **IMPORTANT (904, 2026-09-01,
confirmed against Charge Amps' own official "External API version 4" PDF +
its published OpenAPI spec at https://eapi.charge.space/swagger/v4/swagger.json):
on a Halo, connectorId=1 is the actual EV charge point — connectorId=2 is a
plain Schuko (regular European wall socket), NOT a second EV charger.** Earlier
session notes assumed dual-EV load balancing across two connectors on this one
unit — that assumption was wrong. If both of Svensson's EVs need managed
charging, only connector 1 is a real EV connector; the second car either uses
connector 2 unmanaged (Schuko, no current control) or needs its own charger.

This is a direct cloud REST API (base https://eapi.charge.space, v4), NOT an
existing HA integration domain — unlike easee.py/zaptec.py, which piggyback on
an already-installed HA custom_component's services/entities, there is no such
component here. This adapter talks to Charge Amps' API directly over HTTP.

Auth: POST /api/v4/auth/login with body {email, password} and header
`apiKey: <key>` → {token, refreshToken}, token valid 120 min, refresh token
valid 168h (POST /api/v4/auth/refreshtoken). Bearer token in all subsequent
calls: `Authorization: Bearer <token>`.

STATUS: written directly against Charge Amps' own OpenAPI v4 spec (confirmed
endpoint paths, request/response field names) — NOT yet exercised against a
live account, since the API key is still pending (requested from Charge Amps
Support via Åke's Freshdesk ticket, 2026-08-31). Once the key arrives:
  1. Fill in email/password/api_key in the "Carma ha svensson - Charge Amps EV"
     1Password item (4recon vault).
  2. Call ensure_login() once and GET /api/v4/chargepoints/owned to confirm the
     real charge_point_id (guessed nowhere in this file — must come from a
     live call, never hardcoded).
  3. Verify the `mode: On/Off` settings write actually starts/stops charging
     given RFID is unlocked on this unit (per the app screenshot) — if it
     doesn't, fall back to the remotestart/remotestop endpoints, which require
     simulating an RFID tag (StartAuth: rfidLength, rfidFormat, rfid) since
     that's how Charge Amps models "who authorized this session", even for
     app/API-triggered starts.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from ..const import MAX_EV_CURRENT
from . import EVAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://eapi.charge.space"
_DYNAMIC_MIN = 6  # TODO verify against a live account: no confirmed minimum-current
# floor found in the API docs (unlike Easee's 10A max_limit quirk)
_TOKEN_TTL_S = 120 * 60
_TOKEN_REFRESH_MARGIN_S = 120  # refresh a bit before actual expiry


class ChargeAmpsAdapter(EVAdapter):
    """Adapter for one connector on a Charge Amps charge point (e.g. Halo).

    connector_id=1 is the real EV connector on a Halo. Do not default a
    second instance to connector_id=2 expecting a second EV charge point —
    that's the Schuko outlet (see module docstring).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        api_key: str,
        charge_point_id: str,
        connector_id: int = 1,
    ) -> None:
        self.hass = hass
        self.email = email
        self.password = password
        self.api_key = api_key
        self.charge_point_id = charge_point_id
        self.connector_id = connector_id
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0
        self._last_status: dict | None = None

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    async def ensure_login(self, force: bool = False) -> bool:
        """Login (or refresh) and cache the bearer token. Returns True on success."""
        now = time.monotonic()
        if not force and self._token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN_S:
            return True
        if self.analyze_only:
            _LOGGER.info("DRY-RUN ChargeAmps: login/refresh (skipped)")
            return True
        try:
            async with self._session() as sess:
                if self._refresh_token and not force:
                    async with sess.post(
                        f"{_BASE_URL}/api/v4/auth/refreshtoken",
                        json={"token": self._token, "refreshToken": self._refresh_token},
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            self._token = data["token"]
                            self._refresh_token = data["refreshToken"]
                            self._token_expires_at = now + _TOKEN_TTL_S
                            return True
                        _LOGGER.warning(
                            "ChargeAmps: refresh failed (%d), falling back to full login", r.status
                        )

                async with sess.post(
                    f"{_BASE_URL}/api/v4/auth/login",
                    headers={"apiKey": self.api_key},
                    json={"email": self.email, "password": self.password},
                ) as r:
                    if r.status != 200:
                        _LOGGER.error("ChargeAmps: login failed, status=%d", r.status)
                        return False
                    data = await r.json()
                    self._token = data["token"]
                    self._refresh_token = data["refreshToken"]
                    self._token_expires_at = now + _TOKEN_TTL_S
                    return True
        except aiohttp.ClientError as err:
            _LOGGER.error("ChargeAmps: login/refresh network error: %s", err)
            return False

    async def _get(self, path: str) -> dict | None:
        if not await self.ensure_login():
            return None
        try:
            async with (
                self._session() as sess,
                sess.get(
                    f"{_BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {self._token}"},
                ) as r,
            ):
                if r.status != 200:
                    _LOGGER.error("ChargeAmps: GET %s -> %d", path, r.status)
                    return None
                return await r.json()
        except aiohttp.ClientError as err:
            _LOGGER.error("ChargeAmps: GET %s network error: %s", path, err)
            return None

    async def _put(self, path: str, body: dict) -> bool:
        if self.analyze_only:
            _LOGGER.info("DRY-RUN ChargeAmps: PUT %s -> %s", path, body)
            return True
        if not await self.ensure_login():
            return False
        try:
            async with (
                self._session() as sess,
                sess.put(
                    f"{_BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json=body,
                ) as r,
            ):
                if r.status != 200:
                    _LOGGER.error("ChargeAmps: PUT %s -> %d", path, r.status)
                    return False
                return True
        except aiohttp.ClientError as err:
            _LOGGER.error("ChargeAmps: PUT %s network error: %s", path, err)
            return False

    async def refresh_status(self) -> None:
        """Poll GET /chargepoints/{id}/status and cache the result. Call periodically."""
        data = await self._get(f"/api/v4/chargepoints/{self.charge_point_id}/status")
        if data:
            self._last_status = data

    def _connector_status(self) -> dict:
        if not self._last_status:
            return {}
        for c in self._last_status.get("connectorStatuses", []):
            if c.get("connectorId") == self.connector_id:
                return c
        return {}

    # ── Read (from last refresh_status() cache — cheap, no adapter call blocks on network) ──

    @property
    def status(self) -> str:
        return self._connector_status().get("status", "")

    @property
    def current_a(self) -> float:
        """Average current across phases (A) from the last measurement, or 0."""
        measurements = self._connector_status().get("measurements", [])
        if not measurements:
            return 0.0
        currents = [m.get("current", 0.0) for m in measurements]
        return sum(currents) / len(currents) if currents else 0.0

    @property
    def phase_currents_a(self) -> dict[str, float]:
        """Per-phase current (A), e.g. {'L1': .., 'L2': ..} — from the API's own measurements."""
        return {
            m.get("phase", "?"): m.get("current", 0.0)
            for m in self._connector_status().get("measurements", [])
        }

    @property
    def power_w(self) -> float:
        return self._connector_status().get("chargingPowerKw", 0.0) * 1000

    @property
    def power_kw(self) -> float:
        return self.power_w / 1000

    @property
    def total_consumption_kwh(self) -> float:
        return self._connector_status().get("totalConsumptionKwh", 0.0)

    @property
    def is_charging(self) -> bool:
        # CAPI status enum (v4 doc): 0=Available,1=Charging,2=Connected,3=Error,4=Unknown
        return self.status.lower() == "charging"

    @property
    def plug_connected(self) -> bool:
        return self.status.lower() in ("connected", "charging")

    # ── Write ─────────────────────────────────────────────────

    async def _write_settings(
        self, *, max_current: float | None = None, mode: str | None = None
    ) -> bool:
        """PUT connector settings — covers both enable/disable (mode) and current limit."""
        body: dict = {
            "chargePointId": self.charge_point_id,
            "connectorId": self.connector_id,
        }
        if max_current is not None:
            body["maxCurrent"] = max_current
        if mode is not None:
            body["mode"] = mode  # "On" | "Off" | "Schedule"
        return await self._put(
            f"/api/v4/chargepoints/{self.charge_point_id}/connectors/{self.connector_id}/settings",
            body,
        )

    async def enable(self) -> bool:
        _LOGGER.info("ChargeAmps: enable connector %d (mode=On)", self.connector_id)
        return await self._write_settings(mode="On")

    async def disable(self) -> bool:
        _LOGGER.info("ChargeAmps: disable connector %d (mode=Off)", self.connector_id)
        return await self._write_settings(mode="Off")

    async def set_current(self, amps: int) -> bool:
        amps = max(_DYNAMIC_MIN, min(MAX_EV_CURRENT, amps))
        _LOGGER.info("ChargeAmps: set max_current connector %d -> %dA", self.connector_id, amps)
        return await self._write_settings(max_current=float(amps))
