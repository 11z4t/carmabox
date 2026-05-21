# bat_balancer — Komponent-kontrakt (HACS)

**Domain:** `bat_balancer`
**Repo:** github.com/11z4t/carmabox
**Version:** 3.0.0 (manifest) — bumps via Phase 1
**Status:** DRAFT — väntar review 900/901 (Frej + Storm)

## Syfte

bat_balancer fördelar en **erbjuden effekt** (W, signerad) mellan två batteribanker (kontor + förråd) och styr GoodWe-inverter EMS så att faktisk laddning/urladdning följer offer-kontraktet.

> "bat bal vet bara en effekt som ska fördelas och den ska förhålla sig till det" — Borje 2026-05-21

## Ansvar (vad komponenten styr)

1. **Effekt** — magnitud (W) som distribueras
2. **Riktning** — charge / discharge / standby (= tecken på offer)
3. **EMS** — `ems_mode` (charge_battery / battery_standby / discharge_battery) + `ems_power_limit` (magnitud) per bank
4. **op_mode = peak_shaving ALLTID** — säkerställer att GoodWe-inverter inte avviker (Borje HARD INVARIANT)
5. **SoC-balansering** — bias-fördelning så `|SoC_kontor − SoC_forrad| ≤ 1%` sustained

## Vad komponenten INTE styr

- ❌ Beräknar inte själv målerbjudande (= Brain ansvar)
- ❌ Läser inte grid_w, pv_w, EV-state direkt (= Brain ansvar)
- ❌ Läser inte `input_select.bat_balancer_mode` (= Brain ansvar — se Brain-kontrakt)
- ❌ Skriver aldrig till `select.goodwe_inverter_operation_mode` annat än `peak_shaving`

---

## Input-kontrakt

### Helpers som bat_balancer LÄSER

| Entity | Typ | Roll |
|--------|-----|------|
| `input_number.brain_target_bat_w` | input_number | **ERBJUDEN EFFEKT** (signerad W). Skrivs av Brain. EXAKT EN input för bat_balancer. |

### Helpers som Brain LÄSER (bat_balancer rör dem ALDRIG)

| Entity | Typ | Roll |
|--------|-----|------|
| `input_select.bat_balancer_mode` | input_select | AUTO / MANUAL / SHADOW. Operatörs-override. |
| `input_number.bat_balancer_target_manual_w` | input_number | Manuell önskad effekt (signerad W). Gäller endast om mode=MANUAL. |

> **Brain-kontrakt:** se `custom_components/brain/CONTRACT.md` — Brain läser dessa, översätter till offer, skriver `brain_target_bat_w`.

### Sensorer från externa integrationer (läses av bat_balancer)

- `sensor.goodwe_battery_soc_kontor`
- `sensor.goodwe_battery_soc_forrad`
- `sensor.goodwe_battery_temperature_kontor`
- `sensor.goodwe_battery_temperature_forrad`
- (per CONFIG_FLOW — varje bank konfigurerbar)

---

## Output-kontrakt

### Sensorer som bat_balancer EXPONERAR

| Entity | Beskrivning | Enhet | Use case |
|--------|-------------|-------|----------|
| `sensor.bat_balancer_distributed_total_w` | **FÖRDELAD EFFEKT** (signerad sum kontor + förråd) | W | UI-card "Fördelad"-kolumn + Brain closed-loop |
| `sensor.bat_balancer_distribution_kontor_w` | Per-bank distribution kontor | W | Diagnostik |
| `sensor.bat_balancer_distribution_forrad_w` | Per-bank distribution förråd | W | Diagnostik |
| `sensor.bat_balancer_target_effective_w` | Det offer bat_balancer agerar på (= read-through av brain_target_bat_w efter stale-handling) | W | Felsökning |
| `sensor.bat_balancer_capability` | `ok`/`degraded` + `max_w_now`-attribut (= kapacitetsgolv) | text | UI-status |
| `sensor.bat_balancer_avg_soc_pct` | Medel-SoC båda banker | % | UI-display |

### Inverter-writes (HW-control)

Per bank (kontor/förråd):
- `select.goodwe_inverter_operation_mode_<bank>` → **PEAK_SHAVING ALLTID** (HARD INVARIANT)
- `select.goodwe_inverter_ems_mode_<bank>` → enligt offer-tecken:
  - offer > 0 → `charge_battery`
  - offer < 0 → `discharge_battery` (eller `battery_standby` om PV-surplus mode)
  - offer = 0 → `battery_standby`
- `number.goodwe_inverter_ems_power_limit_<bank>` → magnitud (|distribution_<bank>_w|)

---

## Beteende-kontrakt (HARD INVARIANTs)

### INV-1: Offer-cap EXAKT (ingen tolerans)
```
|distribution_kontor_w + distribution_forrad_w| ≤ |brain_target_bat_w|
```
**Borje 2026-05-21:** Erbjuden effekt = MAX, ingen +100W-tolerans.

### INV-2: Tecken-bevarande
Båda banker = samma tecken som offer. Inga motsatta per-bank-targets (= ingen kontor-charge medan förråd-disch).

### INV-3: SoC-konvergens
```
|SoC_kontor − SoC_forrad| ≤ 1.0%   (sustained efter 1h drift)
```
Bias-distribution mot bank med lägst SoC (charge) eller högst SoC (disch).

### INV-4: op_mode = peak_shaving ALLTID
bat_balancer skriver aldrig `general`, `eco_charge`, eller annat än `peak_shaving` till operation_mode.

### INV-5: Stale-offer = 0
Om `brain_target_bat_w` är stale (>60s utan write) → bat_balancer behandlar offer = 0.

### INV-6: BMS-cap respekt
Om bank rapporterar BMS-fault eller temperatur utanför 0-45°C → bank exkluderas, overflow distribueras till andra banken (BMS-overflow-redistribution).

### INV-7: Tecken-transition-ramp
Vid offer-tecken-byte (charge ↔ disch) MÅSTE ramp-down via 0 (sign-state-machine i `sign_state_machine.py`).

---

## Cykel (varje 5s)

```
1. READ: offer_w = state(input_number.brain_target_bat_w)
2. STALE-CHECK: om age > 60s → offer_w = 0
3. CAPABILITY: läs BMS-status, temperatur, SoC båda banker
4. DISTRIBUTE: distribution_engine.distribute_target_to_banks(offer_w, banks)
   - Headroom-weighted allocation
   - SoC-equalization bias
   - BMS-cap + overflow-redistribution
   - INV-1 clamp (|sum| ≤ |offer|)
5. SIGN-STATE-MACHINE: ramp via 0 vid tecken-byte
6. WRITE-HW: ems_mode + ems_power_limit per bank
7. WRITE-OP-MODE: peak_shaving (om avviker)
8. PUBLISH SENSORS: distributed_total_w + per-bank + capability
```

---

## Konfigurationskrav (HA-installation)

### Required helpers (skapas via config_flow OM auto-create=true, annars manuell YAML)

```yaml
input_select:
  bat_balancer_mode:
    name: "Bat bal mode"
    options: [AUTO, MANUAL, SHADOW]
    icon: mdi:tune-variant

input_number:
  bat_balancer_target_manual_w:
    name: "Bat bal MANUAL target (W)"
    min: -25000
    max: 25000
    step: 100
    initial: 0
    unit_of_measurement: "W"
    mode: box

  brain_target_bat_w:
    name: "Brain target bat (W)"
    min: -25000
    max: 25000
    step: 1
    unit_of_measurement: "W"
    mode: box
```

### Required banks (config_flow)

Per bank (kontor + förråd, eller anpassat):
- entity_id för SoC-sensor
- entity_id för temperatur-sensor
- entity_id för EMS mode-select
- entity_id för EMS power-limit-number
- entity_id för operation_mode-select

---

## QC / Acceptanskriterier (HARD)

### AC-CONTRACT-1: Offer-respons (charge)
```
GIVEN brain_target_bat_w = +3000
WHEN tick + 10s
THEN distribution_kontor + distribution_forrad ∈ [+2900, +3000]
AND båda banker ems_mode = charge_battery
AND båda banker op_mode = peak_shaving
```

### AC-CONTRACT-2: Offer-respons (discharge)
```
GIVEN brain_target_bat_w = -3000
WHEN tick + 10s
THEN distribution_kontor + distribution_forrad ∈ [-3000, -2900]
AND båda banker ems_mode = discharge_battery (eller battery_standby)
AND båda banker op_mode = peak_shaving
```

### AC-CONTRACT-3: Offer = 0
```
GIVEN brain_target_bat_w = 0
WHEN tick + 10s
THEN båda distribution = 0
AND båda banker ems_mode = battery_standby
AND ems_power_limit = 0
AND op_mode = peak_shaving
```

### AC-CONTRACT-4: Stale Brain
```
GIVEN brain_target_bat_w inte uppdaterad på 65s
WHEN tick
THEN offer_w internal = 0
AND distribution = 0/0
AND op_mode = peak_shaving (skall ej ändras)
```

### AC-CONTRACT-5: SoC-konvergens
```
GIVEN SoC_kontor = 30%, SoC_forrad = 70%, offer = -3000 (disch)
WHEN 1h drift
THEN |SoC_kontor − SoC_forrad| < 1%
AND distribution_forrad |w| > distribution_kontor |w| (= mer disch på högre SoC)
```

### AC-CONTRACT-6: BMS-cap
```
GIVEN bank kontor BMS-fault, bank förråd ok, offer = +5000
WHEN tick
THEN distribution_kontor = 0
AND distribution_forrad ∈ [+4900, +5000]   (övertar)
AND INV-1 respekteras
```

### AC-CONTRACT-7: op_mode-guardian
```
GIVEN något skriver operation_mode = general
WHEN bat_balancer tick (max 30s senare)
THEN operation_mode = peak_shaving (återställs)
AND log entry "op_mode-restored" emitted
```

---

## QC-implementation

### Pytest-regression (`tests/regression/test_contract_bat_balancer.py`)
Varje AC-CONTRACT-N har motsvarande pytest-case med mockad HA-state.

### Live-guardian (HA-automation)
`automation.bat_balancer_contract_violation_guardian`:
- Triggers var 30s
- Conditions: INV-1 brott (|distributed_total_w| > |brain_target_bat_w| + 50W deadband)
- Action: Slack [P1] + log

### CI-gates (carmabox-repo)
- Pre-commit: ruff + py_compile + smoke pytest
- GitHub Actions: pytest unit/regression + coverage ≥90%
- Manifest-version-bump-check (måste matcha senaste git-tag)

---

## Out of scope (för denna komponent)

- Beräkning av offer (= Brain)
- ev_balancer + binary_balancer (= separata HACS-komponenter)
- UI-card / Lovelace (= per-installation, ej i HACS-paketet)
- Operator-helpers UI (= per-installation YAML eller blueprint)
