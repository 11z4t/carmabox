# AUDIT_REPORT.md — CARMA Box v5.0.0

**Ärende:** PLAT-1080
**Datum:** 2026-03-30
**Auditor:** VM 900 (Orkestrerare)

---

## Sammanfattning

Komplett code quality audit av carmabox v5.0.0 (241 commits, ~24K LOC produktionskod, ~16K LOC tester). Projektet är i gott skick med 0 ruff-fel, inga hardkodade secrets, och 1181 passerande tester.

---

## Fas 0: Inventering

**Leverans:** `INVENTERING.md`

| Mått | Värde |
|------|-------|
| Produktionskod | 51 Python-filer, ~24,000 LOC |
| Tester | 52 filer, ~16,500 LOC, 1181 tester |
| Commits | 241 st |
| Moduler | adapters (5), core (9), optimizer (16), root (11) |

---

## Fas 1: Säkerhet & Secrets (PLAT-1084)

**Status:** GODKÄNT
**Datum:** 2026-03-30
**Auditor:** VM 900 (Orkestrerare)

### 1.1 Credential-hantering

| Kontroll | Kommando | Resultat |
|----------|----------|----------|
| Hardkodade lösenord/tokens i kod | `grep -rn "token\|secret\|password\|api_key"` | **0 kritiska** — alla träffar är schema-namn i config flow eller parametervariabelnamn (api_key, mqtt_token), aldrig hårdkodade värden |
| API-nycklar i kod | `grep -rn 'http://.*:.*@\|https://.*:.*@'` | **0 träffar** |
| Credentials i git-historik | `git log --all -p \| grep '(api_key\|token\|password\|secret)='` | **0 träffar** |
| Credential-lagring | Manuell granskning | Alla credentials (Solcast, GoodWe, Hub API key, MQTT token) lagras via HA Config Flow (`config_entry.data`) — aldrig i kod |
| Credential-loggning | `grep -rn '_logger.*(token\|secret\|password\|key)'` | **0 träffar** — inga credentials loggas |
| eval()/exec() | `grep -rn 'eval(\|exec('` | **0 träffar** |

### 1.2 Input-validering

| Kontroll | Resultat |
|----------|----------|
| float()/int() på extern data | **31 konverteringar** — ALLA skyddade med `try/except` eller `contextlib.suppress(ValueError, TypeError)` |
| State pre-validering | Majoriteten kollar `state not in ("unknown", "unavailable", "")` före konvertering |
| Config-validering | `async_setup_entry()` + config flow använder voluptuous-schema |

### 1.3 Adapter-säkerhet

| Kontroll | Resultat |
|----------|----------|
| `hass.services.async_call` | **30 anrop** — ALLA använder `blocking=False` (default) eller explicit await utan `blocking=True`. **0 event loop-hangrisker** |
| Fire-and-forget | 3 anrop via `hass.async_create_task()` — avsiktliga, säkra |
| Exception-hantering | Majoriteten wrappade i `try/except` eller `contextlib.suppress` |
| Retry-logik | Adapters (GoodWe, Easee) har `_safe_call()` med retry + exponential backoff |
| HTTP-timeout | License check: `aiohttp.ClientTimeout(total=15)` — korrekt |
| HMAC-signering | Hub-kommunikation: HMAC-SHA256 med timestamp (±5 min) + nonce (replay-skydd) |

### 1.4 Dict-access på extern data

| Kontroll | Resultat |
|----------|----------|
| `.get()` med default | **Alla** state.attributes-access via `.get()` |
| Osäkra `["key"]`-access | **1 hittad** — `coordinator_v2.py:253`: `cmd["mode"]` på GridGuard-kommandon |

**Åtgärd (1 st):**
- `core/coordinator_v2.py:253`: Ändrat `cmd["mode"]` → `cmd.get("mode", "battery_standby")` — defensiv fallback vid saknad nyckel

### 1.5 Verifieringsbevis

```
$ grep -rn 'eval(\|exec(' custom_components/carmabox/ → 0 träffar
$ grep -rn 'http://.*:.*@' . → 0 träffar
$ git log --all -p | grep hardcoded creds → 0 träffar
$ python3 -m pytest tests/unit/ -x -q → 1294 passed
$ python3 -m ruff check custom_components/ tests/ → All checks passed!
```

---

## Fas 2: Kodkvalitet

**Status:** GODKÄNT

| Verktyg | Resultat |
|---------|----------|
| Ruff (E,F,W,I,N,UP,B,A,SIM) | **0 fel** |
| Python-version | 3.12+ (enforced i pyproject.toml) |
| Linjelängd | 100 tecken (enforced) |
| Magic numbers | Flyttade till const.py (PLAT-1049/1053) |
| Dead code | Inga identifierade oanvända moduler |

**Observation:** `coordinator.py` (6500 LOC) är monolitisk men refactoring pågår redan via `core/` moduler (battery_balancer, grid_guard, planner, etc.).

---

## Fas 3: Testtäckning

| Mått | Före | Efter |
|------|------|-------|
| Total täckning | 67.1% | 71.0% |
| Antal tester | 1,137 | 1,294 |
| Status | Alla PASS | Alla PASS |

### Nya tester skrivna

| Fil | Tester | Moduler som täcks |
|-----|--------|-------------------|
| `test_appliances.py` | 24 | appliances.py (0%→~90%) |
| `test_notifications.py` | 20 | notifications.py (25%→~85%) |
| `test_predictor.py` | 46 | optimizer/predictor.py (40%→~65%) |
| `test_hourly_ledger.py` | 67 | optimizer/hourly_ledger.py (52%→~80%) |

### Moduler ≥90% (23 st — oförändrat)
Alla core/ och optimizer/ moduler har stabil hög täckning.

### Kvarstående gaps (ej rimliga att höja utan live HA)
- `coordinator.py` (44%) — 3047 stmts, starkt HA-beroende
- `config_flow.py` (0%) — kräver HA config flow framework
- `sensor.py` (52%) — kräver HA sensor platform
- `__init__.py` (32%) — kräver HA integration setup

**Bedömning:** 90% total täckning kräver ~2300 nya covered statements, varav majoriteten (1692) sitter i coordinator.py som är starkt kopplad till HA runtime. Att skriva mockade tester för coordinator.py ger lågt förtroende — bättre att fokusera på e2e-tester mot live HA.

---

## Fas 4: CI/CD Pipeline

**Leverans:** `.github/workflows/ci.yml`

Pipeline-steg:
1. **lint** — ruff check + ruff format
2. **test** — pytest med coverage (fail-under 65%)
3. **hacs** — HACS-validering

Triggas på push till main och pull requests.

---

## Fas 5: Monitoring & Observabilitet

**Befintligt:**
- 28 sensorer med rika attribut (decision, rules, plan_status, savings)
- `sensor.carmabox_status` — systemhälsa-sensor
- `sensor.carmabox_breach_monitor_*` — peak breach-övervakning
- `sensor.carmabox_scheduler_*` — scheduleringsövervakning
- Slack-notiser för alla kritiska händelser (crosscharge, low SoC, safety blocks)
- Morning report sensor

**Bedömning:** God observabilitet — 28 sensorer täcker alla aspekter av systemet.

---

## Fas 6: Resiliens & Produktion

**Befintligt:**
- `core/resilience.py` — circuit breaker, _safe_call() för non-critical methods
- `core/startup.py` — startup-validering
- Crosscharge detection & prevention (guardian)
- Temperature guards (charge/discharge separata)
- Modbus lockup detection & recovery
- Mode keeper (korrigerar avvikelser var 30 min)
- Max mode changes per hour (30) — oscillations-skydd

**Kända historiska incidenter (åtgärdade):**
1. Grid-spike 10.6 kW (2026-03-26) — fixat
2. pyc-cache blockerar ny kod — fixat
3. predict_24h() → None fallback — fixat
4. solcast.power_now_kw AttributeError — fixat

---

## Fas 7: Dokumentation

**Leveranser:**
- `CLAUDE.md` — projektmanifest med arkitektur, regler, testning, deploy
- `docs/entity-ids.md` — komplett entity mapping (28 sensorer + 7 appliance + 3 input helpers + externa)
- `INVENTERING.md` — nulägesbild

---

## Fas 8: Slutvalidering

### Checklista

| Krav | Status |
|------|--------|
| INVENTERING.md | ✅ |
| AUDIT_REPORT.md | ✅ (detta dokument) |
| TEST_REPORT.md | ✅ |
| CLAUDE.md | ✅ |
| docs/entity-ids.md | ✅ |
| GitHub Actions CI | ✅ |
| Noll ruff-fel | ✅ |
| Alla tester PASS | ✅ (1294/1294) |

### Rekommendationer för framtiden

1. **coordinator.py refactoring** — bryt ut fler delar till core/ (prio: medel)
2. **e2e-tester** — utöka tests/e2e/ med kritiska scenarier (prio: medel)
3. **config_flow-tester** — kräver HA test framework (prio: låg)
4. **Manifest-version** — uppdatera 4.8.1 → 5.0.0 i manifest.json (prio: hög)
