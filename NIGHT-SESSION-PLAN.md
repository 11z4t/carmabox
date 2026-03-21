# Nattarbete 21-22 mars 2026 — Plan

## Mål: Närma oss 10/10 i ALLA områden

### Områden att bedöma (ärlig self-assessment imorgon)

| Område | Nuvarande | Mål | Fokus inatt |
|--------|-----------|-----|-------------|
| Executor-regler | 7/10 | 9/10 | Regelordning, edge cases |
| Transparens | 5/10 | 8/10 | Regelflödes-sensor, UX |
| Säkerhet | 7/10 | 9/10 | Watchdog-tester, input validation |
| Besparingsberäkning | 4/10 | 7/10 | Validera mot verklighet |
| Tester | 7/10 | 9/10 | Watchdog, insight, edge cases |
| Kodkvalitet | 8/10 | 9/10 | Ruff, mypy, inga hardcoded |
| EV-styrning | 6/10 | 8/10 | Nattplan, PV surplus |
| Miner-styrning | 5/10 | 7/10 | Auto-detect, tester |
| Insiktsmail | 3/10 | 7/10 | Riktig data, analys |
| Self-healing | 6/10 | 8/10 | Watchdog W1-W5 tester |
| Deploy-pipeline | 5/10 | 7/10 | HACS vs manual, pycache |

### Arbetsordning inatt

1. **Besparingsvalidering** — granska record_peak, record_discharge, beräkna om 328 kr stämmer
2. **Regelflödes-sensor** — visuell representation av regelkedjan
3. **Watchdog-tester** — W1-W5 unit tests
4. **Insight-sensor robusthet** — edge cases (0 data, None, unavailable)
5. **Kodkvalitet** — fullständig QC av coordinator.py
6. **Deploy-fix** — HACS vs manual problem (v1.1.0 vs v3.0.0)
7. **EV edge cases** — cable disconnect under laddning, SoC sensor unavailable
8. **Jira-tickets** — för varje fynd
