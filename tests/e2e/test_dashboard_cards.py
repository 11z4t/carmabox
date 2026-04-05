"""E2E: PLAT-793 — CARMA Box dashboard card tests (alla flikar, alla kritiska kort).

Kör med:
    HA_URL=http://192.168.5.22:8123 \\
    HA_USER=<user> HA_PASS=<pass> HA_TOKEN=<token> \\
        xvfb-run python -m pytest tests/e2e/test_dashboard_cards.py -v

Kräver:
  - HA nåbar på HA_URL (default: http://192.168.5.22:8123)
  - HA_TOKEN: long-lived access token för API-prechecks
    (HA → Profil → Säkerhet → Långlivade åtkomsttoken)
  - HA_USER / HA_PASS: för Playwright browser-login

Dashboard-paths (v29.x):
  carma=Flik1  detaljer=Flik2  installningar=Flik3  regler=Flik4  wall=Flik5
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import pytest
import requests

# ── Konfiguration ──────────────────────────────────────────────────────────────

HA_URL = os.environ.get("HA_URL", "http://192.168.5.22:8123")
HA_USER = os.environ.get("HA_USER", "test")
HA_PASS = os.environ.get("HA_PASS", "carmabox123")

SCREENSHOT_DIR = Path("/tmp/carmabox_e2e_screenshots")

# States som ALDRIG ska förekomma i core-entities
FORBIDDEN_API_STATES = frozenset({"unavailable", "unknown", "", "none", "nan"})

# ── Dashboard URL:er (namngivna paths, inte numeriska) ────────────────────────

_D = f"{HA_URL}/dashboard-carmabox"
TAB_JUST_NU = f"{_D}/carma"
TAB_VARFOR = f"{_D}/detaljer"
TAB_INSTALLNINGAR = f"{_D}/installningar"
TAB_REGLER = f"{_D}/regler"
TAB_VAGG = f"{_D}/wall"

# ── JavaScript: djup Shadow DOM-traversal ─────────────────────────────────────

# Samlar ALL text ur shadow DOM (max djup 25, WeakSet mot cirklar)
_SHADOW_TEXT_JS = """
(el) => {
    const seen = new WeakSet();
    function collect(node, depth) {
        if (!node || depth > 25 || seen.has(node)) return '';
        seen.add(node);
        let t = '';
        if (node.shadowRoot) t += collect(node.shadowRoot, depth + 1);
        for (const c of (node.childNodes || [])) {
            if (c.nodeType === 3) t += (c.textContent || '');
            else t += collect(c, depth + 1);
        }
        return t;
    }
    return collect(el, 0);
}
"""

# Söker efter en specifik tagg-typ i shadow DOM (t.ex. 'apexcharts-card')
_HAS_TAG_JS = """
([el, tagName]) => {
    const tag = tagName.toLowerCase();
    const seen = new WeakSet();
    function find(node, depth) {
        if (!node || depth > 25 || seen.has(node)) return false;
        seen.add(node);
        if (node.tagName && node.tagName.toLowerCase() === tag) return true;
        if (node.shadowRoot && find(node.shadowRoot, depth + 1)) return true;
        for (const c of (node.children || [])) {
            if (find(c, depth + 1)) return true;
        }
        return false;
    }
    return find(el, 0);
}
"""


# ── Hjälpfunktioner ────────────────────────────────────────────────────────────


def _screenshot(page, name: str) -> None:
    """Spara screenshot för debugging vid testfel."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}_{int(time.time())}.png"
    with contextlib.suppress(Exception):
        page.screenshot(path=str(path))


def _api_state(token: str, entity_id: str) -> dict:
    """Hämta HA state-dict för entity via REST API. Returnerar {} vid fel."""
    r = requests.get(
        f"{HA_URL}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return r.json() if r.status_code == 200 else {}


def _state_val(token: str, entity_id: str) -> str:
    """Returnera state-sträng för entity."""
    return _api_state(token, entity_id).get("state", "")


async def _shadow_text(page) -> str:
    """Hämta all text ur hela HA:s shadow DOM."""
    body = await page.query_selector("body")
    if body is None:
        return ""
    return await page.evaluate(_SHADOW_TEXT_JS, body)


async def _login_and_goto(page, url: str) -> None:
    """Logga in i HA via keyboard-input (shadow DOM-säkert) och navigera till url."""
    await page.goto(HA_URL)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)
    # Keyboard-login: kompatibel med alla HA-versioner och shadow DOM auth-flow
    await page.keyboard.press("Tab")
    await page.keyboard.type(HA_USER)
    await page.keyboard.press("Tab")
    await page.keyboard.type(HA_PASS)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(4000)
    await page.goto(url)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(3000)


# ── Session-fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def ha_reachable():
    """Hoppa över alla tester om HA inte svarar.

    Separerar infrastrukturfel från buggar: om HA är nere
    skippas testerna istf att misslyckas med kryptiska nätverksfel.
    """
    try:
        r = requests.get(f"{HA_URL}/api/", timeout=5)
        assert r.status_code in (200, 401, 403), f"Oväntat HTTP-svar: {r.status_code}"
    except Exception as exc:
        pytest.skip(f"HA inte nåbar på {HA_URL}: {exc}")


@pytest.fixture(scope="session")
def ha_api_token(ha_reachable) -> str:
    """Long-lived API-token från HA_TOKEN env var.

    Generera via: HA → Profil → Säkerhet → Långlivade åtkomsttoken
    Exportera:    export HA_TOKEN=<token>
    """
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        pytest.skip("HA_TOKEN saknas — hoppar över API-prechecks")
    return token


# ── Tab-fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
async def tab_just_nu(page, ha_reachable):
    """Autentiserad Playwright-page på Tab 1 — Just nu (/dashboard-carmabox/carma)."""
    await _login_and_goto(page, TAB_JUST_NU)
    yield page
    _screenshot(page, "tab1_just_nu")


@pytest.fixture
async def tab_varfor(page, ha_reachable):
    """Autentiserad Playwright-page på Tab 2 — Varför (/dashboard-carmabox/detaljer)."""
    await _login_and_goto(page, TAB_VARFOR)
    yield page
    _screenshot(page, "tab2_varfor")


@pytest.fixture
async def tab_installningar(page, ha_reachable):
    """Autentiserad Playwright-page på Tab 3 — Inställningar."""
    await _login_and_goto(page, TAB_INSTALLNINGAR)
    yield page
    _screenshot(page, "tab3_installningar")


@pytest.fixture
async def tab_regler(page, ha_reachable):
    """Autentiserad Playwright-page på Tab 4 — Regler."""
    await _login_and_goto(page, TAB_REGLER)
    yield page
    _screenshot(page, "tab4_regler")


@pytest.fixture
async def tab_vagg(page, ha_reachable):
    """Autentiserad Playwright-page på Tab 5 — Vägg (wall display)."""
    await _login_and_goto(page, TAB_VAGG)
    yield page
    _screenshot(page, "tab5_vagg")


# ══════════════════════════════════════════════════════════════════════════════
# GROUP A — API-prechecks
# Verifierar entity-states via REST API INNAN browser-tester.
# Fångar "sensor offline" tidigt utan att öppna browser.
# ══════════════════════════════════════════════════════════════════════════════

# Entities som ALLTID måste ha giltiga, numeriska värden
_CORE_ENTITIES = [
    "sensor.house_grid_power",
    "sensor.pv_solar_total",
    "sensor.pv_battery_soc_kontor",
    "sensor.pv_battery_soc_forrad",
    "sensor.carma_box_battery_soc",
    "sensor.carma_box_decision_reason",
    "sensor.nordpool_kwh_se3_sek_3_10_025",
    "sensor.goodwe_battery_power_kontor",
    "sensor.goodwe_battery_power_forrad",
    "sensor.house_consumption",
]


@pytest.mark.parametrize("entity_id", _CORE_ENTITIES)
def test_api_core_entity_not_forbidden(ha_api_token, entity_id):
    """Core-entity får ej ha forbidden state (unavailable/unknown/tom).

    Dessa sensorer är fundamentala för CARMA Box-funktion.
    Om de är unavailable/unknown är koordinatorn blind.
    """
    state = _state_val(ha_api_token, entity_id)
    assert state.lower() not in FORBIDDEN_API_STATES, f"{entity_id} har forbidden state: '{state}'"


def test_api_decision_reason_has_action_attribute(ha_api_token):
    """sensor.carma_box_decision_reason måste ha 'action'-attribut med giltigt värde.

    Action-attributet styr CARMA Box varje minut.
    Saknas det har koordinatorn slutat producera beslut.
    """
    data = _api_state(ha_api_token, "sensor.carma_box_decision_reason")
    assert data, "sensor.carma_box_decision_reason finns ej i HA"
    attrs = data.get("attributes", {})
    action = attrs.get("action", "")
    assert action not in (
        "",
        None,
        "unavailable",
        "unknown",
    ), f"decision_reason.action är ogiltigt: '{action}'. Tillgängliga attrs: {list(attrs.keys())}"


def test_api_plan_status_has_plan_attribute(ha_api_token):
    """sensor.carma_box_plan_status måste ha 'plan'-attribut (lista).

    Plan-attributet innehåller 24h-prognosen.
    Saknas det planerar inte CARMA Box framåt.
    """
    data = _api_state(ha_api_token, "sensor.carma_box_plan_status")
    assert data, "sensor.carma_box_plan_status finns ej i HA"
    attrs = data.get("attributes", {})
    assert (
        "plan" in attrs
    ), f"plan_status saknar 'plan'-attribut. Tillgängliga: {list(attrs.keys())}"


def test_api_nordpool_price_is_positive_number(ha_api_token):
    """Nordpool-priset måste vara ett positivt tal (öre/kWh).

    Utan giltigt elpris kan CARMA Box inte fatta ekonomiska beslut
    (billig nätladdning, peak shaving, exportvärde).
    """
    state = _state_val(ha_api_token, "sensor.nordpool_kwh_se3_sek_3_10_025")
    try:
        value = float(state)
    except (ValueError, TypeError):
        pytest.fail(f"Nordpool-pris är ej numeriskt: '{state}'")
    assert value > 0, f"Nordpool-pris är ej positivt: {value}"


def test_api_missing_entity_returns_404(ha_api_token):
    """HA API ska returnera 404 för en entitet som inte existerar.

    Negativt API-test: verifierar att svar är tillförlitliga och
    att vi inte råkar ut för falska 200:or.
    """
    r = requests.get(
        f"{HA_URL}/api/states/sensor.denna_sensor_finns_absolut_inte_xyz_123",
        headers={"Authorization": f"Bearer {ha_api_token}"},
        timeout=10,
    )
    assert r.status_code == 404, f"Förväntade 404, fick {r.status_code}"


def test_api_unauthenticated_request_returns_401():
    """HA API utan Authorization-header ska returnera 401.

    Säkerhetstest: verifierar att HA inte exponerar sensor-data publikt.
    """
    r = requests.get(
        f"{HA_URL}/api/states/sensor.house_grid_power",
        timeout=10,
    )
    assert (
        r.status_code == 401
    ), f"Förväntat 401 (Unauthorized), fick {r.status_code}. HA verkar exponera API publikt!"


# ══════════════════════════════════════════════════════════════════════════════
# GROUP B — Tab 1: Just nu  (/dashboard-carmabox/carma)
# Primär driftvy: apparater, sol/nät/batteri-gauges, power-flow, beslut.
# ══════════════════════════════════════════════════════════════════════════════


async def test_tab1_loads_without_load_errors(tab_just_nu):
    """Tab 1 ska ladda utan 'can't load'-felmeddelanden.

    HA visar 'Can't load ...' när ett kort-type saknas (custom card
    ej registrerat). Dessa bryter hela vyn.
    """
    page = tab_just_nu
    body = await _shadow_text(page)
    body_lower = body.lower()
    assert len(body_lower) > 200, "Tab 1 är tom — laddades ej"
    for err in ("can't load", "failed to load"):
        assert err not in body_lower, f"Tab 1 har laddningsfel: '{err}'"


async def test_tab1_solar_gauge_label_visible(tab_just_nu):
    """Sol-gauge (sensor.pv_solar_total) ska visa etiketten 'Sol'.

    Gauge-kortet renderar etiketten från 'name:'-fältet.
    Saknas den har kortet kraschat eller sensor är offline.
    """
    body = await _shadow_text(tab_just_nu)
    assert "sol" in body.lower(), "Sol-gauge-etikett saknas på Tab 1 — gauge laddades ej"


async def test_tab1_grid_power_label_visible(tab_just_nu):
    """Nät-gauge (sensor.house_grid_power) ska visa etiketten 'Nät'.

    Fundamental sensor — visar aktuell nätimport/-export i realtid.
    """
    body = await _shadow_text(tab_just_nu)
    assert "nät" in body.lower(), "Nät-gauge-etikett saknas på Tab 1"


async def test_tab1_battery_kontor_visible(tab_just_nu):
    """Batteri-SoC kontor ska visas på Tab 1.

    Operatören behöver se balansen mellan kontor- och förråds-
    batterierna (15 kWh resp. 5 kWh).
    """
    body = await _shadow_text(tab_just_nu)
    assert "kontor" in body.lower(), "Batteri-etikett 'Kontor' saknas på Tab 1"


async def test_tab1_battery_forrad_visible(tab_just_nu):
    """Batteri-SoC förråd ska visas på Tab 1."""
    body = await _shadow_text(tab_just_nu)
    assert (
        "förråd" in body.lower() or "forrad" in body.lower()
    ), "Batteri-etikett 'Förråd' saknas på Tab 1"


async def test_tab1_decision_not_ingen_data(tab_just_nu):
    """Besluts-markdown ska inte visa 'Ingen data'.

    'Ingen data' = Jinja-template fick ingen input från sensor.
    Tyder på att sensor.carma_box_decision_reason är offline.
    """
    body = await _shadow_text(tab_just_nu)
    assert "ingen data" not in body.lower(), (
        "Tab 1 besluts-kort visar 'Ingen data' — "
        "sensor.carma_box_decision_reason är troligen offline"
    )


async def test_tab1_apparatkort_renders(tab_just_nu):
    """Apparat pill-bar ska visa aktiva apparater ELLER fallback-text.

    Pill-bar visar Tvätt/Tumlare/Disk/Pool/VP/Elbil/Miner om de drar ström.
    'Inga apparater aktiva' visas när inget är på. Båda är korrekta.
    """
    body = await _shadow_text(tab_just_nu)
    body_lower = body.lower()
    has_active = any(
        lbl in body_lower for lbl in ["tvätt", "tumlare", "disk", "pool", "elbil", "miner", "vp"]
    )
    has_fallback = "inga apparater" in body_lower
    assert has_active or has_fallback, (
        "Tab 1 apparat pill-bar renderades ej "
        "(varken apparater eller 'Inga apparater aktiva' hittades)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# GROUP C — Tab 2: Varför  (/dashboard-carmabox/detaljer)
# Reasoning, plan-chart, elprisgraf, solprognos, logg, systemhälsa.
# ══════════════════════════════════════════════════════════════════════════════


async def test_tab2_loads_and_shows_decision_content(tab_varfor):
    """Tab 2 ska ladda och visa beslutsinnehåll.

    Primärt syfte: visa VARFÖR CARMA Box tog ett visst beslut.
    """
    body = await _shadow_text(tab_varfor)
    assert len(body) > 200, "Tab 2 verkar tom"
    has_decision = any(
        x in body.lower() for x in ["beslut", "åtgärd", "action", "standby", "ladda", "discharge"]
    )
    assert has_decision, "Tab 2 visar ingen beslutsinformation — besluts-markdown laddades ej"


async def test_tab2_decision_not_ingen_data(tab_varfor):
    """Beslutsreasoning på Tab 2 får inte visa 'Ingen data'.

    'Ingen data' indikerar att sensor.carma_box_decision_reason
    är offline eller har slutat producera data.
    """
    body = await _shadow_text(tab_varfor)
    assert "ingen data" not in body.lower(), "Tab 2 besluts-reasoning visar 'Ingen data'"


async def test_tab2_nordpool_price_visible(tab_varfor):
    """Nordpool-priset ska visas på Tab 2.

    Priset används för att förklara laddnings-/urladdningsbeslut.
    """
    body = await _shadow_text(tab_varfor)
    assert (
        "öre" in body.lower() or "nordpool" in body.lower()
    ), "Tab 2 Nordpool-prissektion saknar öre/nordpool-text"


async def test_tab2_plan_chart_or_plan_text_present(tab_varfor):
    """Plan-chart (apexcharts) eller plan-text ska finnas på Tab 2.

    apexcharts-card renderar 24h SoC+EV-prognos interaktivt.
    Finns varken chart eller plan-text har kortet kraschat.
    """
    page = tab_varfor
    body_el = await page.query_selector("body")
    has_apex = await page.evaluate(_HAS_TAG_JS, [body_el, "apexcharts-card"])
    body = await _shadow_text(page)
    has_plan_text = "plan" in body.lower() or "soc" in body.lower()
    assert has_apex or has_plan_text, "Tab 2 saknar plan-chart (apexcharts-card) och plan-text"


async def test_tab2_effekt_live_has_values(tab_varfor):
    """Effekt live-sektion ska visa watt/kW-värden på Tab 2.

    Visar realtids-effekt för sol, batteri, nät och förbrukning.
    """
    body = await _shadow_text(tab_varfor)
    assert (
        "effekt" in body.lower() or " w" in body.lower()
    ), "Tab 2 effekt live-sektion saknar watt-värden"


async def test_tab2_health_section_renders(tab_varfor):
    """Systemhälsa-sektionen ska visas på Tab 2.

    Hälso-kortet visar om inverters, Easee, Nordpool och Solcast är online.
    Kritisk för att diagnostisera CARMA Box-problem.
    """
    body = await _shadow_text(tab_varfor)
    has_health = any(x in body.lower() for x in ["hälsa", "inverter", "kontor", "healthy"])
    assert has_health, "Tab 2 systemhälsa-sektion inte synlig"


# ══════════════════════════════════════════════════════════════════════════════
# GROUP D — Tab 3: Inställningar  (/dashboard-carmabox/installningar)
# v6-helpers: input_boolean, input_number, input_select, GoodWe EMS.
# ══════════════════════════════════════════════════════════════════════════════


async def test_tab3_loads_settings_content(tab_installningar):
    """Tab 3 ska ladda inställningssidan med synligt innehåll."""
    body = await _shadow_text(tab_installningar)
    has_settings = any(
        x in body.lower() for x in ["inställningar", "styrning", "enabled", "kw", "öre", "soc"]
    )
    assert has_settings, "Tab 3 inställningssida laddades ej"


async def test_tab3_v6_enabled_toggle_present(tab_installningar):
    """v6_enabled-toggle (CARMA styrning på/av) ska finnas på Tab 3.

    Master-switch: om den inte renderar kan operatören inte
    aktivera/avaktivera CARMA Box-styrning.
    """
    body = await _shadow_text(tab_installningar)
    has_toggle = any(x in body.lower() for x in ["enabled", "styrning", "aktivera", "v6"])
    assert has_toggle, "Tab 3 v6_enabled-toggle syns inte"


async def test_tab3_numeric_input_values_visible(tab_installningar):
    """Numeriska input-värden (kW, öre, SoC) ska synas på Tab 3.

    input_number-entities renderar med aktuellt värde.
    Inga siffror = entities offline eller templates trasiga.
    """
    body = await _shadow_text(tab_installningar)
    has_numbers = any(x in body.lower() for x in ["kw", "kwh", "öre", "soc", "%"])
    assert has_numbers, "Tab 3 visar inga numeriska inställningsvärden"


async def test_tab3_ev_section_renders(tab_installningar):
    """EV-inställningar ska visas på Tab 3.

    EV-sektionen: manuell 6A-knapp, EV-mode select, dagligt SoC-mål.
    Kritisk för att konfigurera säker EV-laddning (max 10A).
    """
    body = await _shadow_text(tab_installningar)
    has_ev = any(x in body.lower() for x in ["ev", "elbil", "easee", "laddning"])
    assert has_ev, "Tab 3 EV-inställnings-sektion saknas"


# ══════════════════════════════════════════════════════════════════════════════
# GROUP E — Tab 4: Regler  (/dashboard-carmabox/regler)
# rule_flow, EV-regler, batteri-regler, safety-sektion.
# ══════════════════════════════════════════════════════════════════════════════


async def test_tab4_loads_regler_content(tab_regler):
    """Tab 4 ska ladda och visa regelinnehåll."""
    body = await _shadow_text(tab_regler)
    assert len(body) > 100, "Tab 4 verkar tom"
    has_rules = any(x in body.lower() for x in ["regel", "rule", "regler", "batteri", "ev"])
    assert has_rules, "Tab 4 visar inget regelinnehåll"


async def test_tab4_no_ingen_data(tab_regler):
    """Tab 4 ska inte visa 'Ingen data'.

    'Ingen data' på regler-fliken indikerar att rule_flow-sensorn
    är offline eller har en template-bugg.
    """
    body = await _shadow_text(tab_regler)
    assert (
        "ingen data" not in body.lower()
    ), "Tab 4 visar 'Ingen data' — rule_flow-sensor troligen offline"


async def test_tab4_no_critical_load_errors(tab_regler):
    """Tab 4 ska inte ha 'can't load'-felmeddelanden.

    'Can't load ...' i HA = custom card som inte registrerats korrekt.
    """
    body = await _shadow_text(tab_regler)
    for err in ("can't load", "failed to load"):
        assert err not in body.lower(), f"Tab 4 har laddningsfel: '{err}'"


# ══════════════════════════════════════════════════════════════════════════════
# GROUP F — Tab 5: Vägg  (/dashboard-carmabox/wall)
# Kiosk-vy för Samsung Tab 1280x800: hero, SoC, bästa tiderna, dagssummering.
# ══════════════════════════════════════════════════════════════════════════════


async def test_tab5_loads_wall_content(tab_vagg):
    """Tab 5 vägg-display ska ladda med synligt innehåll."""
    body = await _shadow_text(tab_vagg)
    assert len(body.strip()) > 50, "Tab 5 vägg-display verkar tom"


async def test_tab5_soc_percentage_visible(tab_vagg):
    """SoC-värde (%) ska visas på vägg-displayen.

    Wall display visar alltid batteriets laddningsstatus
    som primär statusinformation.
    """
    body = await _shadow_text(tab_vagg)
    assert "%" in body, "Tab 5 vägg-display saknar %-värde — SoC-kort renderades ej"


async def test_tab5_hero_not_ingen_data(tab_vagg):
    """Hero-kort på Tab 5 ska inte visa 'Ingen data'.

    Hero-kortet är det första operatören ser på vägg-displayen.
    'Ingen data' = sensor.carma_box_decision_reason offline.
    """
    body = await _shadow_text(tab_vagg)
    assert "ingen data" not in body.lower(), "Tab 5 hero-kort visar 'Ingen data'"


async def test_tab5_dagssummering_has_kr_or_kwh(tab_vagg):
    """Dagssummering på Tab 5 ska visa kr eller kWh-värden.

    Visar energiförbrukning och besparingar för dagen.
    """
    body = await _shadow_text(tab_vagg)
    has_summary = any(x in body.lower() for x in ["kr", "kwh", "dag", "besparing"])
    assert has_summary, "Tab 5 dagssummering saknar kr/kWh-värden"


# ══════════════════════════════════════════════════════════════════════════════
# GROUP G — Negativa tester
# Verifierar att systemet hanterar ogiltiga inputs korrekt.
# ══════════════════════════════════════════════════════════════════════════════


def test_invalid_dashboard_path_does_not_return_500():
    """En ogiltig dashboard-path ska ge 200 (SPA-shell) eller 404, aldrig 500.

    HA är en Single Page Application: okända paths ger 200 med HTML-shell
    som sedan hanteras client-side. HTTP 500 = serverfel.
    """
    r = requests.get(
        f"{HA_URL}/dashboard-carmabox/denna-flik-finns-inte-xyz",
        timeout=10,
    )
    assert r.status_code != 500, f"Ogiltig dashboard-path orsakade HTTP 500: {r.status_code}"
