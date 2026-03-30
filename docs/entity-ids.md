# CARMA Box — Entity ID Reference

**Domain:** `carmabox`
**Platform:** `sensor`
**Unique ID pattern:** `carmabox_<key>`

---

## Sensorer (skapade av integrationen)

### Huvudsensorer (28 st)

| Entity ID | Beskrivning | Enhet |
|-----------|-------------|-------|
| `sensor.carmabox_plan_accuracy` | Plan accuracy — hur nära verkligheten vs plan | % |
| `sensor.carmabox_decision` | Aktuellt beslut med resonemang | text |
| `sensor.carmabox_plan_status` | Planstatus (charging_pv, discharging, standby, idle) | text |
| `sensor.carmabox_rules` | Aktiv regel med tabell över alla 7 optimeringsregler | text |
| `sensor.carmabox_target_kw` | Peak shaving target | kW |
| `sensor.carmabox_savings_month` | Månadens besparingar | kr |
| `sensor.carmabox_battery_soc` | Total batteri-SoC | % |
| `sensor.carmabox_grid_import` | Aktuell nätimport | kW |
| `sensor.carmabox_ev_soc` | EV state of charge | % |
| `sensor.carmabox_battery_efficiency` | Batteri köp/sälj-ratio | x |
| `sensor.carmabox_optimization_score` | CARMA vs native peak shaving | % |
| `sensor.carmabox_grid_charge_efficiency` | Nätladdning priseffektivitet | % |
| `sensor.carmabox_ellevio_realtime` | Rullande viktat timmedelvärde | kW |
| `sensor.carmabox_shadow` | Shadow mode jämförelse med v6 | text |
| `sensor.carmabox_status` | Systemhälsa — transparenssensor | text |
| `sensor.carmabox_plan_score` | Plan score — daglig accuracy | % |
| `sensor.carmabox_household_insights` | Månatlig benchmark mot liknande hushåll | text |
| `sensor.carmabox_daily_insight` | Daglig insikt: max kW, kostnad, rekommendationer | text |
| `sensor.carmabox_rule_flow` | Aktiv regelflödes-spårning | text |
| `sensor.carmabox_energy_ledger` | Daglig energibok med batteribesparingar | text |
| `sensor.carmabox_scheduler_last_breach` | Senaste peak breach-detaljer | text |
| `sensor.carmabox_scheduler_breach_count_month` | Månadsvis antal breach | st |
| `sensor.carmabox_scheduler_24h_plan` | 24h scheduler-plan | text |
| `sensor.carmabox_scheduler_ev_next_full_charge` | Nästa EV-fullladdning | datum |
| `sensor.carmabox_battery_idle_today` | Batteriets idle-tid idag | min |
| `sensor.carmabox_battery_utilization_score` | Batteriutnyttjande-poäng | % |
| `sensor.carmabox_breach_monitor_projected` | Projicerad mätaravläsning | kW |
| `sensor.carmabox_breach_monitor_pct` | Breach monitor procent med load shed | % |

### Appliance-sensorer (dynamiska, per kategori)

| Entity ID | Beskrivning |
|-----------|-------------|
| `sensor.carmabox_appliance_laundry` | Förbrukning Vitvaror |
| `sensor.carmabox_appliance_heating` | Förbrukning Värme/VP |
| `sensor.carmabox_appliance_pool` | Förbrukning Pool |
| `sensor.carmabox_appliance_miner` | Förbrukning Miner |
| `sensor.carmabox_appliance_lighting` | Förbrukning Belysning |
| `sensor.carmabox_appliance_ups` | Förbrukning UPS |
| `sensor.carmabox_appliance_other` | Förbrukning Övrigt |

---

## Input-helpers (refererade, EJ skapade av integrationen)

| Entity ID | Typ | Användning |
|-----------|-----|------------|
| `input_number.carma_ev_last_known_soc` | input_number | Seed för EV SoC-prediktion |
| `input_number.v6_plan_horizon_h` | input_number | Planeringshorisont (24-168h) |
| `input_text.v6_battery_plan` | input_text | Multi-day batteriplan |

---

## Externa entiteter (läses av integrationen)

### Solcast PV-prognos
- `sensor.solcast_pv_forecast_forecast_today`
- `sensor.solcast_pv_forecast_forecast_tomorrow`

### Ellevio Peak Shaving
- `sensor.ellevio_viktad_timmedel_pagaende` — Rullande viktat timmedel
- `sensor.ellevio_viktad_prognos_timmedel` — Timprognos
- `sensor.ellevio_dagens_max` — Dagens max

### Tempest Väder
- `sensor.tempest_solar_radiation`
- `sensor.tempest_pressure`
- `sensor.tempest_temperature`
- `sensor.tempest_illuminance`
- `sensor.tempest_wind_speed`
- `sensor.tempest_wind_gust`

### GoodWe Batteri/PV (per batteri)
- `sensor.goodwe_battery_min_cell_temperature_<prefix>`
- `sensor.goodwe_battery_power_<prefix>`
- `switch.goodwe_fast_charging_switch_<prefix>`
- `select.goodwe_<name>_ems_mode`
- `number.goodwe_<prefix>_ems_power_limit`

### Easee EV-laddare
- `sensor.easee_home_12840_status`
- `switch.easee_is_enabled`
- `button.easee_home_12840_override_schedule`

### Nordpool Elpriser
- `sensor.nordpool_kwh_se3_sek_3_10_025`

### Nät/Hus
- `sensor.house_grid_power`

### Övrigt
- `climate.kontor_ac`
