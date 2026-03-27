# Grid Guard — Detaljerad Design v1.0

## Syfte
Säkerställa att Ellevio viktat timmedelvärde ALDRIG överstiger 2.0 kW.
Grid Guard är LAG 1 i manifestet — högsta prioritet, övertrumfar allt.

## Input-sensorer

| Sensor | Typ | Användning |
|--------|-----|-----------|
| `sensor.ellevio_viktad_timmedel_pagaende` | float kW | Primär — redan viktad |
| `sensor.house_grid_power` | float W | Momentan grid (HomeWizard P1) |
| `sensor.ellevio_viktad_prognos_timmedel` | float kW | HA:s projicering |

## Konstanter (parameterstyrda)

| Parameter | Default | Källa |
|-----------|---------|-------|
| `ellevio_tak_kw` | 2.0 | Config flow |
| `ellevio_night_weight` | 0.5 | Config flow |
| `grid_guard_margin` | 0.85 | Config flow |
| `main_fuse_a` | 25 | Config flow |
| `grid_guard_vp_min_temp_c` | 10.0 | Config flow |

## Beräkningsmodell

```python
# Ellevio viktat timmedel (sensorn levererar redan detta)
viktat_timmedel = float(sensor.ellevio_viktad_timmedel_pagaende)

# Projicering: om nuvarande effekt fortsätter, var landar timmen?
elapsed_min = minuter_sedan_timmens_start()
remaining_min = 60 - elapsed_min
grid_now_kw = house_grid_power / 1000
vikt = 0.5 om natt (22-06), 1.0 om dag (06-22)
grid_viktat_now = grid_now_kw * vikt

# Egen projicering (oberoende dubbelcheck)
projected = (viktat_timmedel * elapsed_min + grid_viktat_now * remaining_min) / 60

# Headroom
headroom_kw = ellevio_tak_kw * grid_guard_margin - projected
```

## Förbud (invarianter — kollas VARJE cykel FÖRE åtgärdstrappan)

```python
def check_invariants(self, state) -> list[GridGuardResult]:
    """Kollas ALLTID, oavsett headroom. Brott = omedelbar korrigering."""
    violations = []

    # INV-1: ALDRIG EMS auto
    for adapter in inverter_adapters:
        if adapter.ems_mode == "auto":
            violations.append(
                fix=set_ems_mode("battery_standby"),
                reason="INV-1: EMS auto förbjudet"
            )

    # INV-2: ALDRIG korskörning
    if bat1_charging and bat2_discharging or bat1_discharging and bat2_charging:
        violations.append(
            fix=set_both_standby(),
            reason="INV-2: Korskörning detekterad"
        )

    # INV-3: ALDRIG fast_charging utan explicit beslut
    for adapter in inverter_adapters:
        if adapter.fast_charging_on and not self._fast_charge_authorized:
            violations.append(
                fix=set_fast_charging(off),
                reason="INV-3: Fast charging utan beslut"
            )

    # INV-4: ALDRIG ladda vid cold lock
    for adapter in inverter_adapters:
        if adapter.cell_temp < cold_lock_temp_c and adapter.is_charging:
            violations.append(
                fix=set_ems_mode("battery_standby"),
                reason=f"INV-4: Laddning vid {adapter.cell_temp}°C"
            )

    return violations
```

Förbuden körs FÖRE åtgärdstrappan. Om ett förbud bryts:
1. Korrigera OMEDELBART
2. Logga BreachRecord
3. Notifiera via Slack
4. **Trigga omplanering** — planen som ledde till förbudsbrott var felaktig
5. Ny plan genereras med constraint som förhindrar samma situation

## Åtgärdstrappa

Vid `headroom < 0` (projicerat > tak × margin):

```
Steg 1: Stäng av VP kontor
        → om kontorstemperatur > 10°C
        → klimat.set_hvac_mode: off
        → sparar 0.5-2 kW

Steg 2: Stäng miner
        → switch off
        → sparar ~500W

Steg 3: Stäng elvärmare pool (om cirkpump ON)
        → switch off
        → sparar 2-6 kW

Steg 4: Stäng VP pool (om cirkpump ON)
        → switch off
        → sparar 0.5-3 kW

Steg 5: Sänk EV amps
        → beräkna max amps som håller under tak
        → min 6A, annars steg 6

Steg 6: Pausa EV helt
        → Easee disable
        → sparar 4-11 kW

Steg 7: Öka batteriurladdning
        → öka ems_power_limit
        → sparar 0-5 kW per inverter

Varje steg utvärderas: räcker det? Om ja → stoppa.
Om nej → nästa steg.
```

## Återställning

När `headroom > 0` i 60+ sekunder:
- Återstarta i OMVÄND ordning (7→1)
- Hysteres: vänta `surplus_start_delay_s` (60s) innan återstart
- EV: återstarta vid 6A, ramp upp gradvis

## State Machine

```
         ┌──────────┐
    ──→  │   OK     │
         │headroom>0│
         └────┬─────┘
              │ headroom < 0
         ┌────v─────┐
         │ WARNING  │  → Slack notis
         │ steg 1-4 │  → Stäng laster
         └────┬─────┘
              │ fortfarande < 0 efter 30s
         ┌────v─────┐
         │ CRITICAL │  → Slack + EV åtgärd
         │ steg 5-7 │  → Sänk/pausa EV
         └────┬─────┘
              │ headroom > 0 i 60s
         ┌────v─────┐
         │ RECOVERY │  → Återställ laster
         │ omvänd   │  → Gradvis
         └────┬─────┘
              │ allt återställt
              └──→ OK
```

## Gränssnitt (API)

```python
@dataclass
class GridGuardConfig:
    tak_kw: float = 2.0
    night_weight: float = 0.5
    margin: float = 0.85
    day_start_hour: int = 6
    day_end_hour: int = 22
    main_fuse_a: int = 25
    vp_min_temp_c: float = 10.0

@dataclass
class GridGuardState:
    hour: int = -1
    status: str = "OK"               # OK | WARNING | CRITICAL | RECOVERY
    accumulated_viktat_wh: float = 0
    sample_count: int = 0
    last_grid_w: float = 0
    last_update: float = 0
    actions_taken: list[str] = field(default_factory=list)
    ev_was_paused: bool = False
    ev_was_reduced_to: int = 0
    vp_was_off: bool = False
    lasters_stopped: list[str] = field(default_factory=list)

@dataclass
class GridGuardResult:
    action: str          # none | reduce_load | reduce_ev | pause_ev | increase_discharge
    headroom_kw: float
    projected_kw: float
    viktat_timmedel_kw: float
    status: str          # OK | WARNING | CRITICAL | RECOVERY
    commands: list[dict] # [{entity, service, data}, ...]
    reason: str

class GridGuard:
    def __init__(self, config: GridGuardConfig): ...

    def evaluate(
        self,
        viktat_timmedel_kw: float,      # sensor.ellevio_viktad_timmedel_pagaende
        grid_import_w: float,            # sensor.house_grid_power
        ev_power_w: float,               # sensor.easee_home_12840_power
        ev_amps: int,
        ev_phase_count: int,
        battery_available_kwh: float,
        active_consumers: list[Consumer], # Alla aktiva styrbara förbrukare
        kontor_temp_c: float,            # climate.kontor_ac current_temperature
        timestamp: float,
    ) -> GridGuardResult: ...

    def reset_hour(self, hour: int): ...

    @property
    def headroom_kw(self) -> float: ...

    @property
    def projected_timmedel_kw(self) -> float: ...
```

## HA-sensorer (output)

```
sensor.carmabox_grid_guard_status        → "OK" | "WARNING" | "CRITICAL" | "RECOVERY"
sensor.carmabox_grid_guard_headroom_kw   → 0.6
sensor.carmabox_grid_guard_projected_kw  → 1.4
sensor.carmabox_grid_guard_actions       → "VP kontor av, EV 6A"
```

## Notifieringar

| Händelse | Kanal | Timing |
|----------|-------|--------|
| WARNING (steg 1-4) | Slack | Omedelbart |
| CRITICAL (steg 5-7) | Slack | Omedelbart |
| LAG 1 brott (timmedel > tak) | Slack | Omedelbart |
| Alla åtgärder i dygnsrapport | Email | 06:30 |
| "Inga resurser kvar" | Slack | Omedelbart — kräver manuell åtgärd |

## Mätpunkter (Fas 1)

### Realtid (varje 30s cykel)
| ID | Mätpunkt | Gräns | Verifiering |
|----|----------|-------|-------------|
| GG-CP1 | `projected_viktat_kw` | ≤ tak × margin (1.7) | Loggas varje cykel |
| GG-CP2 | `reaction_time_ms` | < 1000 | Tidsstämpel före/efter evaluate() |
| GG-CP3 | `commands_executed` | Alla OK | Verifiera adapter-svar |
| GG-CP4 | `headroom_accuracy` | ±15% vs faktiskt timmedel | Jämför vid timmens slut |

### Per timme
| ID | Mätpunkt | Gräns | Verifiering |
|----|----------|-------|-------------|
| GG-HC1 | `actual_viktat_timmedel` | ≤ 2.0 kW | Aldrig brott |
| GG-HC2 | `guard_interventions` | count | Logga antal |
| GG-HC3 | `false_positive_rate` | < 5% | Ingripande utan behov |
| GG-HC4 | `headroom_min` | > 0 | Aldrig negativt vid timmens slut |

### Per dygn
| ID | Mätpunkt | Gräns | Verifiering |
|----|----------|-------|-------------|
| GG-DC1 | `max_viktat_timmedel` | ≤ 2.0 kW | Nattrapport |
| GG-DC2 | `lag1_brott_count` | 0 | Nattrapport |
| GG-DC3 | `total_ev_pause_min` | Minimera | Nattrapport |
| GG-DC4 | `total_vp_off_min` | Minimera | Nattrapport |

## Tester (22 st)

### Unit tests — ren logik (ingen HA)

```python
# === Grundläggande ===
test_under_tak_no_action()
    # viktat=1.0, tak=2.0, margin=0.85 → headroom=0.7 → action=none

test_over_margin_warning()
    # viktat=1.8, tak=2.0, margin=0.85 → headroom=-0.1 → status=WARNING

test_over_tak_critical()
    # viktat=2.1, tak=2.0 → headroom=-0.1 → status=CRITICAL

# === Åtgärdstrappa ===
test_step1_vp_kontor_off()
    # WARNING + VP kontor aktiv + temp>10°C → stäng VP

test_step1_vp_kontor_skip_cold()
    # WARNING + VP kontor aktiv + temp<10°C → SKIPPA, nästa steg

test_step2_miner_off()
    # WARNING + miner aktiv → stäng miner

test_step5_reduce_ev_amps()
    # CRITICAL + EV 16A + overshoot 2kW → sänk till 13A (3fas)

test_step6_pause_ev()
    # CRITICAL + EV 6A + fortfarande over → pausa

test_step7_increase_discharge()
    # CRITICAL + EV pausad + batteri available → öka urladdning

test_combined_steps()
    # Stort overshoot → VP av + miner av + EV sänkt

# === Projicering ===
test_projection_early_hour()
    # kl XX:05, grid 5kW → projicerat ~5kW (stor påverkan)

test_projection_late_hour()
    # kl XX:55, grid 5kW, medel 1.5kW → projicerat ~1.8kW (liten påverkan)

test_projection_accuracy()
    # Simulera hel timme, verifiera projicering vs faktiskt

# === Återställning ===
test_recovery_reverse_order()
    # headroom > 0 i 60s → återstarta i omvänd ordning

test_recovery_ev_gradual()
    # EV pausad → starta vid 6A, inte 16A

test_recovery_hysteresis()
    # headroom fluktuerar runt 0 → ingen oscillation

# === Edge cases ===
test_hour_reset()
    # kl XX:00 → nollställ ackumulering

test_sensor_unavailable()
    # viktat_timmedel = unavailable → fallback senaste + margin

test_3phase_ev_math()
    # 1 amp sänkning = 690W (3×230), inte 230W

test_night_vs_day_weight()
    # Samma faktisk effekt → lägre viktat nattetid

# === Förbud (invarianter) ===
test_inv1_ems_auto_detected()
    # EMS=auto → korrigera till standby + breach record

test_inv1_ems_auto_triggers_replan()
    # EMS=auto → omplanering triggas

test_inv2_crosscharge_detected()
    # bat1 charging + bat2 discharging → båda standby

test_inv2_crosscharge_triggers_replan()
    # Korskörning → omplanering triggas

test_inv3_fast_charging_unauthorized()
    # fast_charging ON utan beslut → stäng av

test_inv4_cold_lock_charging()
    # Laddning vid 3°C → stoppa laddning, urladdning OK

test_inv4_cold_lock_discharge_ok()
    # Urladdning vid 3°C → TILLÅT (bara laddning förbjuds)

test_invariants_run_before_actions()
    # Förbud kollas FÖRE åtgärdstrappan

# === Integration ===
test_grid_guard_called_first()
    # evaluate() anropas före _execute_plan()

test_grid_guard_blocks_on_critical()
    # CRITICAL → _execute_plan() körs INTE

test_grid_guard_sensor_mapping()
    # Verifiera att alla sensorer mappas korrekt från HA
```

### Gränssnittstester (mockad HA)

```python
test_scenario_disk_plus_ev()
    # Disk 2kW startar + EV 4.1kW + hus 1.7kW = 7.8kW
    # Natt: viktat = 7.8 * 0.5 = 3.9 > 2.0*0.85
    # → VP av, EV pausad → grid = 3.7kW → viktat 1.85 ✅

test_scenario_short_spike_ok()
    # kl XX:50, snitt 1.5kW, spike 6kW i 3 min
    # projicerat = (1.5*50 + 6*3 + 1.5*7)/60 = 1.65 → under tak ✅
    # → INGEN åtgärd (spike är kort)
```
