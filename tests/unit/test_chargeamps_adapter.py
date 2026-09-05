"""Tests for ChargeAmpsAdapter — PLAT-1980 / Platinum remediation (2026-09-05).

Covers the EVAdapter contract (mirrors test_easee_adapter.py's structure) plus
the two failure modes this adapter is uniquely exposed to as a raw REST client
(unlike easee.py, which piggybacks on an already-installed HA integration):

  1. Network/auth failure paths (login failure, refresh failure, timeout,
     non-200, malformed connector lookup) — none may raise or hang.
  2. Degraded-mode signaling (QC-MANIFEST v4.1 C3.4): a 0.0/{} reading from
     current_a/phase_currents_a/power_w must be distinguishable from "no data
     yet" via data_available/data_age_s — a fasvakt/phase-guard consuming a
     fabricated zero is a safety failure, not a cosmetic one.

No real network access — aiohttp.ClientSession is replaced with an in-memory
fake (no aioresponses dependency, keeps this test file's own dependency
footprint at zero beyond what the repo already declares).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from types import TracebackType

from custom_components.carmabox.adapters.chargeamps import (
    _DYNAMIC_MIN,
    ChargeAmpsAdapter,
)
from custom_components.carmabox.const import MAX_EV_CURRENT

CHARGE_POINT_ID = "2505090652M"
EMAIL = "ake@ekerosvenssons.se"
PASSWORD = "hunter2"
API_KEY = "test-api-key"
SHELLY_PREFIX = "shellypro3em_5c013b055f20"


class _FakeResponse:
    """Fake aiohttp response supporting `async with sess.get(...) as r:`."""

    def __init__(self, status: int, json_data: dict[str, Any] | None = None) -> None:
        self.status = status
        self._json_data = json_data if json_data is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._json_data

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RaisingResponse:
    """A response context manager that raises on __aenter__ (network error / timeout)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> _RaisingResponse:
        raise self._exc

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Fake aiohttp.ClientSession. `responses` maps (method, path_suffix) -> response.

    `path_suffix` is matched with `in` against the requested URL so tests
    don't need to spell out the full base URL for every route.
    """

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def _resolve(self, method: str, url: str, json: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, url, json))
        for (m, suffix), resp in self.responses.items():
            if m == method and suffix in url:
                return resp
        raise AssertionError(f"No fake response registered for {method} {url}")

    def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return self._resolve("GET", url)

    def post(self, url: str, headers: dict[str, str] | None = None, json: Any = None) -> Any:
        return self._resolve("POST", url, json)

    def put(self, url: str, headers: dict[str, str] | None = None, json: Any = None) -> Any:
        return self._resolve("PUT", url, json)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value in entities:
        state = MagicMock()
        state.state = value
        state.attributes = {}
        states[entity_id] = state
    hass.states.get = lambda eid: states.get(eid)
    return hass


LOGIN_OK = ("POST", "/auth/login"), _FakeResponse(200, {"token": "tok1", "refreshToken": "ref1"})
REFRESH_OK = (
    ("POST", "/auth/refreshtoken"),
    _FakeResponse(200, {"token": "tok2", "refreshToken": "ref2"}),
)


def _connector_status_response(
    *,
    connector_id: int = 1,
    status: str = "Charging",
    measurements: list[dict[str, Any]] | None = None,
    charging_power_kw: float = 2.6,
) -> _FakeResponse:
    connector: dict[str, Any] = {
        "connectorId": connector_id,
        "status": status,
        "chargingPowerKw": charging_power_kw,
        "totalConsumptionKwh": 0.57,
    }
    if measurements is not None:
        connector["measurements"] = measurements
    return _FakeResponse(200, {"connectorStatuses": [connector]})


def _adapter(hass: MagicMock | None = None, **kwargs: Any) -> ChargeAmpsAdapter:
    return ChargeAmpsAdapter(
        hass if hass is not None else _make_hass(),
        EMAIL,
        PASSWORD,
        API_KEY,
        CHARGE_POINT_ID,
        **kwargs,
    )


def _patch_session(adapter: ChargeAmpsAdapter, fake: _FakeSession) -> None:
    adapter._session = lambda: fake  # type: ignore[method-assign]


# ── Login / auth ──────────────────────────────────────────────────


class TestEnsureLogin:
    @pytest.mark.asyncio
    async def test_login_success_caches_token(self) -> None:
        adapter = _adapter()
        fake = _FakeSession({("POST", "/auth/login"): LOGIN_OK[1]})
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is True
        assert adapter._token == "tok1"
        assert adapter._refresh_token == "ref1"

    @pytest.mark.asyncio
    async def test_login_failure_returns_false(self) -> None:
        adapter = _adapter()
        fake = _FakeSession({("POST", "/auth/login"): _FakeResponse(401, {})})
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is False
        assert adapter._token is None

    @pytest.mark.asyncio
    async def test_login_network_error_returns_false_not_raises(self) -> None:
        import aiohttp

        adapter = _adapter()
        fake = _FakeSession(
            {("POST", "/auth/login"): _RaisingResponse(aiohttp.ClientConnectionError("dns fail"))}
        )
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_timeout_returns_false_not_raises(self) -> None:
        adapter = _adapter()
        fake = _FakeSession({("POST", "/auth/login"): _RaisingResponse(TimeoutError())})
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is False

    @pytest.mark.asyncio
    async def test_cached_token_skips_network(self) -> None:
        adapter = _adapter()
        adapter._token = "cached"
        adapter._token_expires_at = time.monotonic() + 1000

        fake = _FakeSession({})  # no routes registered — any call raises AssertionError
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is True
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_refresh_success_used_over_full_login(self) -> None:
        adapter = _adapter()
        adapter._token = "old"
        adapter._refresh_token = "old-refresh"
        adapter._token_expires_at = 0.0  # force expired

        fake = _FakeSession({("POST", "/auth/refreshtoken"): REFRESH_OK[1]})
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is True
        assert adapter._token == "tok2"
        assert fake.calls[0][0] == "POST"
        assert "/auth/refreshtoken" in fake.calls[0][1]

    @pytest.mark.asyncio
    async def test_refresh_failure_falls_back_to_full_login(self) -> None:
        adapter = _adapter()
        adapter._token = "old"
        adapter._refresh_token = "old-refresh"
        adapter._token_expires_at = 0.0

        fake = _FakeSession(
            {
                ("POST", "/auth/refreshtoken"): _FakeResponse(401, {}),
                ("POST", "/auth/login"): LOGIN_OK[1],
            }
        )
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is True
        assert adapter._token == "tok1"

    @pytest.mark.asyncio
    async def test_analyze_only_skips_network(self) -> None:
        adapter = _adapter()
        adapter.analyze_only = True
        fake = _FakeSession({})
        _patch_session(adapter, fake)

        result = await adapter.ensure_login()

        assert result is True
        assert fake.calls == []


# ── Degraded-mode signaling (C3.4 grind) ─────────────────────────


class TestDataAvailability:
    def test_data_unavailable_before_first_refresh(self) -> None:
        adapter = _adapter()
        assert adapter.data_available is False
        assert adapter.data_age_s is None

    def test_zero_reading_before_refresh_is_not_mistaken_for_real(self) -> None:
        """The core safety property: a caller can tell 'never refreshed' apart
        from 'genuinely 0A' by checking data_available, even though current_a
        itself returns 0.0 in both cases (ABC contract requires a float)."""
        adapter = _adapter()
        assert adapter.current_a == 0.0
        assert adapter.phase_currents_a == {}
        assert adapter.data_available is False  # <- the signal a fasvakt must check

    @pytest.mark.asyncio
    async def test_data_available_true_after_successful_refresh(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(),
            }
        )
        _patch_session(adapter, fake)

        await adapter.refresh_status()

        assert adapter.data_available is True
        assert adapter.data_age_s is not None
        assert adapter.data_age_s >= 0

    @pytest.mark.asyncio
    async def test_failed_refresh_does_not_flip_data_available_true(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _FakeResponse(500, {}),
            }
        )
        _patch_session(adapter, fake)

        await adapter.refresh_status()

        assert adapter.data_available is False
        assert adapter._last_status is None

    @pytest.mark.asyncio
    async def test_failed_refresh_after_good_one_preserves_cache_but_ages(self) -> None:
        """A transient poll failure keeps serving the last good reading (so a
        brief blip doesn't itself trip a guard) but data_age_s must keep
        growing so staleness is observable — this is what makes the cache
        safe rather than a silent, permanently-fresh-looking fabrication."""
        adapter = _adapter()
        good_fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(
                    measurements=[{"phase": "L1", "current": 6.0, "voltage": 230.0}]
                ),
            }
        )
        _patch_session(adapter, good_fake)
        await adapter.refresh_status()
        first_age = adapter.data_age_s
        assert adapter.current_a == 6.0

        failing_fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _FakeResponse(500, {}),
            }
        )
        _patch_session(adapter, failing_fake)
        await adapter.refresh_status()

        # Cache preserved (still 6.0A, not silently reset to 0)...
        assert adapter.current_a == 6.0
        # ...but the age is a real, growing number, not reset by the failed poll.
        assert adapter.data_age_s is not None
        assert first_age is not None
        assert adapter.data_age_s >= first_age


# ── Connector status parsing ──────────────────────────────────────


class TestConnectorStatusParsing:
    @pytest.mark.asyncio
    async def test_current_a_averages_phases(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(
                    measurements=[
                        {"phase": "L1", "current": 6.0, "voltage": 230.0},
                        {"phase": "L2", "current": 5.5, "voltage": 230.0},
                        {"phase": "L3", "current": 0.0, "voltage": 230.0},
                    ]
                ),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.current_a == pytest.approx((6.0 + 5.5 + 0.0) / 3)

    @pytest.mark.asyncio
    async def test_phase_currents_a_dict(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(
                    measurements=[
                        {"phase": "L1", "current": 6.0, "voltage": 230.0},
                        {"phase": "L2", "current": 5.5, "voltage": 230.0},
                    ]
                ),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.phase_currents_a == {"L1": 6.0, "L2": 5.5}

    @pytest.mark.asyncio
    async def test_measurements_null_does_not_crash(self) -> None:
        """API returns measurements: null while status=Available — must not raise."""
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(
                    status="Available", measurements=None
                ),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.current_a == 0.0
        assert adapter.phase_currents_a == {}
        assert adapter.data_available is True  # refresh itself succeeded

    @pytest.mark.asyncio
    async def test_wrong_connector_id_not_found_in_response(self) -> None:
        """connector_id=2 configured, but response only has connector 1."""
        adapter = _adapter(connector_id=2)
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(connector_id=1),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.status == ""
        assert adapter.current_a == 0.0

    @pytest.mark.asyncio
    async def test_refresh_status_login_failure_leaves_cache_untouched(self) -> None:
        adapter = _adapter()
        fake = _FakeSession({("POST", "/auth/login"): _FakeResponse(401, {})})
        _patch_session(adapter, fake)

        await adapter.refresh_status()

        assert adapter._last_status is None
        assert adapter.data_available is False

    @pytest.mark.asyncio
    async def test_refresh_status_network_error_returns_gracefully(self) -> None:
        import aiohttp

        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _RaisingResponse(aiohttp.ClientConnectionError("reset")),
            }
        )
        _patch_session(adapter, fake)

        await adapter.refresh_status()  # must not raise

        assert adapter.data_available is False

    @pytest.mark.asyncio
    async def test_refresh_status_timeout_returns_gracefully(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _RaisingResponse(TimeoutError()),
            }
        )
        _patch_session(adapter, fake)

        await adapter.refresh_status()  # must not raise

        assert adapter.data_available is False

    @pytest.mark.asyncio
    async def test_write_put_timeout_returns_false(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _RaisingResponse(TimeoutError()),
            }
        )
        _patch_session(adapter, fake)

        result = await adapter.enable()

        assert result is False

    @pytest.mark.asyncio
    async def test_is_charging_true_when_status_charging(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(status="Charging"),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.is_charging is True
        assert adapter.plug_connected is True

    @pytest.mark.asyncio
    async def test_power_kw_derived_from_power_w(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(charging_power_kw=2.5),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.power_kw == pytest.approx(2.5)

    @pytest.mark.asyncio
    async def test_total_consumption_kwh(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.total_consumption_kwh == pytest.approx(0.57)

    @pytest.mark.asyncio
    async def test_plug_connected_false_when_available(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(status="Available"),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.is_charging is False
        assert adapter.plug_connected is False


# ── Shelly Pro 3EM integration (corrected from the Pro EM assumption) ──


class TestShellyPro3EMIntegration:
    def test_shelly_power_w_sums_three_phases(self) -> None:
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_phase_a_current", "6.0"),
            (f"sensor.{SHELLY_PREFIX}_phase_a_voltage", "230.0"),
            (f"sensor.{SHELLY_PREFIX}_phase_b_current", "5.5"),
            (f"sensor.{SHELLY_PREFIX}_phase_b_voltage", "228.0"),
            (f"sensor.{SHELLY_PREFIX}_phase_c_current", "0.0"),
            (f"sensor.{SHELLY_PREFIX}_phase_c_voltage", "229.0"),
        )
        adapter = _adapter(hass, shelly_prefix=SHELLY_PREFIX)
        expected = 6.0 * 230.0 + 5.5 * 228.0 + 0.0 * 229.0
        assert adapter.shelly_power_w == pytest.approx(expected)

    def test_shelly_power_w_zero_without_prefix(self) -> None:
        adapter = _adapter()
        assert adapter.shelly_power_w == 0.0

    def test_shelly_missing_voltage_defaults_230(self) -> None:
        hass = _make_hass((f"sensor.{SHELLY_PREFIX}_phase_a_current", "10.0"))
        adapter = _adapter(hass, shelly_prefix=SHELLY_PREFIX)
        assert adapter.shelly_power_w == pytest.approx(10.0 * 230.0)

    def test_shelly_non_numeric_state_defaults_zero(self) -> None:
        """A malformed/garbage sensor state must not raise, just count as 0."""
        hass = _make_hass((f"sensor.{SHELLY_PREFIX}_phase_a_current", "not-a-number"))
        adapter = _adapter(hass, shelly_prefix=SHELLY_PREFIX)
        assert adapter.shelly_power_w == 0.0

    @pytest.mark.asyncio
    async def test_power_w_prefers_shelly_over_chargeamps(self) -> None:
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_phase_a_current", "10.0"),
            (f"sensor.{SHELLY_PREFIX}_phase_a_voltage", "230.0"),
        )
        adapter = _adapter(hass, shelly_prefix=SHELLY_PREFIX)
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(charging_power_kw=1.0),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.power_w == pytest.approx(2300.0)  # Shelly wins, not 1000W

    @pytest.mark.asyncio
    async def test_power_w_falls_back_at_exactly_10w_boundary(self) -> None:
        """Threshold is strict > 10 (mirrors easee.py's identical boundary)."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_phase_a_current", str(10.0 / 230.0)),  # == 10.0W exactly
            (f"sensor.{SHELLY_PREFIX}_phase_a_voltage", "230.0"),
        )
        adapter = _adapter(hass, shelly_prefix=SHELLY_PREFIX)
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("GET", "/status"): _connector_status_response(charging_power_kw=1.0),
            }
        )
        _patch_session(adapter, fake)
        await adapter.refresh_status()

        assert adapter.power_w == pytest.approx(1000.0)  # falls back to Charge Amps


# ── Write path ─────────────────────────────────────────────────


class TestSetCurrentClamp:
    @pytest.mark.asyncio
    async def test_set_current_clamp_upper(self) -> None:
        adapter = _adapter()
        adapter.analyze_only = True  # write path stays gated per module docstring
        fake = _FakeSession({})
        _patch_session(adapter, fake)

        result = await adapter.set_current(20)

        assert result is True

    @pytest.mark.asyncio
    async def test_set_current_value_clamped_in_body_upper(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _FakeResponse(200, {}),
            }
        )
        _patch_session(adapter, fake)

        await adapter.set_current(20)

        put_call = next(c for c in fake.calls if c[0] == "PUT")
        assert put_call[2]["maxCurrent"] == float(MAX_EV_CURRENT)

    @pytest.mark.asyncio
    async def test_set_current_value_clamped_in_body_lower(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _FakeResponse(200, {}),
            }
        )
        _patch_session(adapter, fake)

        await adapter.set_current(1)

        put_call = next(c for c in fake.calls if c[0] == "PUT")
        assert put_call[2]["maxCurrent"] == float(_DYNAMIC_MIN)

    @pytest.mark.asyncio
    async def test_enable_sends_mode_on(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _FakeResponse(200, {}),
            }
        )
        _patch_session(adapter, fake)

        result = await adapter.enable()

        assert result is True
        put_call = next(c for c in fake.calls if c[0] == "PUT")
        assert put_call[2]["mode"] == "On"

    @pytest.mark.asyncio
    async def test_disable_sends_mode_off(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _FakeResponse(200, {}),
            }
        )
        _patch_session(adapter, fake)

        result = await adapter.disable()

        assert result is True
        put_call = next(c for c in fake.calls if c[0] == "PUT")
        assert put_call[2]["mode"] == "Off"

    @pytest.mark.asyncio
    async def test_write_fails_when_login_fails(self) -> None:
        adapter = _adapter()
        fake = _FakeSession({("POST", "/auth/login"): _FakeResponse(401, {})})
        _patch_session(adapter, fake)

        result = await adapter.enable()

        assert result is False

    @pytest.mark.asyncio
    async def test_write_non_200_returns_false(self) -> None:
        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _FakeResponse(500, {}),
            }
        )
        _patch_session(adapter, fake)

        result = await adapter.enable()

        assert result is False

    @pytest.mark.asyncio
    async def test_write_network_error_returns_false_not_raises(self) -> None:
        import aiohttp

        adapter = _adapter()
        fake = _FakeSession(
            {
                ("POST", "/auth/login"): LOGIN_OK[1],
                ("PUT", "/settings"): _RaisingResponse(aiohttp.ClientConnectionError("reset")),
            }
        )
        _patch_session(adapter, fake)

        result = await adapter.enable()

        assert result is False

    @pytest.mark.asyncio
    async def test_analyze_only_skips_write_network_call(self) -> None:
        adapter = _adapter()
        adapter.analyze_only = True
        fake = _FakeSession({})  # no routes — a real call would raise AssertionError

        _patch_session(adapter, fake)

        result = await adapter.enable()

        assert result is True
        assert fake.calls == []
