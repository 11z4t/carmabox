# CARMA Box — Regelordning (Executor)

## Principer
- **Girig på sol** — ALDRIG exportera om lokal användning finns
- **Självläkande** — watchdog korrigerar fel beslut inom 30s
- **Transparent** — varje beslut har reasoning chain
- **Prioritetskedja PV-överskott:** Batteri → EV → Miner → Export

## Executor-regler (prioritetsordning)

Körs var 30:e sekund. Första matchande regel vinner.

### Säkerhetsgates (alltid först)
| Gate | Villkor | Åtgärd |
|------|---------|--------|
| G1 Heartbeat | Coordinator ej uppdaterad >120s | Block alla kommandon |
| G2 Rate limit | >60 mode-ändringar/timme | Block alla kommandon |
| G3 Crosscharge | Batteri 1 laddar + batteri 2 laddar ur | Tvinga standby |

### Beslutsegler
| Regel | Villkor | Åtgärd | Rationale |
|-------|---------|--------|-----------|
| **R0.5** | PV > 500W + batteri ej fullt | charge_pv | Fånga all solenergi i batteri |
| **R1** | Grid < 0 (export) | charge_pv (om plats), annars standby | Aldrig exportera i onödan |
| **R1.5** | Pris < 15 öre + SoC < 90% | charge_pv (nätladdning) | Fyll batteri billigt |
| **R2** | Grid viktat > target | discharge | Sänk Ellevio-toppmedel |
| **R3** | Grid viktat < target | standby (idle) | Batteriet vilar, grid klarar sig |

### Surplus-prioritet (efter batteribeslutet)
| Steg | Villkor | Åtgärd |
|------|---------|--------|
| S1 EV | Grid < 0 (export) + kabel inkopplad + SoC < target | Starta EV 6A, max = export/230V |
| S2 Miner | Grid < 0 (export) > 200W efter EV | Miner ON |
| S3 Miner OFF | Grid > 500W (import) | Miner OFF |

### EV-regler (dagtid)
| Regel | Villkor | Åtgärd |
|-------|---------|--------|
| EV-1 | Kabel ej ansluten | Stäng av EV |
| EV-2 | SoC >= target | Stäng av EV |
| EV-3 | Natt + planerad laddning | Ladda enligt prisplan |
| EV-4 | Dag + export (grid < 0) | Ladda med export-effekt, max 10A |
| EV-X | Dag + grid >= 0 + EV laddar | STOPP (aldrig nätimport för EV dag) |

### Watchdog (körs EFTER beslut)
| Check | Detekterar | Korrigering |
|-------|-----------|-------------|
| W1 | Exporterar >500W + batteri ej fullt + ej laddning | → charge_pv |
| W2 | Grid > target + batteri har kapacitet + ej urladdning | → discharge |
| W3 | 100% SoC + grid > target + standby | → discharge |
| W4 | EV laddar + grid importerar (dag) | → stoppa EV |
| W5 | Högt pris >80 öre + batteri >50% + idle | → logg varning |

## Konfigurerbara parametrar
| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| target_weighted_kw | 2.0 | Ellevio viktat effektmål (kW) |
| min_soc | 15% | Minimum SoC (GoodWe cutoff 10% + 5% marginal) |
| night_weight | 0.5 | Ellevio natt-vikt (22-06) |
| grid_charge_price_threshold | 15 öre | Max pris för nätladdning |
| grid_charge_max_soc | 90% | Max SoC vid nätladdning |
| ev_night_target_soc | 75% | EV natt-mål |
| ev_max_amps | 10A | Max EV ström (säkringsskydd) |
