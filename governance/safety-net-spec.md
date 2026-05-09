# CARMA Box — Safety-Net Specification

**Version:** 2.0  
**Uppdaterad:** 2026-05-08  
**Börs-direktiv:** 2026-05-07 (initial) + 2026-05-08 00:38 (förtydligande min-active)  
**Status:** GÄLLANDE

---

## Två funktioner

Safety-net består av **två separata funktioner** som BÅDA alltid är aktiva:

### 1. PASSIVE GUARD — "Do No Harm"
Hindrar destruktiva kommandon. Blockerar enskilda operationer om villkor ej uppfylls.  
Ingen corrective action — svarar bara JA/NEJ på "är detta kommando säkert?".

Implementeras i: `optimizer/safety_guard.py` (SafetyGuard-klassen)

### 2. MIN-ACTIVE KEEP-ALIVE — "Keep House Lit"
Säkerställer att el-flow till hus + kontor aldrig avbryts — varken av Brain-beslut eller Brain-stall.  
Periodisk verifiering + corrective action om något är fel.

Implementeras i: koordinator-loopen + ny `safety_net_monitor()` funktion (se C7–C10)

---

## Hard Invariants (kan ALDRIG åsidosättas)

### INV-1: FORBIDDEN_INVERTER_MODES
```python
FORBIDDEN_INVERTER_MODES = ['off_grid', 'backup-only']
```
- `off_grid` kopplar loss AC-ut → **huset förlorar el omedelbart**
- `backup-only` isolerar invertern → ingen grid-fed backup
- Tillåtna lägen: `general`, `peak_shaving`, `eco`, `self_use`
- Vad händer vid försök: avvisas med ERROR-log + Slack-alarm, ej utfört

**Bakgrund:** 2026-05-07 sattes förrådet i `off_grid` av misstag → elen gick ned i huset.

### INV-2: MIN_BACKUP_FLOOR_PCT
```python
DEFAULT_MIN_BACKUP_FLOOR_PCT = 10  # % SoC — Börje-tunable via input_number
```
- ≥ 10% SoC reserveras alltid för backup vid grid-failure
- Brain får INTE discharge under denna gräns
- Vid <4°C: höjs till 20% (cold-protection)
- Configurerbar: `input_number.carma_min_backup_floor_pct` (range 5–30, default 10)

### INV-3: GRID-TIED PERSISTENS
Båda invertrar (kontor + förråd) håller grid-anslutning kontinuerligt.  
Brain-stall får ALDRIG resultera i off_grid eller backup-isolation.

### INV-4: BACKUP PORT ALLTID REDO
EPS/backup-port ska vara konfigurerad och aktiverad på båda invertrar.  
Brain-stall får inte deaktivera detta.

---

## C7–C10: Min-Active Keep-Alive Conditions

Kontrolleras periodiskt (var 30s) av `safety_net_monitor()` i koordinatorn.

### C7: Inverter mode = off_grid
**Trigger:** Någon inverter rapporterar operation_mode = `off_grid` eller `backup-only`  
**Corrective action:** Sätt till `general` inom 30s  
**Alarm:** Slack-notis + ERROR-log  
**Rationale:** off_grid → AC-out till huset försvinner

### C8: grid_export_limit_switch = off
**Trigger:** `switch.goodwe_*_grid_export_limit` = `off` (någon inverter)  
**Corrective action:** Sätt till `on`  
**Alarm:** WARNING-log (ej Slack om ej persistent >2 min)  
**Rationale:** Export-switch off kan förhindra grid-tied operation

### C9: bat_soc < backup_floor AND inverter ej i charge_pv
**Trigger:** `sensor.v6_battery_soc_avg` < `input_number.carma_min_backup_floor_pct`  
         AND ingen inverter är i `charge_pv`/`eco` (passivt laddläge)  
**Corrective action:** Sätt ledig inverter till `eco` (PV-absorb mode)  
**Alarm:** WARNING-log  
**Rationale:** Backup-reserven måste laddas upp om den tömts

### C10: Brain stale > 90s
**Trigger:** Koordinatorn har ej kört en full update-cykel på >90s  
**Corrective action:**
1. Alla invertrar → `general` (passiv produktion, backup-redo)
2. EMS mode → `auto` (inverter-default balansering)
3. EMS power limit → `0` (passiv, inga force-targets)
4. EPS/backup-port: verifiera aktiverad (passiv check)
5. Slack-alarm: "⚠️ CARMA Brain stall — safety fallback aktiv"  
**Rationale:** Brain-crash → huset ska fortfarande fungera normalt

---

## Konfigurations-defaults (keep-alive)

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `inverter_default_mode` | `general` | Fallback-läge vid Brain-stall |
| `ems_mode_default_brain_stall` | `auto` | Inverter-default vid stall |
| `ems_power_limit_default` | `0` | Passiv (ingen force-laddning) |
| `grid_export_limit_switch` | `on` | Alltid på |
| `min_backup_floor_pct` | `10` | Börje-tunable (5–30%) |

---

## Vad Safety-net INTE ansvarar för

- Kostnadsoptimering
- Strategival (NIGHT_CHARGE, PV_SURPLUS etc.)
- EV-laddningsbeslut
- Pool/VP-styrning (Shelly = HARD INVARIANT: alltid på)
- Fördelning mellan batteribankerna (utöver inga cross-charge >14A)

---

## Implementation checklist

- [ ] `const.py`: Lägg till `FORBIDDEN_INVERTER_MODES`, `DEFAULT_MIN_BACKUP_FLOOR_PCT`
- [ ] `safety_guard.py`: `check_inverter_mode(mode)` — blockerar FORBIDDEN_MODES
- [ ] koordinatorn: `safety_net_monitor()` — C7/C8/C9/C10 periodisk check
- [ ] `configuration.yaml`/HA: `input_number.carma_min_backup_floor_pct`
- [ ] Tests: `test_safety_net_min_active.py` — täcker C7–C10 + FORBIDDEN_MODES

