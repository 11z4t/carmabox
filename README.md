# CARMA Box

**C**onnected **A**utomated **R**esource **M**anagement **A**dvisor

Energy optimizer för hemmabruk — batterier, solceller, EV-laddning, vitvaror.
Minimerar effekttoppar och elkostnader automatiskt.

## Arkitektur

```
Lokal (varje box)          Central Hub
┌──────────────┐          ┌──────────────┐
│ Optimizer    │──sync──▶│ PostgreSQL    │
│ Integrations │          │ ML Training   │
│ SQLite       │◀─config─│ Claude CLI    │
│ FastAPI      │          │ Reports       │
└──────────────┘          └──────────────┘
```

## Mål (prioritetsordning)

1. **Minimera effekttoppar** — Ellevio 80kr/kW × medel(topp-3)
2. **Minimera elkostnad** — Nordpool prisoptimering
3. **EV ≥75% SoC kl 06:00** — varje morgon
4. **EV 100% inom 7 dygn** — smart toppning vid sol/billig natt
5. **Platt timmedel-kurva** — sprida ut, aldrig toppa

## Tech

- Python 3.12 + FastAPI
- SQLite (lokal) + PostgreSQL (central)
- Docker multi-arch (amd64 + arm64/RPi 5)
- Claude Code CLI för AI-insikter
- reportlab PDF + Jinja2 mail

## Integrationer

| Kategori | Stödda |
|----------|--------|
| Invertrar | GoodWe, (Huawei, SolarEdge planned) |
| EV-laddare | Easee, (Zaptec, Wallbox planned) |
| Elmätare | HomeWizard P1, HA-entity |
| Priser | Nordpool, Tibber, ENTSO-E |
| Solprognos | Solcast, Forecast.Solar |
| Hemautomation | Home Assistant (REST + WS) |

## License

Proprietary — 4recon AB
