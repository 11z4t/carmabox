"""Playwright E2E test configuration."""

from __future__ import annotations

import os

import pytest

HA_URL = os.environ.get("HA_URL", "http://192.168.9.10:8123")
HA_USER = os.environ.get("HA_USER", "test")
HA_PASS = os.environ.get("HA_PASS", "carmabox123")


@pytest.fixture(scope="session")
def browser_context_args():
    """Browser context with viewport."""
    return {"viewport": {"width": 1366, "height": 768}}


@pytest.fixture(scope="session")
def ha_token() -> str:
    """Long-lived HA access token from HA_TOKEN env var.

    Required for API pre-check tests. Tests that use this fixture will skip
    automatically if HA_TOKEN is not set. Generate via HA → Profile → Security
    → Long-lived access tokens and export: export HA_TOKEN=<token>
    """
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        pytest.skip("HA_TOKEN env var not set — skipping API pre-check")
    return token


@pytest.fixture
async def ha_page(page):
    """Login to HA and yield authenticated page."""
    await page.goto(HA_URL)
    # Wait for login page
    await page.wait_for_selector("input[name='username'], ha-auth-flow", timeout=10000)

    # Fill login
    try:
        await page.fill("input[name='username']", HA_USER)
        await page.fill("input[name='password']", HA_PASS)
        await page.click("button[type='submit']")
    except Exception:
        # HA 2024+ uses ha-auth-flow web component
        auth = page.locator("ha-auth-flow")
        await auth.locator("input").first.fill(HA_USER)
        await auth.locator("input").last.fill(HA_PASS)
        await auth.locator("mwc-button, ha-button").click()

    # Wait for dashboard
    await page.wait_for_url("**/lovelace**", timeout=15000)
    yield page
