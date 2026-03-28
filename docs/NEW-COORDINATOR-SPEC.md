# Ny Coordinator — Kravspecifikation

## Syfte

Ersätt nuvarande coordinator.py (5800+ rader, blandning av gammal och ny kod)
med en ren coordinator som ENBART anropar core-moduler.

## Grundprinciper

1. **Coordinator = dirigent, inte musiker** — den anropar moduler, gör inget själv
2. **Ingen duplicerad logik** — varje beslut fattas av EN modul
3. **Stateless per cykel** — all state läses från sensorer/adapters, inte intern cache
4. **Persistent state via runtime_store** — överlever HA restart
5. **Testbar** — varje metod kan testas isolerat med mockad HA

## Cykel (var 30:e sekund)

```
async def _async_update_data():
    1. STARTUP SAFETY (om inte klar)
    2. COLLECT STATE (läs alla sensorer)
    3. GRID GUARD (LAG 1 + INV 1-5, VETO)
    4. PLAN GENERATION (var 5 min)
    5. PLAN EXECUTION (om grid guard inte agerade)
    6. BATTERY BALANCER (fördela urladdning/laddning)
    7. NATT-EV WORKFLOW (22-06, EV + batteristöd)
    8. SURPLUS CHAIN (fördela överskott/minska vid import)
    9. LAW GUARDIAN (kontrollera alla lagar, logga breach)
    10. PERSIST STATE (spara till runtime_store)
    11. PUBLISH SENSORS (uppdatera HA-sensorer)
```

## Steg-specifikation

### 1. STARTUP SAFETY
**Trigger:** Första cykel efter HA restart
**Åtgärd:**
- Stäng av fast_charging på ALLA inverters
- Sätt battery_standby tills sensorer redo
- Restora persistent state (night_ev_active, plan, ev_enabled)
- Om night_ev_active: starta EV (override_schedule + 6A)
- Vänta tills ALLA sensorer svarar innan normal drift
**Acceptanskriterier:**
- AC1: fast_charging=OFF bekräftat inom 30s efter restart
- AC2: night_ev_active restorerad korrekt
- AC3: Ingen åtgärd sker med unavailable sensordata

### 2. COLLECT STATE
**Åtgärd:** Läs alla sensorer via adapters → CarmaboxState
**Sensorer:**
- Grid: sensor.house_grid_power
- Ellevio: sensor.ellevio_viktad_timmedel_pagaende
- Batteri K: SoC, power, temp, EMS mode, fast_charging
- Batteri F: SoC, power, temp, EMS mode, fast_charging
- EV: SoC, power, current, status, cable_locked
- PV: sensor.pv_solar_total
- Pris: Nordpool current price
- Vitvaror: disk, tvätt, tumlare power
- Tempest: radiation, illuminance, pressure, temperature
**Acceptanskriterier:**
- AC1: Alla sensorer har fallback vid unavailable (Resilience Manager)
- AC2: State-objekt komplett varje cykel
- AC3: Unavailable-sensorer flaggas, inte ignoreras

### 3. GRID GUARD
**Modul:** core/grid_guard.py (redan klar, 34 tester)
**Input:** state + config
**Output:** GridGuardResult (status, headroom, commands)
**Åtgärd:** Om commands → verkställ via adapters
**VETO:** Om grid guard agerar → skippa steg 5
**Acceptanskriterier:**
- AC1: Körs VARJE cykel FÖRE all annan logik
- AC2: INV-1 till INV-5 kollas varje cykel
- AC3: Åtgärdstrappa: VP av → miner av → sänk EV → pausa EV → urladdning
- AC4: Förbudsbrott → omplanering triggas

### 4. PLAN GENERATION
**Modul:** core/planner.py → optimizer/planner.py
**Trigger:** Var 5 min ELLER deviation trigger ELLER startup
**Input:** Nordpool priser, Solcast PV, konsumtionsprofil, EV demand
**Output:** list[HourPlan] sparad i self.plan
**Acceptanskriterier:**
- AC1: Plan med riktiga Nordpool-priser (inte fallback 100)
- AC2: Natt-reserv: spara batteri dagtid om needed tonight
- AC3: Sol-medveten urladdningshastighet
- AC4: Temperaturmedveten min_soc
- AC5: Plan överlever restart (persistent)

### 5. PLAN EXECUTION
**Modul:** core/plan_executor.py (redan klar, 28 tester)
**Input:** plan[current_hour] + state
**Output:** ExecutorCommand (battery_action, ev_action, amps)
**Åtgärd:** Verkställ battery command via adapters
**Acceptanskriterier:**
- AC1: Planen STYR execution, inte ad-hoc regler
- AC2: PV override: sol fångas oavsett plan
- AC3: Reaktiv urladdning vid grid > target ÄVEN utan plan
- AC4: Ingen åtgärd om grid guard har VETO

### 6. BATTERY BALANCER
**Modul:** core/battery_balancer.py (redan klar, 22 tester)
**Input:** batteriinfo + total_watts
**Output:** per-batteri allokering (watts)
**Åtgärd:** Sätt EMS mode + ems_power_limit per adapter
**Acceptanskriterier:**
- AC1: Proportionell till available_kwh
- AC2: Cold lock → omfördela till varmt batteri
- AC3: Båda når min_soc samtidigt (±30 min)
- AC4: ALDRIG EMS auto
- AC5: ALDRIG fast_charging utan authorized flag

### 7. NATT-EV WORKFLOW
**Trigger:** Natt (22-06) + EV ansluten + SoC < target
**Åtgärd:**
- override_schedule (Easee intern schema)
- set_charger_max_limit(6) — ALLTID starta vid 6A
- _cmd_ev_start(6)
- Proportionell urladdning för batteristöd
- ALDRIG sätta > 6A utan headroom-beräkning
**Stopp:** departure_hour ELLER target SoC nådd ELLER inte natt
**Persistent:** night_ev_active överlever restart
**Acceptanskriterier:**
- AC1: EV startar automatiskt kl 22 om SoC < target
- AC2: override_schedule anropas (Easee intern schema)
- AC3: Grid under tak med batteristöd
- AC4: Disk detekteras → EV pausas → disk klar → EV startas
- AC5: HA restart → EV startas igen automatiskt
- AC6: ALDRIG 16A som default — alltid 6A start

### 8. SURPLUS CHAIN
**Modul:** core/surplus_chain.py (redan klar, 16 tester)
**Trigger:** Varje cykel
**Vid export:** allocate_surplus → starta förbrukare (knapsack)
**Vid import > target:** should_reduce_consumers → stoppa förbrukare
**Förbrukare:** Alla styrbara (EV, batteri, miner, VP, pool)
**Acceptanskriterier:**
- AC1: Export 0W om förbrukare finns
- AC2: Öka befintlig variabel förbrukare FÖRE ny
- AC3: Knapsack: fyller det som FÅR PLATS
- AC4: Hysteres: 60s start, 180s stopp
- AC5: Bump: stoppa lågprio för att starta högprio

### 9. LAW GUARDIAN
**Modul:** core/law_guardian.py (redan klar, 24 tester)
**Trigger:** Varje cykel
**Åtgärd:** Kolla LAG 1-7 + INV 1-5, skapa BreachRecords
**Notifiering:** 3+ brott/h → Slack, daglig rapport → email
**Acceptanskriterier:**
- AC1: Alla lagar kollas varje cykel
- AC2: BreachRecord med root_cause sparas
- AC3: Slack-notifiering vid kritiska brott
- AC4: Daglig/vecko-sammanfattning

### 10. PERSIST STATE
**Trigger:** Varje cykel (dirty flag)
**Data:** plan, night_ev_active, ev_enabled, ev_amps, last_command
**Acceptanskriterier:**
- AC1: Alla kritiska state överlever restart
- AC2: Max 30s förlust vid krasch

### 11. PUBLISH SENSORS
**Sensorer:**
- carmabox_grid_guard_status
- carmabox_grid_guard_headroom_kw
- carmabox_grid_guard_projected_kw
- carmabox_decision_reason
- carmabox_plan_status
- carmabox_battery_soc
- carmabox_ev_status
**Acceptanskriterier:**
- AC1: Sensorer uppdateras varje cykel
- AC2: Attribut innehåller reasoning chain

## Vad som INTE ska finnas

- Ingen `_execute()` (gamla regel-kedjan R0.5-R3)
- Ingen `_execute_ev()` (gamla EV-logik)
- Ingen `_watchdog()` W1-W5 som styr batteri/EV
- Ingen `_self_heal_ev_tamper()`
- Ingen `_check_plan_correction()` (ersatt av replan i executor)
- Ingen `_cmd_charge_pv()` med fast_charging=ON
- Ingen `_cmd_discharge()` med EMS auto
- Inga hårdkodade sensorer — allt via config/adapters
