# CARMA Box Redesign — Implementationsplan

**Princip:** Små iterativa steg. Varje steg är testbart, mätbart, och
har go/nogo-kriterier. Vi deployer ALDRIG utan att alla tester passerar.

**CARMA Box är avaktiverad tills Fas 1 är klar och verifierad.**

---

## FAS 0: Förberedelser (innan kodändring)

### Steg 0.1: Skapa testinfrastruktur
**Vad:** Testfiler + fixtures för alla nya komponenter
**Filer:** `tests/unit/test_grid_guard.py`, `test_proportional.py`, `test_surplus_chain.py`
**Go/nogo:** Filerna finns, pytest discover hittar dem, 0 tester (tomma)
**Mätbart:** `pytest --collect-only` visar nya testfiler

### Steg 0.2: Extrahera nuvarande `_execute()` beteende till tester
**Vad:** Skriv tester som dokumenterar NUVARANDE beteende (regression baseline)
**Filer:** `tests/unit/test_execute_baseline.py`
**Tester:**
- `test_pv_surplus_charges_battery` — R0.5
- `test_export_charges_battery` — R1
- `test_cheap_price_grid_charges` — R1.5
- `test_over_target_discharges` — R2
- `test_under_target_idles` — R3
**Go/nogo:** Alla 5 baseline-tester PASS med nuvarande kod
**Mätbart:** `pytest tests/unit/test_execute_baseline.py` = 5 passed

### Steg 0.3: Verifiera GoodWe EMS modes
**Vad:** Testa MANUELLT vilka EMS modes som fungerar för urladdning
**Test:**
- `discharge_battery` + `fast_charging=OFF` → mät faktisk urladdning
- `auto` + `ems_power_limit=X` → mät faktisk urladdning
- `peak_shaving` + `ems_power_limit=X` → mät faktisk urladdning
- `discharge_pv` + `fast_charging=OFF` → mät vid PV > 0 och PV = 0
**Go/nogo:** Minst 1 mode ger kontrollerbar, proportionell urladdning
**Mätbart:** Dokumentera faktisk W per mode i tabell
**Leverans:** `docs/goodwe-ems-modes-verified.md`

---

## FAS 1: Grid Guard (KRITISK — deployas först)

### Steg 1.1: Skapa `grid_guard.py` modul
**Vad:** Ny fil, fristående, ingen HA-dependency förutom state-objekt
**Fil:** `custom_components/carmabox/core/grid_guard.py`
**Funktioner:**
```python
def evaluate_grid(
    grid_import_w: float,
    projected_timmedel_kw: float,
    tak_kw: float,
    margin: float,
    ev_power_w: float,
    battery_available_kwh: float,
) -> GridGuardResult:
    """Returns: action (none/reduce_ev/pause_ev/increase_discharge), amount"""
```
**Go/nogo:** 7 unit tests PASS (se manifest 12.2)
**Mätbart:** `pytest tests/unit/test_grid_guard.py` = 7 passed, 100% coverage

### Steg 1.2: Unit tests för Grid Guard
**Tester:**
```
test_grid_under_tak               → action=none
test_grid_over_tak_ev_on          → action=pause_ev
test_grid_over_tak_ev_off         → action=increase_discharge
test_projected_timmedel_over      → action=reduce_ev (inte pausa)
test_short_spike_ok               → action=none (medel under)
test_long_spike_triggers          → action=pause_ev
test_sensor_unavailable_fallback  → uses last_known + margin
```
**Go/nogo:** 7/7 PASS
**Mätbart:** Coverage ≥ 95% på grid_guard.py

### Steg 1.3: Integrera Grid Guard i coordinator
**Vad:** Anropa `_grid_guard()` som FÖRSTA steg i `_async_update_data()`
**Ändring:** coordinator.py — lägg till 1 anrop, ~20 rader
**Beteende:** Grid Guard loggar varning vid brott men AGERAR INTE ännu
**Go/nogo:**
- CARMA Box startar utan krasch
- Grid Guard logg-rader syns i HA-loggen
- Inga AttributeError
**Mätbart:** `grep "GRID_GUARD" ha-log` visar entries

### Steg 1.4: Aktivera Grid Guard actions
**Vad:** Grid Guard AGERAR nu — pausar EV, ökar urladdning
**Ändring:** Byt från logg-only till faktisk action
**Go/nogo:**
- Simulera: starta EV + disk → grid > tak → EV pausas inom 30s
- Verifiera: grid sjunker under tak
- Verifiera: EV återstartar när disk klar
**Mätbart:** HC-1 kontrollpunkt PASS under 2h test

### Steg 1.5: Grid Guard go/nogo review
**Checklista:**
- [ ] 7 unit tests PASS
- [ ] Inga krasch under 4h drift
- [ ] Grid aldrig > tak under testperiod
- [ ] EV pausas korrekt vid disk
- [ ] EV återstartas korrekt efter disk
- [ ] HA restart → Grid Guard aktiv inom 30s
**GO = alla checked → fortsätt till Fas 2**
**NOGO = fix och upprepa 1.4**

---

## FAS 2: Proportionell urladdning

### Steg 2.1: Skapa `battery_balancer.py` modul
**Fil:** `custom_components/carmabox/core/battery_balancer.py`
**Funktioner:**
```python
def calculate_proportional_discharge(
    batteries: list[BatteryState],  # soc, cap_kwh, temp_c, min_soc
    total_discharge_w: int,
) -> list[int]:  # watts per battery
    """Proportional to available kWh. Cold-lock aware."""
```
**Go/nogo:** 6 unit tests PASS
**Mätbart:** Coverage ≥ 95%

### Steg 2.2: Unit tests
```
test_proportional_75_25             → [1500, 500] vid 2000W total
test_proportional_cold_lock         → [0, 2000] vid bat1 < 4°C
test_one_at_min_soc                 → [0, 2000] vid bat1 = min_soc
test_both_at_min_soc                → [0, 0]
test_dynamic_update                 → nya proportioner vid SoC-ändring
test_temp_rises_min_soc_drops       → mer kapacitet frigörs
```
**Go/nogo:** 6/6 PASS

### Steg 2.3: Ersätt `_cmd_discharge()` internals
**Vad:** `_cmd_discharge()` anropar `calculate_proportional_discharge()`
**Ändring:** coordinator.py `_cmd_discharge()` — byt internals
**Go/nogo:**
- Baseline-tester fortfarande PASS
- Proportional-tester PASS
- Manuell verifiering: sök 2000W → kontor ~1500W, förråd ~500W
**Mätbart:** CP-2 (batteribalans < 30 min diff)

### Steg 2.4: Proportionell urladdning go/nogo
- [ ] 6 unit tests PASS
- [ ] Baseline-tester fortfarande PASS
- [ ] Manuell verifiering visar korrekt split
- [ ] Batterier konvergerar mot min_soc samtidigt (±30 min)

---

## FAS 3: Plan-Executor koppling

### Steg 3.1: Skapa `plan_executor.py` modul
**Fil:** `custom_components/carmabox/core/plan_executor.py`
**Funktioner:**
```python
def plan_action_for_hour(
    plan: list[HourPlan],
    current_hour: int,
    state: CarmaboxState,
) -> ExecutorAction:
    """Read plan, return what executor should do."""
```
**Go/nogo:** Unit tests PASS

### Steg 3.2: Unit tests
```
test_plan_says_discharge            → action=discharge, watts=2000
test_plan_says_charge_pv            → action=charge_pv
test_plan_says_grid_charge          → action=grid_charge (verify price)
test_plan_says_idle                 → action=idle
test_pv_override_during_idle        → PV > 500W → charge_pv (fysik)
test_no_plan_available              → action=standby (safe default)
test_idle_but_grid_over_target      → reactive discharge
```
**Go/nogo:** 7/7 PASS

### Steg 3.3: Feature flag — `use_plan_executor`
**Vad:** Lägg till config option `use_plan_executor: false` (default OFF)
**Beteende:**
- OFF → gamla `_execute()` körs (nuvarande beteende)
- ON → nya `_execute_plan()` körs
**Go/nogo:** Kan toggla utan krasch

### Steg 3.4: Parallell körning (skugga)
**Vad:** Kör BÅDA executor:erna, men bara gamla agerar. Nya loggar.
**Mätbart:** Jämför beslut — hur ofta skiljer de sig?
**Go/nogo:** Nya executor:n fattar korrekta beslut ≥ 90% av tiden

### Steg 3.5: Aktivera plan executor
**Vad:** `use_plan_executor: true`
**Go/nogo:**
- Grid aldrig > tak under 24h test
- Batterier urladdar enligt plan
- EV når target SoC
- Inga kraschar

### Steg 3.6: Plan-Executor go/nogo
- [ ] 7 unit tests PASS
- [ ] 24h drift utan krasch
- [ ] Grid aldrig > tak
- [ ] Batterier följer plan (±20%)
- [ ] EV SoC target uppnått

---

## FAS 4: Surplus Chain (knapsack)

### Steg 4.1: Skapa `surplus_chain.py` modul
**Fil:** `custom_components/carmabox/core/surplus_chain.py`
**Funktioner:**
```python
def allocate_surplus(
    surplus_w: float,
    consumers: list[Consumer],  # id, priority, min_w, max_w, type, is_running
) -> list[Allocation]:
    """Knapsack: minimize export, respect priority."""
```
**Go/nogo:** Unit tests PASS

### Steg 4.2: Unit tests
```
test_ev_fits                        → EV started
test_ev_too_big_miner_fits          → miner started (not EV)
test_surplus_grows_bump_to_ev       → stop miner, start EV
test_increase_existing_first        → increase battery charge before new consumer
test_hysteresis_no_oscillation      → miner not toggled every 30s
test_all_consumers_filled           → export = 0
test_priority_when_equal_fit        → higher prio wins
```
**Go/nogo:** 7/7 PASS

### Steg 4.3: Integrera i coordinator
**Vad:** Ersätt nuvarande surplus-logik med `allocate_surplus()`
**Go/nogo:** PV-dag utan export > 30 min

### Steg 4.4: Surplus go/nogo
- [ ] 7 unit tests PASS
- [ ] Miner startar vid överskott
- [ ] EV startar när överskott räcker
- [ ] Miner→EV bump fungerar
- [ ] Export < 100W i medel under PV-timmar

---

## FAS 5: Planner fix (korrekt data)

### Steg 5.1: Fix `generate_plan()` SoC och prisdata
**Vad:** Verifiera att planner tar emot korrekt battery_soc och Nordpool-priser
**Tester:**
```
test_plan_receives_actual_soc       → SoC=97, inte 0
test_plan_receives_real_prices      → priser från Nordpool, inte fallback
test_plan_sunrise_detection         → hittar rätt PV-startimme
test_plan_ev_3phase_correct         → 6A = 4.14 kW, inte 1.38
```
**Go/nogo:** 4/4 PASS + plan visar korrekta värden i HA sensor

### Steg 5.2: Planner horisonter
**Vad:** Taktisk (4h/15min), Strategisk (24h/1h), Visionär (72h/4h)
**Go/nogo:** Plan-sensor visar alla tre horisonter

### Steg 5.3: Omplanering vid avvikelse
**Vad:** >20% avvikelse i 3 cykler → trigga omplanering
**Go/nogo:** Omplanering triggas och ny plan är bättre

---

## FAS 6: Natt-workflow

### Steg 6.1: Natt-workflow implementation
**Vad:** Komplett natt-cykel: EV-laddning + urladdning + disk-kompensation
**Tester:** Scenario B + C + D från manifest (solig natt + disk + restart)
**Go/nogo:** Alla 3 scenarier PASS i simulering

### Steg 6.2: Live natttest
**Vad:** Kör en hel natt med ny kod
**Go/nogo:**
- [ ] EV ≥ 75% kl 06
- [ ] Grid timmedel ≤ 4 kW ALLA timmar
- [ ] Batterier ≤ 20% kl 06 (om sol imorgon)
- [ ] Disk hanterad korrekt (om den körde)
- [ ] Nattrapport genererad kl 06:30

---

## FAS 7: Law Guardian + RCA

### Steg 7.1: Kontrollpunkter (CP, HC, DC)
### Steg 7.2: Breach Records + incident-rapport
### Steg 7.3: Automatisk RCA
### Steg 7.4: Notifiering

---

## FAS 8: ML + Autonomitet

### Steg 8.1: Prediktor — konsumtionsprofil
### Steg 8.2: Lärande feedback-loop
### Steg 8.3: Autonom parameteranpassning

---

## FAS 9: Självläkning + Resiliens

### Steg 9.1: HA restart-resiliens
### Steg 9.2: Sensor fallback
### Steg 9.3: Circuit breaker

---

## TIDSLINJE (uppskattning)

| Fas | Steg | Tid | Kumulativt |
|-----|------|-----|-----------|
| 0 | Förberedelser | 0.5 dag | 0.5 dag |
| 1 | Grid Guard | 1 dag | 1.5 dagar |
| 2 | Proportionell | 0.5 dag | 2 dagar |
| 3 | Plan-Executor | 1.5 dagar | 3.5 dagar |
| 4 | Surplus Chain | 1 dag | 4.5 dagar |
| 5 | Planner fix | 1 dag | 5.5 dagar |
| 6 | Natt-workflow | 1 dag (+ 1 natt live) | 6.5 dagar |
| 7 | Guardian + RCA | 1 dag | 7.5 dagar |
| 8 | ML | 2 dagar | 9.5 dagar |
| 9 | Självläkning | 1 dag | 10.5 dagar |

**Fas 1 (Grid Guard) är den enda som krävs innan CARMA Box kan aktiveras igen.**
Resten kan deployas iterativt medan CARMA Box körs.

---

## GO/NOGO MILSTOLPAR

| Milstolpe | Krav | Effekt |
|-----------|------|--------|
| **M1: Grid Guard LIVE** | Fas 0 + 1 klar | CARMA Box kan aktiveras med Grid Guard som spärr |
| **M2: Batterier styrs av plan** | Fas 2 + 3 klar | Batterierna urladdar/laddar enligt plan |
| **M3: Surplus optimerad** | Fas 4 klar | 0 W export när förbrukare finns |
| **M4: Första framgångsrika natten** | Fas 5 + 6 klar | EV 75%, grid < tak, batterier tömda |
| **M5: Autonomt system** | Fas 7 + 8 + 9 klar | CARMA Box sköter sig själv |
