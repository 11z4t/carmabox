"""Playwright E2E test configuration."""
import pytest

HA_URL = "http://192.168.9.10:8123"
HA_USER = "test"
HA_PASS = "carmabox123"


@pytest.fixture(scope="session")
def browser_context_args():
    """Browser context with viewport."""
    return {"viewport": {"width": 1366, "height": 768}}


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
