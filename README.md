# CARMA Box

**C**onnected **A**utomated **R**esource **M**anagement **A**dvisor

Energy optimizer för hemmabruk — batterier, solceller, EV-laddning, vitvaror.
Minimerar effekttoppar och elkostnader automatiskt.

## Lagar (prioritetsordning)

Se [MANIFEST.md](MANIFEST.md) för fullständig specifikation.

1. **Ellevio timmedel ALDRIG över tak** (2 kW viktat)
2. **Batterier ska användas aktivt** (idle = bortkastat)
3. **EV ≥ 75% SoC kl 06:00** varje dag
4. **Minimera export** — maximera egenkonsumtion
5. **Laddning vid lägst elpris** och effektmedel
6. **Urladdning: effektmedel först**, elkostnad sedan
7. **Sol- och säsongsmedvetenhet**

## Arkitektur

```
Varje 30s-cykel:

┌─────────────────────────────────────┐
│ Layer 0: GRID GUARD                 │ ← ALDRIG överskrid Ellevio
│ Invarianter: INV-1 till INV-5       │
├─────────────────────────────────────┤
│ Layer 1: STATE COLLECTOR            │
├─────────────────────────────────────┤
│ Layer 2: PLANNER (var 5 min)        │ ← Sol/pris/temperatur-medveten
├─────────────────────────────────────┤
│ Layer 3: PLAN EXECUTOR              │ ← Planen styr
├─────────────────────────────────────┤
│ Layer 4: BATTERY BALANCER           │ ← Proportionell, cold-lock aware
├─────────────────────────────────────┤
│ Layer 5: SURPLUS CHAIN              │ ← Knapsack: 0W export
├─────────────────────────────────────┤
│ Layer 6: WATCHDOG                   │
└─────────────────────────────────────┘
```

## Core-moduler

| Modul | Ansvar | Tester |
|-------|--------|--------|
| `core/grid_guard.py` | LAG 1 enforcement + INV-1 till INV-5 | 34 |
| `core/battery_balancer.py` | Proportionell urladdning/laddning | 22 |
| `core/plan_executor.py` | Plan → commands, 3-fas EV, replan | 27 |
| `core/surplus_chain.py` | Knapsack allokering, hysteres | 16 |
| `core/planner.py` | Sol/temp-medveten planering | 12 |

## Integrationer

| Kategori | Stödda |
|----------|--------|
| Invertrar | GoodWe |
| EV-laddare | Easee |
| Elmätare | HomeWizard P1 |
| Priser | Nordpool, Tibber |
| Solprognos | Solcast, Forecast.Solar |
| Väder | Tempest WeatherFlow (MQTT) |
| Hemautomation | Home Assistant (HACS) |

## Kör lokalt

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Testsvit (samtliga tester) — grönt på main (2026-08-01): 3882 passed
.venv/bin/pytest tests/ -q

# Ett specifikt domänpaket, t.ex. bat_balancer
.venv/bin/pytest tests/bat_balancer/ -q

# Lint + typkontroll (samma verktyg som QC-MANIFEST-verifieringen).
# OBS (2026-08-01): INTE gröna på main just nu — 11 ruff-fel, 53 mypy-fel,
# huvudsakligen i ev_balancer/carmabox. Konsolideringsarbete pågår på egna
# grenar (se steg1/steg1b-branchar) — kommandona nedan visar HUR man kör
# verktygen, inte att de passerar idag.
.venv/bin/ruff check custom_components/ tests/
.venv/bin/mypy custom_components/ --strict
```

Kräver Python 3.12 (se `pyproject.toml`). Ingen levande Home Assistant-instans krävs för
testsviten — `hass` mockas i varje testfixtur (`unittest.mock.MagicMock`).

## Dokumentation

| Fil | Innehåll |
|-----|----------|
| [MANIFEST.md](MANIFEST.md) | Lagar, invarianter, arkitektur, parametrar |
| [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) | Implementationsplan, milstolpar |
| [docs/GRID-GUARD-DESIGN.md](docs/GRID-GUARD-DESIGN.md) | Grid Guard detaljdesign |

## License

Proprietary — 4recon AB
