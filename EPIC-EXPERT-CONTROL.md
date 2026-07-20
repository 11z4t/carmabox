# EPIC: Expert-Level GoodWe + Easee Control

**Goal:** Transform CARMA Box from "functional" to "expert-level" battery and EV management
**Measurable:** 901 QC approval on each story, +15% battery utilization, zero Ellevio breaches

---

## Stories (Priority Order)

### EXP-01: Shelly Pro 3EM som primär EV-effektsensor ✅ KLAR (mergad main 2026-03-31, commit c4faa31)
**Value:** Snabbare Grid Guard feedback (1s vs 15-30s Easee), exakt per-fas mätning
**AC:**
- [x] EaseeAdapter har property `shelly_power_w` som läser Shelly Pro 3EM total power
- [x] EaseeAdapter har property `shelly_phase_powers` som returnerar per-fas effekt (A, B, C)
- [x] `power_w` property prioriterar Shelly > Easee sensor (med fallback)
- [x] Tester: 12 tester (över kravet 5+) inkl fallback, per-fas, stale data
**DoD:** Commit, push, 901 QC PASS, alla tester gröna — verifierad klar 2026-07-19/20, checkboxen var bara inaktuell

### EXP-02: GoodWe BMS-strömgräns awareness ✅ KLAR (mergad main 2026-03-31, commit 704795a)
**Value:** Korrekt urladdningsallokering vid kall temp (BMS begränsar till 7A vid 5°C)
**AC:**
- [x] GoodWeAdapter har property `bms_charge_limit_a` och `bms_discharge_limit_a`
- [x] `max_discharge_w` property beräknar: `bms_discharge_limit_a * voltage`
- [x] battery_balancer respekterar per-batteri max discharge W
- [x] Tester: 14 tester (över kravet 5+) inkl cold derating, asymmetrisk allokering
**DoD:** Commit, push, 901 QC PASS — verifierad klar 2026-07-19/20, checkboxen var bara inaktuell

### EXP-03: Reaktiv urladdning via peak_shaving_power_limit 🔄 VÄNTAR 901 QC (branch feat/exp-03-peak-shaving-reactive, commit be319f1, 2026-07-20)
**Value:** ±0.5 kW bättre grid-precision, automatisk kompensation vid lastsvängar
**AC:**
- [x] GoodWeAdapter har `set_peak_shaving_limit(watts)` som skriver register 47542
- [x] Coordinator sätter peak_shaving_power_limit = actual_grid + target_headroom varje cykel
- [x] Urladdning justeras automatiskt när huset drar mer/mindre
- [x] Safety: peak_shaving_power_limit clampad 0-10000W
- [x] Tester: 29 tester (över kravet 8+) inkl clamp, reaktiv justering, fallback
**DoD:** Commit ✅, push ✅, 901 QC PASS ⏳ väntar

### EXP-04: EV ramp-steg enforcement (6→8→10A) 🔄 VÄNTAR 901 QC (branch feat/exp-04-ev-ramp-steps, commit 028c6f2, 2026-07-20)
**Value:** Förhindrar strömspike, skyddar Ellevio timmedel vid uppramping
**AC:**
- [x] `_cmd_ev_adjust()` följer EV_RAMP_STEPS steg-för-steg (kärnlogik redan mergad mars 2026)
- [x] Max 1 steg per EV_RAMP_INTERVAL_S (5 min)
- [x] Nedramping: direkt till target (inget behov av stegvis)
- [x] Tester: lucka hittad (interval-gating otestad) — 3 nya tester tillagda 2026-07-20
**DoD:** Commit ✅, push ✅, 901 QC PASS ⏳ väntar (inkl. beslut om repo-brett trasigt pre-commit-hook)

### EXP-05: Reason-for-no-current monitoring + auto-recovery 🔄 VÄNTAR 901 QC (branch feat/exp-05-ev-block-recovery, commit 696fcc3, 2026-07-20)
**Value:** Automatisk recovery vid Easee-block (waiting_in_fully, car_not_charging)
**AC:**
- [x] Coordinator loggar reason_for_no_current varje cykel vid EV pluggad
- [x] Om reason=51 (WaitingInFully): höj max_charger_limit till 10A + resume — BUGG hittad och fixad (var omkastad med reason=6)
- [x] Om reason=6: re-init + override_schedule — BUGG hittad och fixad
- [x] Sensor `carmabox_ev_block_reason` exponerar current reason
- [x] Tester: 19 tester (över kravet 5+) per reason-kod + backoff/retry/cooldown
**DoD:** Commit ✅, push ✅, 901 QC PASS ⏳ väntar

### EXP-06: GoodWe SoH monitoring + derating
**Value:** Batterilivslängd skyddas, degradering synliggörs
**AC:**
- [ ] GoodWeAdapter har property `soh_pct` (läser sensor)
- [ ] Om SoH < 80%: höj min_soc med 5%, logga varning
- [ ] Om SoH < 70%: höj min_soc med 10%, Slack-alert
- [ ] Sensor `carmabox_battery_soh` exponerar per-batteri SoH
- [ ] Tester: 5+ tester inkl derating, alert-tröskel
**DoD:** Commit, push, 901 QC PASS

### EXP-07: Cold-lock discharge blocking
**Value:** Förhindrar urladdning vid extremkyla (< 0°C) som skadar celler
**AC:**
- [ ] Om cell_temp < TEMPERATURE_MIN_DISCHARGE_C (0°C): blockera discharge
- [ ] Om cell_temp < COLD_TEMP_THRESHOLD_C (4°C): reducera max discharge 50%
- [ ] battery_balancer skalar allokering baserat på temp
- [ ] Tester: 5+ tester
**DoD:** Commit, push, 901 QC PASS

### EXP-08: GoodWe write verification
**Value:** Detekterar misslyckade Modbus-skrivningar inom 2s istf 30s
**AC:**
- [ ] `set_ems_mode()` läser tillbaka efter 1s, verifierar korrekt mode
- [ ] `set_discharge_limit()` läser tillbaka, verifierar inom 10%
- [ ] Om verify fail: retry + logga WARNING
- [ ] Tester: 5+ tester inkl verify-fail + retry
**DoD:** Commit, push, 901 QC PASS

### EXP-09: Easee reboot detection + auto re-init
**Value:** Easee firmware-reboot nollställer limits → auto-recovery
**AC:**
- [ ] Detektera max_charger_limit < 10A (indikerar reboot/reset)
- [ ] Auto-trigger ensure_initialized(force=True)
- [ ] Logga event + Slack om frekventa reboots (>3/dag)
- [ ] Tester: 4+ tester
**DoD:** Commit, push, 901 QC PASS

### EXP-10: Cable disconnect alert + EV state tracking
**Value:** Förhindrar "laddar luft" + synliggör EV-anslutningsstatus
**AC:**
- [ ] Spåra cable_locked state change under pågående laddning
- [ ] Om cable_locked false→true under charge: logga unexpected disconnect
- [ ] Sensor `carmabox_ev_connection_state` (connected/charging/disconnected/error)
- [ ] Tester: 4+ tester
**DoD:** Commit, push, 901 QC PASS

---

## Mätbar framgång
- Alla 10 stories QC-godkända av 901
- Testtäckning > 90% (CI-gate)
- Grid Guard precision: ±0.3 kW (mätt via timmedel)
- Battery utilization: idle < 2h/dag (mätt via sensor)
- EV charging: 0 missade nattladdningar/vecka
