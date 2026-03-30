# INVENTERING.md - CARMA Box v5.0.0

**Datum:** 2026-03-30
**Ärende:** PLAT-1081 (uppdaterad från PLAT-1080)
**Version:** 4.8.1 (manifest) / v5.0.0 (logisk)
**Repo:** github.com/11z4t/carmabox + gitea:4recon/carmabox

---

## 1. Projektöversikt

| Egenskap | Värde |
|----------|-------|
| Typ | Home Assistant Custom Component (HACS) |
| Domän | `carmabox` |
| Python | >=3.12 |
| HA minimum | 2024.4.0 |
| IoT-klass | local_push |
| Kodägare | @4recon |

**Syfte:** Intelligent energiförvaltning — batteristyrning, solprognos, elprisoptimering, EV-laddning, peak shaving, Ellevio-effekttak.

**Referensdokument bekräftade:**
- MANIFEST.md: Läst och bekräftad (7 lagar, 5 invarianter, parameterstyrd arkitektur)
- IMPLEMENTATION-PLAN.md: Läst och bekräftad (Fas 0-9, Grid Guard→ML)

---

## 2. Kodstruktur

### Produktionskod: 53 Python-filer, 23,831 LOC

```
custom_components/carmabox/
├── __init__.py             (128 LOC, 68 stmts)   — HA integration setup
├── appliances.py           (169 LOC, 80 stmts)   — Appliance management
├── config_flow.py         (1077 LOC, 390 stmts)  — HA config flow UI
├── const.py                (290 LOC, 133 stmts)  — Constants & parameters
├── coordinator.py         (6518 LOC, 3047 stmts) — HUVUDMODUL: state machine, EMS, Modbus
├── coordinator_bridge.py   (980 LOC, 436 stmts)  — Bridge till core-moduler
├── diagnostics.py           (94 LOC, 27 stmts)   — HA diagnostics
├── hub.py                  (563 LOC, 287 stmts)  — Hub communication
├── notifications.py        (246 LOC, 96 stmts)   — Push notifications
├── repairs.py              (132 LOC, 52 stmts)   — HA repairs integration
├── sensor.py              (1076 LOC, 300 stmts)  — Sensor platform
├── adapters/               (5 filer, 901 LOC)
│   ├── __init__.py         (161 LOC, 83 stmts)   — Adapter base
│   ├── easee.py            (239 LOC, 121 stmts)  — Easee EV charger
│   ├── goodwe.py           (227 LOC, 88 stmts)   — GoodWe inverter
│   ├── nordpool.py          (96 LOC, 57 stmts)   — Nordpool elpriser
│   ├── solcast.py          (103 LOC, 57 stmts)   — Solcast PV prognos
│   └── tempest.py           (75 LOC, 32 stmts)   — WeatherFlow Tempest
├── core/                  (11 filer, 4659 LOC)
│   ├── battery_balancer.py  (332 LOC, 106 stmts) — Proportionell urladdning
│   ├── coordinator_v2.py    (475 LOC, 163 stmts) — V2 coordinator logic
│   ├── grid_guard.py        (461 LOC, 206 stmts) — Ellevio-tak övervakning
│   ├── law_guardian.py      (477 LOC, 209 stmts) — Regelefterlevnad
│   ├── ml_predictor.py      (270 LOC, 126 stmts) — ML-prediktion
│   ├── plan_executor.py     (416 LOC, 144 stmts) — Plan-driven executor
│   ├── planner.py           (625 LOC, 200 stmts) — Energiplanering
│   ├── reports.py           (632 LOC, 46 stmts)  — Rapportgenerering
│   ├── resilience.py        (224 LOC, 122 stmts) — Felhantering & recovery
│   ├── startup.py           (110 LOC, 25 stmts)  — Startup-sekvens
│   └── surplus_chain.py     (637 LOC, 210 stmts) — Knapsack-allokering
└── optimizer/             (16 filer, 5701 LOC)
    ├── battery_health.py    (347 LOC, 130 stmts) — Batterihälsa
    ├── consumption.py       (140 LOC, 57 stmts)  — Förbrukningsprofil
    ├── ev_dynamic.py        (101 LOC, 21 stmts)  — EV dynamisk
    ├── ev_solar.py           (96 LOC, 17 stmts)  — EV soldriven
    ├── ev_strategy.py       (289 LOC, 110 stmts) — EV-strategi
    ├── evening_optimizer.py (291 LOC, 105 stmts) — Kvällsoptimering
    ├── grid_logic.py        (135 LOC, 54 stmts)  — Nätlogik
    ├── hourly_ledger.py     (575 LOC, 227 stmts) — Timreskontra
    ├── models.py            (394 LOC, 257 stmts) — Datamodeller
    ├── multiday_planner.py  (363 LOC, 142 stmts) — Flerdagsplanering
    ├── plan_scoring.py      (274 LOC, 105 stmts) — Planpoängsättning
    ├── planner.py           (232 LOC, 99 stmts)  — Planeringsmotor
    ├── predictor.py         (459 LOC, 227 stmts) — Prediktionsmodul
    ├── price_patterns.py    (247 LOC, 91 stmts)  — Prismönster
    ├── pv_correction.py     (259 LOC, 121 stmts) — PV-korrigering
    ├── report.py            (192 LOC, 90 stmts)  — Optimeringsrapport
    ├── roi.py               (284 LOC, 97 stmts)  — ROI-beräkning
    ├── safety_guard.py      (365 LOC, 171 stmts) — Säkerhetsvakt
    ├── savings.py           (456 LOC, 116 stmts) — Besparingsberäkning
    ├── scheduler.py        (1296 LOC, 547 stmts) — Schemaläggare
    └── weather_learning.py  (202 LOC, 96 stmts)  — Väderlärande
```

### Tester: ~100 Python-filer

```
tests/
├── unit/       (46 filer)  — 1294 tester, alla PASS
├── integration/ (2 filer)  — config_flow, init
├── e2e/        (1 fil)     — kräver live HA
└── fixtures/   (scenarier + adaptrar)
```

---

## 3. Testtäckning (nuläge 2026-03-30)

| Mått | Värde |
|------|-------|
| Total täckning | **71%** |
| Statements | 9,791 |
| Missade | 2,860 |
| Tester | 1,294 |
| Status | Alla PASS (9 warnings) |

### Moduler under 80% täckning

| Modul | Täckning | Stmts | Miss | Kommentar |
|-------|----------|-------|------|-----------|
| config_flow.py | 0% | 390 | 390 | Helt otestad (integrationstester separat) |
| __init__.py | 32% | 68 | 46 | Setup-logik otestad |
| coordinator.py | 44% | 3047 | 1692 | STÖRST modul, hälften otestad |
| repairs.py | 50% | 52 | 26 | Halvtestad |
| sensor.py | 52% | 300 | 145 | Sensor-registrering otestad |
| coordinator_bridge.py | 61% | 436 | 172 | Bridge-logik delvis otestad |
| hub.py | 65% | 287 | 100 | Hub-kommunikation delvis otestad |

### Moduler ≥90% täckning (30+ st)

**core/ (alla ≥90%):**
battery_balancer (97%), coordinator_v2 (99%), grid_guard (97%), law_guardian (99%), ml_predictor (95%), plan_executor (99%), planner (96%), reports (98%), resilience (91%), startup (100%), surplus_chain (90%)

**optimizer/ (flertalet ≥90%):**
consumption (100%), ev_dynamic (100%), ev_solar (100%), evening_optimizer (100%), grid_logic (100%), hourly_ledger (100%), models (99%), multiday_planner (99%), plan_scoring (96%), planner (100%), report (100%), savings (100%), price_patterns (97%), roi (93%), safety_guard (95%), weather_learning (92%), pv_correction (89%), predictor (89%), battery_health (87%), ev_strategy (86%), scheduler (83%)

**adapters/ (alla ≥91%):**
nordpool (100%), solcast (100%), tempest (100%), __init__ (98%), easee (92%), goodwe (91%)

**Övrigt:**
appliances (100%), const (100%), diagnostics (100%), notifications (89%)

---

## 4. Kodkvalitet

| Verktyg | Status |
|---------|--------|
| Ruff | **0 fel** (E, F, W, I, N, UP, B, A, SIM) |
| MyPy | Körs med `--ignore-missing-imports` |
| Python | 3.12+ |
| Linjelängd | 100 tecken |

---

## 5. Säkerhet — Git-historik scan

| Kontroll | Status |
|----------|--------|
| Hardkodade secrets | **Inga hittade** |
| Lösenord i kod | Nej — konfigurationsbaserat |
| API-nycklar | Nej — HA config flow |
| Token-hantering | Via HA:s credential-system |
| `git log -p` scan | **PASS** — inga credentials i historik |

Notering: `git log -p --all | grep -i "api_key|token|password|secret"` hittade enbart:
- Variabelnamn (`mqtt_token`, `api_key` som parametrar)
- SUPERVISOR_TOKEN-åtkomst via HA:s interna env (korrekt)
- HMAC-verifieringslogik (korrekt)
- Inga faktiska credentials exponerade.

---

## 6. CI/CD

| Egenskap | Status |
|----------|--------|
| GitHub Actions | `.github/workflows/ci.yml` — lint + test + HACS |
| Pre-commit hooks | `.githooks/` finns |
| Test coverage gate | 65% (fail under) |
| Automatisk deploy | Via HACS/deploy-pipeline |

---

## 7. Kända problem & risker

| # | Risk | Prioritet | Kommentar |
|---|------|-----------|-----------|
| R1 | `coordinator.py` = 6518 LOC, 3047 stmts | HÖG | Monolitisk, svår att testa (44% coverage) |
| R2 | `config_flow.py` = 0% coverage | MEDEL | 390 stmts helt otestade |
| R3 | `sensor.py` = 52% coverage | MEDEL | Sensor-registrering & state-hantering otestad |
| R4 | `coordinator_bridge.py` = 61% coverage | MEDEL | Kritisk bridge-logik delvis otestad |
| R5 | `hub.py` = 65% coverage | MEDEL | Hub-kommunikation otestad |
| R6 | `repairs.py` = 50% coverage | LÅG | Liten modul |
| R7 | 9 pytest warnings (unawaited coroutines) | LÅG | Mock-relaterade, ej funktionella |

---

## 8. Beroenden

- Home Assistant >=2024.4.0
- Python >=3.12
- Inga externa pip-requirements (allt via HA)
- Adaptrar: GoodWe, Easee, Solcast, Nordpool, Tempest (alla HA-integrationer)

---

## 9. Gap-analys: nuläge → produktionsredo

| Gap | Prioritet | Nuläge | Mål | Effort | Fas |
|-----|-----------|--------|-----|--------|-----|
| coordinator.py testtäckning | HÖG | 44% (1692 miss) | ≥80% | Stor | 1 |
| config_flow.py testtäckning | MEDEL | 0% (390 miss) | ≥70% | Medel | 2 |
| sensor.py testtäckning | MEDEL | 52% (145 miss) | ≥80% | Medel | 2 |
| coordinator_bridge.py testtäckning | MEDEL | 61% (172 miss) | ≥80% | Medel | 2 |
| hub.py testtäckning | MEDEL | 65% (100 miss) | ≥80% | Liten | 3 |
| Total coverage 71%→90% | HÖG | 71% | 90% | Stor | 1-3 |
| coordinator.py refactoring | LÅG | 6518 LOC monolith | <2000 LOC | Stor | Framtida |
| Monitoring/observabilitet | LÅG | Minimal | Strukturerad | Medel | 4+ |

---

## 10. Sammanfattning

| Mått | Värde |
|------|-------|
| Produktionsfiler | 53 st (.py) |
| Total produktions-LOC | 23,831 |
| Tester | 1,294 PASS |
| Testtäckning | 71% |
| Ruff-fel | 0 |
| Säkerhetsrisker | 0 (inga credentials i git) |
| Moduler <80% coverage | 7 st |
| Moduler ≥90% coverage | 30+ st |
| Störst modul | coordinator.py (6518 LOC, 44% coverage) |
| CI/CD | GitHub Actions aktiv |
