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

STATUS (904, 2026-09-03): login + GET /chargepoints/{id}/status verified
against the real account and real charger — both work exactly as coded.
charge_point_id="2505090652M" (name "HALO_090652M") confirmed live, connector
1=Charger(EV)/connector 2=Schuko confirmed via the API response itself (not
just the PDF). Credentials + charge_point_id live in the "Carma ha svensson -
Charge Amps EV" 1Password item (4recon vault); API key lives in the "Carma ha
svensson" note (chargeamps_api_key).

IMPORTANT — NOT YET RESOLVED: the live chargepoint object has
`"isLoadbalanced": true`. Svensson has a Charge Amps "Amp Guard" dynamic load
balancer whose own product page describes it as "connected to the EV charger
via Charge Amps Cloud" — meaning enable()/disable()/set_current() writes via
this adapter may interact with Amp Guard's own cloud-side balancing in ways
that aren't understood yet. Do NOT call the write methods (enable/disable/
set_current) against the live charger until this is confirmed safe with
Charge Amps — read-only status polling (refresh_status()) is fine.

STILL UNVERIFIED: whether `mode: On/Off` in the settings PUT actually starts/
stops charging (RFID is unlocked per the app screenshot, so it might), or
whether remotestart/remotestop (which require simulating an RFID tag) is the
real mechanism — not tested, since write methods are on hold pending the Amp
Guard question above.

CORRECTED (904, 2026-09-05): the EV-feed Shelly is a **Pro 3EM** (3-channel,
`phase_a/b/c_current`/`_voltage` entities), not a "Pro EM" with 2 numbered
channels — confirmed live via HA entity naming on 2026-09-04
(`sensor.shellypro3em_..._phase_a/b/c_effekt`). The earlier "Pro EM 2-channel"
assumption in this file was never corrected until now; `shelly_prefix` reads
the Pro 3EM's three phases (same entity pattern as `easee.py`'s
`shelly_3em_prefix`), summed, since Charge Amps sessions have been observed
using 2 of the 3 phases (L1+L2, L3=0A) and a single-phase reading would have
silently undercounted real load.

DEGRADED-MODE SIGNALING (904, 2026-09-05, QC-MANIFEST v4.1 C3.4): a stale or
never-successfully-refreshed status previously produced silent 0.0/{} reads
from `current_a`/`phase_currents_a`/`power_w` — indistinguishable from a
genuinely idle charger. This is unsafe for any consumer using these values as
a phase-current guard against a main fuse: a fabricated zero can let an
overcurrent condition through undetected. `data_available` and
`data_age_s` are now the explicit, testable signal a caller MUST check before
treating a zero reading as real; see their docstrings.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, TypedDict

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
_HTTP_TIMEOUT_S = 15  # QC-MANIFEST C3.2: every external call needs an explicit deadline


class ConnectorMeasurement(TypedDict):
    """One phase measurement entry from a connector status response."""

    phase: str
    current: float
    voltage: float


class ConnectorStatus(TypedDict, total=False):
    """Shape of one entry in `connectorStatuses` from GET .../status.

    `total=False`: the API does not guarantee every field is present on every
    connector state (e.g. `measurements` is null while `Available`) — treat
    absence as absence, not as a defaulted value, at the boundary (C6.3).
    """

    connectorId: int
    status: str
    measurements: list[ConnectorMeasurement]
    chargingPowerKw: float
    totalConsumptionKwh: float


class ChargePointStatusResponse(TypedDict, total=False):
    """Shape of the GET /chargepoints/{id}/status response body."""

    id: str
    status: str
    connectorStatuses: list[ConnectorStatus]


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
        shelly_prefix: str = "",
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
        self._last_status: ChargePointStatusResponse | None = None
        self._last_refresh_ok_at: float | None = None
        # Shelly Pro 3EM on the EV feed (904, 2026-09-05, see module docstring
        # for the Pro EM -> Pro 3EM correction).
        self.shelly_prefix = shelly_prefix

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S),
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
        except TimeoutError:
            _LOGGER.error("ChargeAmps: login/refresh timed out after %ds", _HTTP_TIMEOUT_S)
            return False
        except aiohttp.ClientError as err:
            _LOGGER.error("ChargeAmps: login/refresh network error: %s", err)
            return False

    async def _get(self, path: str) -> dict[str, Any] | None:
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
                result: dict[str, Any] = await r.json()
                return result
        except TimeoutError:
            _LOGGER.error("ChargeAmps: GET %s timed out after %ds", path, _HTTP_TIMEOUT_S)
            return None
        except aiohttp.ClientError as err:
            _LOGGER.error("ChargeAmps: GET %s network error: %s", path, err)
            return None

    async def _put(self, path: str, body: dict[str, Any]) -> bool:
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
        except TimeoutError:
            _LOGGER.error("ChargeAmps: PUT %s timed out after %ds", path, _HTTP_TIMEOUT_S)
            return False
        except aiohttp.ClientError as err:
            _LOGGER.error("ChargeAmps: PUT %s network error: %s", path, err)
            return False

    async def refresh_status(self) -> None:
        """Poll GET /chargepoints/{id}/status and cache the result. Call periodically.

        On failure, the previous cache is deliberately left in place (a
        transient poll failure should not erase the last known reading), but
        `_last_refresh_ok_at` is NOT updated — `data_available`/`data_age_s`
        make that distinction observable to callers instead of silently
        aging in place forever.
        """
        data = await self._get(f"/api/v4/chargepoints/{self.charge_point_id}/status")
        if data:
            self._last_status = ChargePointStatusResponse(
                id=data.get("id", ""),
                status=data.get("status", ""),
                connectorStatuses=data.get("connectorStatuses", []),
            )
            self._last_refresh_ok_at = time.monotonic()

    def _connector_status(self) -> ConnectorStatus:
        if not self._last_status:
            return {}
        for c in self._last_status.get("connectorStatuses", []):
            if c.get("connectorId") == self.connector_id:
                return c
        return {}

    # ── Degraded-mode signaling (QC-MANIFEST v4.1 C3.4) ─────────
    # A zero from current_a/phase_currents_a/power_w below is indistinguishable
    # from a genuinely idle connector. Any safety-critical consumer (a phase
    # fuse guard) MUST check data_available (and typically data_age_s) before
    # treating that zero as real telemetry rather than "we don't know".

    @property
    def data_available(self) -> bool:
        """True only if refresh_status() has ever completed successfully."""
        return self._last_refresh_ok_at is not None

    @property
    def data_age_s(self) -> float | None:
        """Seconds since the last successful refresh, or None if never refreshed."""
        if self._last_refresh_ok_at is None:
            return None
        return time.monotonic() - self._last_refresh_ok_at

    # ── Read (from last refresh_status() cache — cheap, no adapter call blocks on network) ──

    @property
    def status(self) -> str:
        return self._connector_status().get("status", "")

    @property
    def current_a(self) -> float:
        """Average current across phases (A) from the last measurement, or 0.

        0.0 means "no current in the last measurement OR no measurement at
        all" — check `data_available` first in any safety-critical caller.
        """
        measurements = self._connector_status().get("measurements") or []
        if not measurements:
            return 0.0
        currents = [m.get("current", 0.0) for m in measurements]
        return sum(currents) / len(currents) if currents else 0.0

    @property
    def phase_currents_a(self) -> dict[str, float]:
        """Per-phase current (A), e.g. {'L1': .., 'L2': ..} — from the API's own measurements.

        Empty dict means "no measurement available" (never refreshed, connector
        not found in the last response, or the API returned `measurements: null`
        for this connector's status, e.g. while `Available`) — NOT "0A on every
        phase". Check `data_available` before treating an empty/zero result as
        a real reading.
        """
        return {
            m.get("phase", "?"): m.get("current", 0.0)
            for m in (self._connector_status().get("measurements") or [])
        }

    def _state_by_id(self, entity_id: str, default: float = 0.0) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    @property
    def shelly_power_w(self) -> float:
        """EV power from the Shelly Pro 3EM on the EV feed (fast/local vs Charge Amps' cloud value).

        Sums all three phases — Charge Amps sessions on this circuit have been
        observed using only 2 of 3 phases (L1+L2, L3=0A), so summing all three
        is required to not silently undercount real load; see module
        docstring for the Pro EM -> Pro 3EM correction.
        Returns 0.0 if shelly_prefix not set or all phases unavailable.
        """
        if not self.shelly_prefix:
            return 0.0
        total_w = 0.0
        for phase in ("a", "b", "c"):
            current = self._state_by_id(f"sensor.{self.shelly_prefix}_phase_{phase}_current")
            voltage = self._state_by_id(
                f"sensor.{self.shelly_prefix}_phase_{phase}_voltage", default=230.0
            )
            total_w += current * voltage
        return total_w

    @property
    def power_w(self) -> float:
        """EV charging power — prefers Shelly Pro 3EM (fast/local) over Charge Amps' own reading."""
        shelly = self.shelly_power_w
        if shelly > 10:  # > 10W = valid reading, not noise
            return shelly
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
        body: dict[str, Any] = {
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
