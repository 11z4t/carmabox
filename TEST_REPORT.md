# TEST_REPORT.md — CARMA Box v5.0.0

**Ärende:** PLAT-1080
**Datum:** 2026-03-30
**Testmiljö:** Python 3.12.3, pytest 8.x, pytest-asyncio, pytest-cov

---

## Översikt

| Mått | Värde |
|------|-------|
| Totalt antal tester | 1,294 |
| Passerade | 1,294 |
| Misslyckade | 0 |
| Varningar | 9 (deprecation, ej kritiska) |
| Körtid | ~22s |
| Total täckning | 71.0% |

---

## Täckning per modul

### 100% täckning (13 moduler)

| Modul | Stmts |
|-------|-------|
| adapters/nordpool.py | 57 |
| adapters/solcast.py | 57 |
| adapters/tempest.py | 32 |
| const.py | 133 |
| core/__init__.py | 0 |
| core/startup.py | 25 |
| diagnostics.py | 27 |
| optimizer/__init__.py | 0 |
| optimizer/consumption.py | 57 |
| optimizer/ev_dynamic.py | 21 |
| optimizer/ev_solar.py | 17 |
| optimizer/evening_optimizer.py | 105 |
| optimizer/grid_logic.py | 54 |
| optimizer/planner.py | 99 |
| optimizer/report.py | 90 |
| optimizer/savings.py | 116 |

### ≥90% täckning (17 moduler)

| Modul | Täckning | Stmts | Miss |
|-------|----------|-------|------|
| core/coordinator_v2.py | 99% | 163 | 2 |
| core/law_guardian.py | 99% | 209 | 3 |
| core/plan_executor.py | 99% | 144 | 2 |
| optimizer/models.py | 99% | 257 | 1 |
| optimizer/multiday_planner.py | 99% | 142 | 1 |
| adapters/__init__.py | 98% | 83 | 2 |
| core/reports.py | 98% | 46 | 1 |
| core/battery_balancer.py | 97% | 106 | 3 |
| core/grid_guard.py | 97% | 206 | 6 |
| optimizer/price_patterns.py | 97% | 91 | 3 |
| core/planner.py | 97% | 200 | 7 |
| optimizer/plan_scoring.py | 96% | 105 | 4 |
| optimizer/safety_guard.py | 95% | 171 | 8 |
| core/ml_predictor.py | 95% | 126 | 6 |
| optimizer/roi.py | 93% | 97 | 7 |
| adapters/easee.py | 92% | 121 | 10 |
| optimizer/weather_learning.py | 92% | 96 | 8 |
| adapters/goodwe.py | 91% | 88 | 8 |
| core/resilience.py | 91% | 122 | 11 |
| core/surplus_chain.py | 90% | 210 | 21 |

### <90% täckning (11 moduler)

| Modul | Täckning | Stmts | Miss | Kommentar |
|-------|----------|-------|------|-----------|
| optimizer/pv_correction.py | 89% | 121 | 13 | Nära mål |
| optimizer/battery_health.py | 87% | 130 | 17 | Nära mål |
| optimizer/ev_strategy.py | 86% | 110 | 15 | |
| notifications.py | 85%* | 96 | ~14 | Nya tester (PLAT-1080) |
| appliances.py | 90%* | 80 | ~8 | Nya tester (PLAT-1080) |
| optimizer/scheduler.py | 83% | 547 | 94 | |
| hub.py | 65% | 287 | 100 | HA-beroende |
| coordinator_bridge.py | 61% | 436 | 172 | HA-beroende |
| sensor.py | 52% | 300 | 145 | HA platform |
| repairs.py | 50% | 52 | 26 | HA repairs |
| optimizer/hourly_ledger.py | 52% | 227 | 109 | |
| coordinator.py | 44% | 3,047 | 1,692 | Monolitisk, HA-beroende |
| optimizer/predictor.py | 40% | 227 | 137 | |
| __init__.py | 32% | 68 | 46 | HA integration setup |
| config_flow.py | 0% | 390 | 390 | HA config flow |

*Uppskattad efter PLAT-1080-tester

---

## Nya tester (PLAT-1080)

| Testfil | Antal | Modul |
|---------|-------|-------|
| test_appliances.py | 24 | appliances.py |
| test_notifications.py | 20 | notifications.py |
| test_predictor.py | 46 | optimizer/predictor.py |
| test_hourly_ledger.py | 67 | optimizer/hourly_ledger.py |
| **Totalt** | **157** | |

---

## Testtyper

| Typ | Antal | Plats |
|-----|-------|-------|
| Unit | 1,181 | tests/unit/ |
| Integration | ~10 | tests/integration/ (exkluderade default) |
| E2E | ~5 | tests/e2e/ (kräver live HA) |

---

## Kvarstående för 90%

För att nå 90% total täckning behövs ~2,100 fler covered statements:
- coordinator.py (~1,200 stmts) — kräver omfattande HA-mocking eller refactoring
- config_flow.py (~350 stmts) — kräver HA config flow test framework
- sensor.py (~100 stmts) — kräver HA sensor platform
- predictor.py (~100 stmts) — enhetsbart
- hourly_ledger.py (~100 stmts) — enhetsbart

**Rekommendation:** Fokusera på predictor.py och hourly_ledger.py (enhetsbart), resten kräver HA-specifik testinfrastruktur.
