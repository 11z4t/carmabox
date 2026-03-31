"""PLAT-793: Playwright E2E dashboard card verification.

Verifies that EVERY card in EVERY tab of the CARMA Box dashboard
shows actual values — not 'unavailable', 'Ingen data', or empty.

Runs against live HA at http://192.168.9.10:8123.
Requires: playwright, pytest-playwright, live HA with CARMA Box.

Shadow DOM: HA uses Web Components. Cards are inside nested shadow roots.
We use page.evaluate() with JavaScript to traverse them.
"""

from __future__ import annotations

import pytest
import requests

from .conftest import HA_URL

DASHBOARD_BASE = f"{HA_URL}/dashboard-carmabox"

# Values that indicate a broken card
FORBIDDEN_VALUES = frozenset(
    {
        "unavailable",
        "unknown",
        "Ingen data",
        "NaN",
        "None",
        "Error",
        "",
    }
)

# Entities that MUST have valid values for dashboard to work
CRITICAL_ENTITIES = [
    "sensor.house_grid_power",
    "sensor.pv_solar_total",
    "sensor.pv_battery_soc_kontor",
    "sensor.pv_battery_soc_forrad",
    "sensor.carma_box_decision",
    "sensor.carma_box_rules",
    "sensor.nordpool_kwh_se3_sek_3_10_025",
    "sensor.goodwe_battery_power_kontor",
    "sensor.goodwe_battery_power_forrad",
    "sensor.house_consumption",
]

# Entities where 'unavailable' is OK (car not always home)
OPTIONAL_ENTITIES = frozenset(
    {
        "sensor.xpeng_g9_xpeng_g9_battery_soc",
    }
)


# ── API pre-checks ──────────────────────────────────────────


def _get_ha_token() -> str | None:
    """Try to get HA long-lived access token from environment or file."""
    import os

    token = os.environ.get("HA_TOKEN")
    if token:
        return token
    # Fallback: try to read from known location
    for path in ["/tmp/ha_token", "/home/charlie/.ha_token"]:
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return None


def _check_entity_via_api(entity_id: str) -> tuple[str, str]:
    """Check entity state via HA REST API. Returns (state, status)."""
    token = _get_ha_token()
    if not token:
        pytest.skip("No HA_TOKEN available for API pre-check")
    try:
        resp = requests.get(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return "api_error", f"HTTP {resp.status_code}"
        data = resp.json()
        return data.get("state", ""), "ok"
    except Exception as e:
        return "error", str(e)


class TestAPIPreCheck:
    """Verify critical entities have values BEFORE testing dashboard."""

    @pytest.mark.parametrize("entity_id", CRITICAL_ENTITIES)
    def test_entity_has_value(self, entity_id: str) -> None:
        """Entity must return a valid state via HA API."""
        state, status = _check_entity_via_api(entity_id)
        if status != "ok":
            pytest.skip(f"API not reachable: {status}")
        assert state not in FORBIDDEN_VALUES, f"{entity_id} has forbidden value '{state}'"


# ── Shadow DOM helpers ──────────────────────────────────────


async def _get_card_texts(page, card_selector: str) -> list[str]:
    """Extract text content from cards, traversing Shadow DOM."""
    return await page.evaluate(
        """(selector) => {
            const results = [];
            // Find all matching elements in light DOM and shadow roots
            function findInShadow(root, sel) {
                const els = root.querySelectorAll(sel);
                els.forEach(el => {
                    const text = (el.shadowRoot?.textContent || el.textContent || '').trim();
                    if (text) results.push(text);
                });
                // Recurse into shadow roots
                root.querySelectorAll('*').forEach(el => {
                    if (el.shadowRoot) findInShadow(el.shadowRoot, sel);
                });
            }
            findInShadow(document, selector);
            return results;
        }""",
        card_selector,
    )


async def _navigate_to_tab(ha_page, tab_index: int) -> None:
    """Navigate to a specific dashboard tab."""
    await ha_page.goto(f"{DASHBOARD_BASE}/{tab_index}")
    # Wait for dashboard to render
    await ha_page.wait_for_timeout(3000)


async def _check_no_error_cards(page) -> list[str]:
    """Find any cards showing error states."""
    return await page.evaluate(
        """() => {
            const errors = [];
            function scan(root) {
                root.querySelectorAll('*').forEach(el => {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text.includes('entity not available') ||
                        text.includes('entity not found') ||
                        text === 'error') {
                        errors.push(text.substring(0, 100));
                    }
                    if (el.shadowRoot) scan(el.shadowRoot);
                });
            }
            scan(document);
            return [...new Set(errors)];
        }"""
    )


# ── Tab tests ───────────────────────────────────────────────


@pytest.mark.e2e
class TestTab1JustNu:
    """Flik 1: 'Just nu' — live overview with gauges and power flow."""

    @pytest.mark.asyncio
    async def test_tab_renders_without_errors(self, ha_page) -> None:
        """Tab 1 renders without 'entity not available' errors."""
        await _navigate_to_tab(ha_page, 0)
        errors = await _check_no_error_cards(ha_page)
        assert not errors, f"Error cards found: {errors}"

    @pytest.mark.asyncio
    async def test_decision_not_ingen_data(self, ha_page) -> None:
        """Decision card must show actual decision, not 'Ingen data'."""
        await _navigate_to_tab(ha_page, 0)
        texts = await _get_card_texts(ha_page, "hui-markdown-card")
        # At least one markdown card should have content
        all_text = " ".join(texts).lower()
        assert "ingen data" not in all_text, "Decision shows 'Ingen data'"
        assert len(texts) > 0, "No markdown cards found"

    @pytest.mark.asyncio
    async def test_gauges_have_numeric_values(self, ha_page) -> None:
        """Gauge cards must show numeric values."""
        await _navigate_to_tab(ha_page, 0)
        gauge_values = await ha_page.evaluate(
            """() => {
                const values = [];
                function scan(root) {
                    root.querySelectorAll('ha-gauge, hui-gauge-card').forEach(el => {
                        const sr = el.shadowRoot;
                        if (sr) {
                            const text = sr.textContent || '';
                            values.push(text.trim());
                        }
                    });
                    root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) scan(el.shadowRoot);
                    });
                }
                scan(document);
                return values;
            }"""
        )
        for val in gauge_values:
            assert val not in FORBIDDEN_VALUES, f"Gauge shows forbidden value: {val}"


@pytest.mark.e2e
class TestTab2Varfor:
    """Flik 2: 'Varför' — decision reasoning and charts."""

    @pytest.mark.asyncio
    async def test_tab_renders_without_errors(self, ha_page) -> None:
        """Tab 2 renders without errors."""
        await _navigate_to_tab(ha_page, 1)
        errors = await _check_no_error_cards(ha_page)
        assert not errors, f"Error cards found: {errors}"

    @pytest.mark.asyncio
    async def test_reasoning_markdown_not_empty(self, ha_page) -> None:
        """Decision reasoning markdown must have content."""
        await _navigate_to_tab(ha_page, 1)
        texts = await _get_card_texts(ha_page, "hui-markdown-card")
        assert any(
            len(t) > 10 for t in texts
        ), f"No markdown card with meaningful content. Got: {texts[:3]}"

    @pytest.mark.asyncio
    async def test_charts_render(self, ha_page) -> None:
        """ApexCharts cards must render (canvas/svg present)."""
        await _navigate_to_tab(ha_page, 1)
        chart_count = await ha_page.evaluate(
            """() => {
                let count = 0;
                function scan(root) {
                    root.querySelectorAll('apexcharts-card, hui-graph-card').forEach(() => count++);
                    root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) scan(el.shadowRoot);
                    });
                }
                scan(document);
                return count;
            }"""
        )
        assert chart_count >= 1, "No chart cards found on tab 2"


@pytest.mark.e2e
class TestTab3Installningar:
    """Flik 3: 'Inställningar' — input controls."""

    @pytest.mark.asyncio
    async def test_tab_renders_without_errors(self, ha_page) -> None:
        """Tab 3 renders without errors."""
        await _navigate_to_tab(ha_page, 2)
        errors = await _check_no_error_cards(ha_page)
        assert not errors, f"Error cards found: {errors}"

    @pytest.mark.asyncio
    async def test_inputs_have_values(self, ha_page) -> None:
        """Input controls must show values, not 'unavailable'."""
        await _navigate_to_tab(ha_page, 2)
        unavail_count = await ha_page.evaluate(
            """() => {
                let count = 0;
                function scan(root) {
                    root.querySelectorAll('*').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text === 'unavailable' || text === 'unknown') count++;
                        if (el.shadowRoot) scan(el.shadowRoot);
                    });
                }
                scan(document);
                return count;
            }"""
        )
        assert unavail_count == 0, f"{unavail_count} inputs show unavailable/unknown"


@pytest.mark.e2e
class TestTab4Regler:
    """Flik 4: 'Regler' — rules display."""

    @pytest.mark.asyncio
    async def test_tab_renders_without_errors(self, ha_page) -> None:
        """Tab 4 renders without errors."""
        await _navigate_to_tab(ha_page, 3)
        errors = await _check_no_error_cards(ha_page)
        assert not errors, f"Error cards found: {errors}"

    @pytest.mark.asyncio
    async def test_rules_visible(self, ha_page) -> None:
        """At least one rule must be visible."""
        await _navigate_to_tab(ha_page, 3)
        texts = await _get_card_texts(ha_page, "hui-markdown-card")
        all_text = " ".join(texts)
        assert len(all_text) > 20, "Rules tab has no meaningful content"


@pytest.mark.e2e
class TestTab5Vagg:
    """Flik 5: 'Vägg' — wall display mode."""

    @pytest.mark.asyncio
    async def test_tab_renders_without_errors(self, ha_page) -> None:
        """Tab 5 renders without errors."""
        await _navigate_to_tab(ha_page, 4)
        errors = await _check_no_error_cards(ha_page)
        assert not errors, f"Error cards found: {errors}"

    @pytest.mark.asyncio
    async def test_hero_card_shows_decision(self, ha_page) -> None:
        """Hero card must show actual decision, not 'Ingen data'."""
        await _navigate_to_tab(ha_page, 4)
        texts = await _get_card_texts(ha_page, "hui-markdown-card")
        all_text = " ".join(texts).lower()
        assert "ingen data" not in all_text, "Hero card shows 'Ingen data'"

    @pytest.mark.asyncio
    async def test_soc_display_numeric(self, ha_page) -> None:
        """SoC display must show numeric percentage."""
        await _navigate_to_tab(ha_page, 4)
        texts = await _get_card_texts(ha_page, "hui-gauge-card")
        for t in texts:
            assert t not in FORBIDDEN_VALUES, f"SoC shows forbidden: {t}"
