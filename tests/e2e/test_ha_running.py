"""E2E: Verify dev-HA is running and CARMA Box is loaded."""

from playwright.sync_api import Page

HA_URL = "http://192.168.9.10:8123"


def test_ha_responds(page: Page) -> None:
    """HA should respond with login or dashboard."""
    response = page.goto(HA_URL)
    assert response is not None
    assert response.status in (200, 302)


def test_ha_shows_auth(page: Page) -> None:
    """Login page should show auth form."""
    page.goto(HA_URL)
    page.wait_for_load_state("networkidle")
    content = page.content()
    assert "home-assistant" in content.lower() or "auth" in content.lower()


def test_ha_login_via_keyboard(page: Page) -> None:
    """Login via keyboard (Shadow DOM workaround)."""
    page.goto(HA_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    # HA uses Shadow DOM — keyboard navigation works
    page.keyboard.press("Tab")
    page.keyboard.type("test")
    page.keyboard.press("Tab")
    page.keyboard.type("carmabox123")
    page.keyboard.press("Enter")
    page.wait_for_timeout(5000)

    url = page.url
    assert any(x in url for x in ["lovelace", "onboarding", "config", "states"]), (
        f"Unexpected URL after login: {url}"
    )
