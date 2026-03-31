"""Playwright E2E test configuration — network access enabled."""

from __future__ import annotations

import os
import socket

import pytest
import pytest_socket as _ps

HA_URL = os.environ.get("HA_URL", "http://192.168.5.22:8123")
HA_USER = os.environ.get("HA_USER", "admin")
HA_PASS = os.environ.get("HA_PASS", "carmabox123")


def _unrestrict_socket() -> None:
    """Fully undo pytest-homeassistant-custom-component socket restrictions."""
    _ps.enable_socket()
    socket.socket.connect = _ps._true_connect  # type: ignore[attr-defined]


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item) -> None:
    """Re-enable network AFTER HA plugin's per-test socket restriction (trylast)."""
    _unrestrict_socket()


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session) -> None:
    """Unrestrict network at session start."""
    _unrestrict_socket()


@pytest.fixture(scope="session", autouse=True)
def _session_unrestrict_network():
    """Session fixture: unrestrict socket before any session-scoped fixtures run."""
    _unrestrict_socket()
    yield


@pytest.fixture(autouse=True)
def _allow_network():
    """Per-test fixture: re-enable network."""
    _unrestrict_socket()
    yield


@pytest.fixture(scope="session")
def browser_context_args():
    """Browser context with viewport."""
    return {"viewport": {"width": 1366, "height": 768}}


@pytest.fixture(scope="session")
def ha_token() -> str:
    """Long-lived HA access token from HA_TOKEN env var."""
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        pytest.skip("HA_TOKEN env var not set — skipping API pre-check")
    return token


@pytest.fixture
async def ha_page(page):
    """Login to HA and yield authenticated page."""
    await page.goto(HA_URL)
    await page.wait_for_selector("input[name='username'], ha-auth-flow", timeout=10000)

    try:
        await page.fill("input[name='username']", HA_USER)
        await page.fill("input[name='password']", HA_PASS)
        await page.click("button[type='submit']")
    except Exception:
        auth = page.locator("ha-auth-flow")
        await auth.locator("input").first.fill(HA_USER)
        await auth.locator("input").last.fill(HA_PASS)
        await auth.locator("mwc-button, ha-button").click()

    await page.wait_for_url("**/lovelace**", timeout=15000)
    yield page


@pytest.fixture(autouse=True)
def verify_cleanup():
    """No-op override — prevent HA plugin verify_cleanup from conflicting with Playwright."""
    yield


@pytest.fixture(autouse=True)
def enable_event_loop_debug():
    """No-op override — prevent HA plugin from modifying Playwright's event loop."""


@pytest.fixture(autouse=True)
def expected_lingering_tasks():
    """Override HA default — not applicable to e2e tests."""
    return True


@pytest.fixture(autouse=True)
def expected_lingering_timers():
    """Override HA default — not applicable to e2e tests."""
    return True
