# CLAUDE.md — CARMA Box

**Connected Automated Resource Management Advisor**

## Projekt

| Egenskap | Värde |
|----------|-------|
| Typ | Home Assistant Custom Component (HACS) |
| Domän | `carmabox` |
| Python | >=3.12 |
| HA min | 2024.4.0 |
| Repo | github.com/11z4t/carmabox + gitea:4recon/carmabox |
| Bolag | 4Reconciliation AB |

## Arkitektur

```
custom_components/carmabox/
├── coordinator.py       — Huvudmodul: state machine, EMS, Modbus (6500 LOC)
├── coordinator_bridge.py — Bridge till core-moduler
├── sensor.py            — 28 sensorer + dynamiska appliance-sensorer
├── config_flow.py       — HA config flow (wizard)
├── adapters/            — GoodWe, Easee, Solcast, Nordpool, Tempest
├── core/                — grid_guard, battery_balancer, planner, surplus_chain, etc.
└── optimizer/           — scheduler, predictor, savings, safety_guard, etc.
```

## Lagar (MANIFEST.md)

1. **Ellevio timmedelvärde** ≤ tak (2.0 kW viktat, natt×0.5)
2. **Batterier ska användas aktivt** (idle max 4h)
3. **EV ≥ target SoC** varje morgon (75%)
4. **Nätimport minimeras** — PV+batteri först
5. **Säkerhet** — temperatur, SoC-golv, crosscharge-prevention
6. **Ellevio-kostnad minimeras** — peak shaving top-3
7. **Besparingar rapporteras** korrekt

## Testning

```bash
python3 -m pytest tests/unit/ -x -q                    # Snabbtest
python3 -m pytest tests/unit/ --cov=custom_components/carmabox  # Med täckning
python3 -m ruff check custom_components/ tests/         # Lint
```

- **Aldrig** kör e2e-tester utan live HA
- Integration-tester exkluderas i default pytest-config

## Deploy

All deploy via git: commit → push → HACS/deploy-pipeline.
**ALDRIG** direkt deploy (docker cp, scp, etc.)

## Kända regler

- `coordinator.py` är monolitisk (6500 LOC) — refactoring pågår via core/
- `ems_power_limit` MÅSTE vara 0 om inte aktiv grid-laddning
- GoodWe `auto` mode = FÖRBJUDET, `discharge_pv` = rätt discharge-mode
- `initial:` på input_select helpers = FÖRBJUDET (HA restart-bug)
- `input_datetime.set_datetime` → ALLTID `timestamp:` format (ALDRIG `datetime:`)

## CI

GitHub Actions: `.github/workflows/ci.yml` (push main/develop, PR main)
- quality (ruff check + format + py_compile + no .pyc)
- tests (pytest unit/scenarios/regression + coverage gate 90%, Codecov upload)
- manifest-check (MANIFEST.md not empty + hacs.json valid JSON)

Pre-commit: `.githooks/pre-commit` (ruff + format + pyc cleanup + unit smoke)
Coverage gate: `pyproject.toml` `[tool.coverage.report]` fail_under=90
