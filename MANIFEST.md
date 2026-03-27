# CARMA Box — MANIFEST

**C**onnected **A**utomated **R**esource **M**anagement **A**dvisor

Allt i detta manifest är lag. CARMA Box-koden MÅSTE följa detta manifest.
Vid konflikt mellan kod och manifest gäller manifestet.

---

## 1. LAGAR (prioritetsordning, 1 = högst)

### LAG 1: Ellevio timmedelvärde får ALDRIG överstiga tak

Ellevio mäter **timmedelvärde** — genomsnittlig effekt per timme.
Inte momentan effekt. Det innebär att en kort spike kan kompenseras
om resten av timmen hålls låg. Men det innebär också att skadan från
en okontrollerad period (t.ex. 10 min med 10 kW) sprids över hela timmen.

```
timmedel = Σ(grid_kw × Δt) / 60 min

projicerat_timmedel = (ackumulerad_kwh + kvarvarande_min × nuvarande_kw) / 60

projicerat_timmedel ≤ ellevio_tak_kw × grid_guard_margin
```

Ellevio har ETT tak med nattvikt:

```
ellevio_viktat_timmedel = faktiskt_timmedel × vikt(timme)

dag  (06-22): vikt = 1.0 → max 2.0 kW faktiskt
natt (22-06): vikt = 0.5 → max 4.0 kW faktiskt (= 2.0 kW viktat)

Gränsen är ALLTID: ellevio_viktat_timmedel ≤ ellevio_tak_kw
```

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `ellevio_tak_kw` | 2.0 | Ellevio viktat timmedel-tak (kW) |
| `ellevio_night_weight` | 0.5 | Natt-vikt (22-06) |
| `grid_guard_margin` | 0.85 | Agera vid X% av tak |

**GridGuard beräknar varje 30s-cykel:**
1. Läs `sensor.ellevio_viktad_timmedel_pagaende` (rullande)
2. Projicera: om nuvarande effekt fortsätter → var landar timmedelvärdet?
3. Om projicerat > tak × margin → agera:
   a. Sänk EV-amps (inte nödvändigtvis pausa — sänk till nivå som håller taket)
   b. Öka batteriurladdning
   c. Om fortfarande över → pausa EV helt
   d. Om fortfarande över → pausa miner/VP

**Kompensation:** Om timmedel redan lågt (t.ex. 1.5 kW efter 40 min)
kan en kort spike tillåtas utan åtgärd om projicerat fortfarande under tak.
Detta ger flexibilitet för disk/torktumlare utan onödig EV-paus.

**Ingen annan lag, regel eller plan får bryta denna lag.**

### LAG 2: Batterierna ska användas aktivt

Stillastående batterier = bortkastade pengar. Batterier ska:
- Urladda för att stödja huslast och EV-laddning
- Ladda vid billig el eller PV-överskott
- Tömmas till min_soc innan sol fyller dem gratis

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `battery_min_soc` | 15 | Absolut golv (GoodWe cutoff 10% + 5% marginal) |
| `battery_idle_max_hours` | 4 | Max timmar idle innan varning |

### LAG 3: EV ska nå minst target SoC VARJE dag

EV ska vara redo för dagligt bruk. Minst 75% SoC kl 06 varje morgon.
Detta är ett DAGLIGT mål — inte "om möjligt" utan "alltid".

CARMA Box MÅSTE planera bakåt från avresetid och säkerställa att
tillräckligt med laddtimmar allokeras, med hänsyn till LAG 1.

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `ev_morning_target_soc` | 75 | Minsta SoC vid avresetid |
| `ev_departure_hour` | 6 | Avresetid (timme, 0-23) |
| `ev_full_charge_interval_days` | 7 | Max dagar mellan 100% laddning |
| `ev_phase_count` | 3 | Antal faser (1 eller 3) |
| `ev_min_amps` | 6 | Lägsta laddström (Easee kräver 6) |
| `ev_max_amps` | 16 | Högsta laddström |

**EV-effekt:** `ev_amps × 230V × ev_phase_count`
- 6A 1-fas = 1.38 kW
- 6A 3-fas = 4.14 kW

**CARMA Box MÅSTE räkna med korrekt fasantal.**

### LAG 4: Minimera export — maximera egenkonsumtion

Egenproducerad solel ska konsumeras lokalt, INTE exporteras.
Export är ABSOLUT sista handsvalet.

**Effektförbrukare (kända av CARMA Box):**

| Förbrukare | Typ | Typisk effekt | Styrbar? | Sensor |
|-----------|-----|---------------|----------|--------|
| Batteri (laddning) | Styrbar | 0-3 kW per inverter | Ja — EMS mode | adapter |
| EV (laddning) | Styrbar | 1.4-11 kW (1-3fas) | Ja — amps | adapter |
| Miner | Styrbar | ~0.5 kW | Ja — on/off | switch |
| VP kontor | Delvis styrbar | 0.5-2 kW | Ja — setpoint/mode | climate |
| VP pool | Delvis styrbar | 1-3 kW | Ja — on/off | switch |
| Elvärmare pool | Styrbar | 2-6 kW | Ja — on/off | switch |
| Tvättmaskin | Ej styrbar | 0.5-2 kW | Nej — detektera | sensor |
| Torktumlare | Ej styrbar | 1-3 kW | Nej — detektera | sensor |
| Diskmaskin | Ej styrbar | 1-2 kW | Nej — detektera | sensor |
| Hus (bas) | Ej styrbar | 1-2 kW | Nej — mäta | sensor |

**Prioritetsordning vid PV-överskott:**

```
1. EV (om ansluten och inte full) — HÖGST vid PV-överskott
2. Batteri (om inte fullt)
3. VP kontor (boost +2°C vid överskott)
4. VP pool (om konfigurerad)
5. Miner (om konfigurerad)
6. Elvärmare pool (om konfigurerad)
7. Export (ABSOLUT sista utväg)
```

**Prioritetsordning vid effektbrist (sänk i omvänd ordning):**

```
1. Stäng elvärmare pool
2. Stäng miner
3. Stäng VP pool
4. Sänk VP kontor (ta bort boost)
5. Sänk EV amps / pausa EV
6. Minska batteriurladdning
7. ALDRIG stäng av hus/vitvaror (ej styrbara)
```

**Vitvaror (tvätt, tork, disk):** Kan INTE styras av CARMA Box.
De körs när de körs. CARMA Box detekterar dem och kompenserar
(sänk EV/öka urladdning). Vid bra PV-förhållanden kan CARMA Box
**notifiera** användaren att det är bra tid att starta vitvaror.

**Dynamisk justering FÖRE ny förbrukare:**

Innan en ny förbrukare med lägre prio startas, ska CARMA Box
försöka öka effekten på redan aktiva variabla förbrukare:

```
Överskott ökar med 500W:
  EV laddar redan 6A → kan vi öka till 8A? (+1380W 3-fas)
    → 500W < 1380W → kan inte öka helt, men kanske 7A?
    → Easee stödjer bara heltals-amps → kan inte öka till 7A
  Batteri laddar redan 2 kW → kan vi öka till 2.5 kW?
    → Ja! Öka batteri +500W
    → Överskott = 0W, ingen ny förbrukare behövs

Överskott ökar med 2000W:
  EV laddar 6A → öka till 8A (+1380W 3-fas) → kvar 620W
  Batteri laddar 2 kW → öka till 2.6 kW (+620W) → kvar 0W
  → Ingen ny förbrukare behövs
```

**Ordning: öka befintlig > starta ny > exportera**

**Förbrukarlistan är parameterstyrd.** Nya förbrukare kan läggas till
och prioritetsordning ändras via config:

```yaml
surplus_chain:
  - id: battery
    name: Batteri
    priority: 1
    min_w: 300
    max_w: 6000
    type: variable    # variable | fixed | on_off
    entity: (adapter)
  - id: ev
    name: EV
    priority: 2
    min_w: 1380       # 6A 1-fas, eller 4140 vid 3-fas
    max_w: 11040      # 16A 3-fas
    type: variable
    entity: (adapter)
  - id: vp_kontor
    name: VP Kontor
    priority: 3
    min_w: 500
    max_w: 2000
    type: variable
    entity: climate.kontor_ac
  - id: miner
    name: Miner
    priority: 6
    min_w: 400
    max_w: 500
    type: on_off
    entity: switch.miner
```

**Knapsack-algoritm — minimera export FÖRST, sedan prioritet:**

Det överordnade målet är **0 W export**. Prioritetslistan avgör
ordningen när flera förbrukare får plats, men en lågprio-förbrukare
som KAN äta överskott är ALLTID bättre än export.

```
Regel: fyll_det_som_får_plats() FÖRE följ_priolista()
```

Förbrukare med högre prioritet ska föredras, MEN om den inte
FÅR PLATS (överskott < min_w) ska CARMA Box välja nästa som FÅR plats.

```
Exempel: 700W överskott
  EV (prio 2): min_w=4140 (3-fas) → FÅR INTE PLATS
  VP (prio 3): min_w=500 → får plats → STARTA VP
  Miner (prio 6): min_w=400 → 700-500=200W kvar → FÅR INTE PLATS
  → Resultat: VP körs, 200W exporteras

Exempel: 700W överskott, ingen VP
  EV (prio 2): min_w=4140 → FÅR INTE PLATS
  Miner (prio 6): min_w=400 → får plats → STARTA MINER
  → Resultat: Miner körs, 200W exporteras

Exempel: överskott ökar till 4500W, miner kör (500W)
  Tillgängligt med miner stoppad: 4500W
  EV (prio 2): min_w=4140 → FÅR PLATS NU
  → STOPPA miner, STARTA EV
  → Resultat: EV laddar, 360W exporteras
  → Miner kan INTE köras samtidigt (4140+500=4640 > 4500)
```

**Algoritm (varje 30s-cykel):**

```python
def allocate_surplus(surplus_w, consumers):
    """Knapsack: fyll förbrukare som får plats, prioritet först."""
    # Sortera efter prioritet (lägst nummer = högst prio)
    consumers = sorted(consumers, key=lambda c: c.priority)

    allocated = []
    remaining = surplus_w

    # Pass 1: Försök allokera i prioritetsordning
    for c in consumers:
        if c.is_running:
            # Redan igång — behåll, justera om variabel
            if c.type == 'variable':
                c.target_w = min(c.max_w, remaining + c.current_w)
            remaining -= c.current_w
            allocated.append(c)
            continue

        if remaining >= c.min_w:
            # Får plats — allokera
            alloc_w = min(c.max_w, remaining)
            c.target_w = alloc_w
            remaining -= alloc_w
            allocated.append(c)

    # Pass 2: Om det finns remaining och lägre-prio-förbrukare
    # som får plats, fyll dem
    for c in consumers:
        if c not in allocated and remaining >= c.min_w:
            alloc_w = min(c.max_w, remaining)
            c.target_w = alloc_w
            remaining -= alloc_w
            allocated.append(c)

    # Pass 3: Bump check — kan vi stoppa en lågprio-förbrukare
    # för att ge plats åt en högprio som nu FÅR plats?
    for high in consumers:
        if high in allocated:
            continue
        # Kan vi frigöra tillräckligt genom att stoppa lågprio?
        freeable = sum(
            c.current_w for c in allocated
            if c.priority > high.priority
        )
        if remaining + freeable >= high.min_w:
            # Stoppa lågprio, starta högprio
            for low in sorted(allocated, key=lambda c: -c.priority):
                if low.priority > high.priority:
                    remaining += low.current_w
                    low.target_w = 0
                    allocated.remove(low)
                    if remaining >= high.min_w:
                        break
            high.target_w = min(high.max_w, remaining)
            remaining -= high.target_w
            allocated.append(high)

    return allocated, remaining  # remaining = export (mål: 0)
```

**VIKTIGT:** Denna algoritm körs VARJE 30s-cykel, inte bara vid förändring.
PV-produktion varierar konstant → surplus ändras → omallokering krävs.

Varje cykel utvärderas:
- Kan en högprio-förbrukare startas om en lågprio-förbrukare stoppas?
- Kan en variabel förbrukare (EV, VP) justeras upp/ned?
- Har överskottet ändrats tillräckligt för att motivera byte?

Hysteresregler (undvika oscillation):
- Starta förbrukare: överskott ≥ min_w i `surplus_start_delay_s` (60s)
- Stoppa förbrukare: överskott < min_w i `surplus_stop_delay_s` (180s)
- Bump (byt lågprio→högprio): överskott+lågprio ≥ högprio.min_w i 60s

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `surplus_start_delay_s` | 60 | Vänta innan start (undvik korta spikar) |
| `surplus_stop_delay_s` | 180 | Vänta innan stopp (undvik oscillation) |
| `surplus_eval_interval_s` | 30 | Omvärderingsintervall |

CARMA Box ska sträva efter att exportera **0 kWh** om lokal användning finns.

### LAG 5: Laddning ska ske till lägsta möjliga elpris och effektmedel

All laddning (batteri + EV) ska optimeras för:
1. **Lägst elpris** — ladda under billiga timmar (Nordpool)
2. **Lägst effektmedel** — sprida ut laddning så att timmedelvärdet aldrig toppar

**Nattladdningsprioritering:**

```
Normal natt (EV behöver inte 100%):
  Batteri har HÖGRE prio än EV
  → Ladda batteri först (billigaste timmarna)
  → EV laddar med det som återstår under taket

100%-laddningsnatt (EV behöver full inom 7-dygnsperioden):
  EV har HÖGRE prio än batteri
  → EV laddar först, batteri laddar om headroom finns
```

Rationale: Batterier behövs nästa dag för effektutjämning (LAG 1).
EV klarar sig på 75% dagligen. Men var 7:e dag behöver EV nå 100%
för BMS-kalibrering — då prioriteras EV.

```
Laddning batteri: bara vid pris < grid_charge_price_threshold
Laddning EV: sprida över natt, undvika topptimmar
Aldrig nätladda batteri vid pris > median
```

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `grid_charge_price_threshold` | 15 | Max pris (öre) för nätladdning batteri |
| `grid_charge_max_soc` | 90 | Max SoC vid nätladdning |

### LAG 6: Urladdning ska främst minska effektmedel, i andra hand elkostnad

Urladdning har TVÅ syften, i denna ordning:

1. **Minska effektmedel** — täcka huslast + EV så att grid-import hålls
   under Ellevio-tak. Detta är PRIMÄRT. Urladdning sker ALLTID
   om alternativet är att bryta LAG 1.

2. **Minska elkostnad** — urladda vid dyrt elpris för att undvika
   att köpa dyr el. Detta är SEKUNDÄRT. Sker bara om LAG 1 inte
   kräver urladdning och priset motiverar det.

```
om grid_import > tak_kw × margin:
    → URLADDA (LAG 1 kräver det, oavsett pris)

om grid_import < tak_kw OCH pris > discharge_price_threshold:
    → URLADDA (prisoptimering, sekundärt)

om grid_import < tak_kw OCH pris < discharge_price_threshold:
    → IDLE (spara batteri)
```

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `discharge_price_threshold_factor` | 0.9 | Urladda vid pris > median × faktor |

### LAG 7: Sol- och säsongsmedvetenhet

Urladdningsstrategi beror på om solen kan fylla batterierna:

| Solprognos imorgon | Urladdningsstrategi | Rationale |
|--------------------|---------------------|-----------|
| > 25 kWh (stark sol) | 2 kW natt → min_soc vid soluppgång | Sol fyller gratis |
| 15-25 kWh (måttlig) | 1 kW natt → 30% vid soluppgång | Sol fyller delvis |
| < 15 kWh (svag/vinter) | 0.5 kW eller idle → 50%+ vid soluppgång | Spara batteri |

**Vinter-logik:** Om PV-prognos < daglig konsumtion (batteriet fylls INTE av sol):
- Analysera Nordpool-priser: finns billiga natt-timmar (<15 öre)?
- Om ja → nätladda till grid_charge_max_soc (90%) nattetid
- Om nej → spara batteri, urladda bara vid toppriser

```
om pv_forecast_tomorrow < daily_consumption:
    # Vinter/mulet — sol räcker inte
    billigaste_timmar = sortera(nattpriser)[0:4]
    om billigaste < grid_charge_price_threshold:
        → nätladda under billiga timmar
        → urladda under dyra timmar (arbitrage)
    annars:
        → minimal urladdning, spara batteri
        → urladda BARA vid toppriser (>80 öre)
```

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `discharge_rate_solar_kw` | 2.0 | Urladdning natt vid stark sol |
| `discharge_rate_partial_kw` | 1.0 | Urladdning vid måttlig sol |
| `discharge_rate_winter_kw` | 0.5 | Urladdning vid svag sol/vinter |
| `solar_strong_threshold_kwh` | 25 | Stark sol-gräns |
| `solar_partial_threshold_kwh` | 15 | Måttlig sol-gräns |

---

## 2. TEKNISKA INVARIANTER (får ALDRIG brytas)

### INV-1: Aldrig EMS auto utan styrning
GoodWe i `auto` mode beslutar själv → okontrollerad urladdning till 0 kW grid.
**CARMA Box MÅSTE alltid sätta explicit EMS-mode + power limit.**

### INV-2: Aldrig korsladning
Ett batteri laddar + ett annat urladdar = korsladning = energiförlust.
**Detektera var 30s. Vid korsladning → tvinga båda till standby.**

### INV-3: Aldrig fast_charging utan explicit beslut
`fast_charging = ON` drar grid-import för att ladda batteri.
**Default = OFF. Får bara aktiveras av grid_charge-beslut vid billig el.**

### INV-4: Temperaturmedveten styrning
Batteri med cell-temp < `cold_lock_temp_c` (4°C):
- Blockera laddning (urladdning fungerar)
- Höj SoC-golv till `battery_min_soc_cold` (20%)
- BMS kan spärra vid kombinationen låg temp + låg SoC

**OBS:** Urladdning genererar värme → cell-temp stiger → min_soc sjunker.
CARMA Box MÅSTE dynamiskt korrigera effective_min_soc varje cykel:

```
effective_min_soc(battery_i) =
    battery_min_soc_cold (20%)  om cell_temp_i < cold_lock_temp_c (4°C)
    battery_min_soc (15%)       annars

available_kwh(battery_i) = max(0, (soc_i - effective_min_soc_i) / 100 × cap_i)
```

Urladdning → temp stiger → effective_min_soc sjunker → mer kapacitet tillgänglig.
Detta är en positiv feedback-loop som CARMA Box ska utnyttja.

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `cold_lock_temp_c` | 4.0 | Under → blockera laddning + höj min_soc |
| `battery_min_soc_cold` | 20 | SoC-golv vid kyla (<4°C) |

### INV-5: Aldrig bryta state vid HA restart
Alla tillstånd som påverkar batteristyrning MÅSTE persisteras.
Vid restart: GridGuard aktiveras OMEDELBART, innan plan genereras.
**Inga `initial:` på helpers. Restora från runtime store.**

### INV-6: Proportionell urladdning
Batterier med olika kapacitet MÅSTE urladda proportionellt:
```
andel_i = (soc_i - min_soc) × cap_i / Σ((soc_j - min_soc) × cap_j)
```
Alla ska nå min_soc **samtidigt**.
Omräknas varje cykel baserat på aktuell SoC.

### INV-7: Diskmaskin/vitvaror → pausa EV
Vid appliance_power > `appliance_threshold_w`:
1. Pausa EV omedelbart
2. Öka urladdning om möjligt (LAG 1 respekteras)
3. Återstarta EV när appliance klar + cooldown

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `appliance_threshold_w` | 500 | Tröskel för vitvarudetektering |
| `appliance_cooldown_min` | 5 | Väntetid efter vitvara innan EV återstartar |

---

## 3. PLANERINGSHORISONTER OCH REAKTIVITET

CARMA Box opererar på fyra tidsskalor samtidigt:

### Momentan reaktion (0-30s) — REFLEX
GridGuard. Ingen planering, ren reflex.
Reagerar på vad som händer NU. Övertrumfar allt.

```
Grid > tak → pausa EV / öka urladdning OMEDELBART
Korsladning → standby OMEDELBART
Sensor borta → fallback OMEDELBART
```

### Korttidsplan (0-4h) — TAKTISK
Detaljerad per 15 min. Styrande för executor.
Uppdateras vid avvikelse eller ny information.

```
Innehåll: exakt EV-amps, exakt urladdning per batteri,
          appliance-prediktion, timmedel-projicering
Datakällor: aktuell SoC, grid, priser (kända), disk-sannolikhet
Precision: hög — baserad på kända fakta
```

### Dygnsplan (4-24h) — STRATEGISK
Per timme. Sätter riktning för taktisk plan.
Genereras vid prisuppdatering (13:00 imorgon-priser) och var 60 min.

```
Innehåll: charge/discharge/idle per timme, EV-schema,
          sunrise/sunset-tider, nätladdningsfönster
Datakällor: Nordpool today+tomorrow, Solcast 24h, konsumtionsprofil
Precision: medel — priser kända, konsumtion predikterad
```

### Grovplan (24-72h) — VISIONÄR
Per 4-timmarsblock. Informerar strategisk plan.
Uppdateras dagligen.

```
Innehåll: sol-prognos per dag, pristendens, EV 100%-planering,
          säsongsanpassning, helg vs vardag
Datakällor: Solcast 3-dagars, Nordpool day-ahead (om tillgängligt),
           historisk konsumtion, väderprognos
Precision: låg — vägledande, inte styrande
```

### Fallback vid databrist

| Horisont | Om data saknas | Fallback |
|----------|----------------|----------|
| Momentan | Grid sensor borta | Last known + 10% margin |
| Taktisk | Priser saknas | Fallback-pris, konservativ plan |
| Strategisk | Nordpool tomorrow ej publicerat | Kopiera today-priser |
| Visionär | Solcast nere | Historiskt medel för månad/säsong |

### Hierarki vid konflikt

```
Momentan REFLEX övertrumfar alltid (LAG 1)
Taktisk plan styr executor var 30s
Strategisk plan informerar taktisk vid varje omplanering
Visionär plan informerar strategisk vid daglig planering

Om taktisk plan bryter LAG → momentan reflex korrigerar
Om strategisk plan bryter LAG → taktisk plan ignorerar den timmen
```

---

## 4. ARKITEKTUR — Lagerhierarki

```
Varje 30s-cykel:

┌─────────────────────────────────────────────┐
│ LAYER 0: GRID GUARD                         │
│ Läs grid import. Om > tak → NÖDÅTGÄRD.      │
│ VETO-rätt över alla andra lager.             │
│ Ingen dependency på plan eller adapters.     │
├─────────────────────────────────────────────┤
│ LAYER 1: STATE COLLECTOR                     │
│ Läs alla sensorer → CarmaboxState            │
│ Hantera unavailable med fallback             │
├─────────────────────────────────────────────┤
│ LAYER 2: PLANNER (var 5 min ELLER deviation) │
│ Nordpool + Solcast + consumption → 48h plan  │
│ Plan = styrdokument, inte prognos            │
├─────────────────────────────────────────────┤
│ LAYER 3: PLAN EXECUTOR                       │
│ Läs plan[current_hour] → verkställ           │
│ Planen styr, executor lyder                  │
│ GridGuard kan blockera → flagga för omplan   │
├─────────────────────────────────────────────┤
│ LAYER 4: PROPORTIONAL DISCHARGE              │
│ Fördela urladdning per batteri               │
│ Kapacitet × kvarvarande SoC                  │
│ Cold lock → omfördela                        │
├─────────────────────────────────────────────┤
│ LAYER 5: SURPLUS CHAIN                       │
│ EV (inom grid budget) → Miner → VP → Export  │
│ EV amps beräknas från grid headroom          │
├─────────────────────────────────────────────┤
│ LAYER 6: WATCHDOG                            │
│ Verifiera att verkligheten matchar beslut     │
│ Korrigera inom 30s                           │
└─────────────────────────────────────────────┘
```

### GridGuard detalj

```python
grid_kw = läs faktisk grid import
tak_kw = ellevio_tak_night_kw om natt, annars ellevio_tak_day_kw
headroom_kw = tak_kw × grid_guard_margin - grid_kw

om headroom_kw < 0:
    NÖDLÄGE:
    1. Pausa EV → headroom += ev_power
    2. Om fortfarande < 0: öka urladdning → headroom += extra_discharge
    3. Om fortfarande < 0: pausa miner/VP
    RETURNERA: blocked=True (skippa Layer 3-5)

RETURNERA: headroom_kw (tillgängligt för EV/laster)
```

### Plan Executor detalj

```python
planned = plan[current_hour]

om PV > 500W och batteri ej fullt:
    → charge_pv (fysik övertrumfar plan — sol MÅSTE fångas)

om planned.action == 'd':  # discharge
    watts = beräkna_behov(grid, target, ev_demand)
    → cmd_discharge_proportional(watts)

om planned.action == 'c':  # PV charge
    om exporterar → charge_pv
    annars → standby

om planned.action == 'g':  # grid charge
    om pris < threshold OCH grid headroom finns → grid_charge
    annars → standby

om planned.action == 'i':  # idle
    om grid > target × 1.05 → reaktiv urladdning
    annars → standby
```

### EV Controller detalj

```python
grid_budget_kw = headroom från GridGuard
max_amps = grid_budget_kw × 1000 / (230 × ev_phase_count)
max_amps = clamp(max_amps, 0, ev_max_amps)

om max_amps >= ev_min_amps:
    starta/justera EV till max_amps
annars:
    pausa EV
```

---

## 4. STARTUP-SEKVENS (HA restart)

```
1. Restora persistent state (runtime store)
2. GridGuard aktiveras OMEDELBART
   - Läs grid import
   - Om > tak → nödåtgärd
   - fast_charging = OFF på alla inverters
3. Samla sensordata (2-3 cykler = 60-90s)
   - Vänta tills battery SoC, grid, EV status är tillgängliga
4. Om plan finns i store och < 1h gammal → använd den
   Om inte → generera ny plan omedelbart
5. Normal drift
```

---

## 5. FAILURE MODES

| Fel | Åtgärd |
|-----|--------|
| Grid sensor unavailable | Använd senaste kända + 10% marginal |
| Battery SoC unavailable | Skippa urladdning, behåll senaste mode |
| EV SoC unavailable | Använd last_known med derating |
| Priser unavailable | Fallback-pris, logga varning |
| GoodWe service call fail | Retry 1x med 5s delay, sedan logga |
| Plan generation krasch | Behåll gamla planen, GridGuard aktiv |
| Coordinator krasch 10x | Markera unavailable, GridGuard fortsätter |
| Båda batterier cold-locked | Standby, ingen laddning, urladdning OK |
| EV charger offline | Logga, retry var 5 min |

---

## 6. PARAMETERLISTA (komplett)

Alla parametrar konfigureras via HA config flow (Options).
Defaults är dimensionerade för en typisk svensk villa med GoodWe + Easee.

### Grid/Ellevio
| Parameter | Default | Typ | Beskrivning |
|-----------|---------|-----|-------------|
| `ellevio_tak_kw` | 2.0 | float | Viktat timmedel-tak (kW), gäller hela dygnet |
| `ellevio_night_weight` | 0.5 | float | Natt-vikt (22-06). Faktiskt tak natt = tak/vikt = 4kW |
| `grid_guard_margin` | 0.85 | float | Agera vid X% av tak |
| `night_weight` | 0.5 | float | Ellevio natt-vikt |

### Batteri
| Parameter | Default | Typ | Beskrivning |
|-----------|---------|-----|-------------|
| `battery_1_kwh` | 15.0 | float | Batteri 1 kapacitet |
| `battery_2_kwh` | 5.0 | float | Batteri 2 kapacitet (0=inget) |
| `battery_min_soc` | 15 | int | SoC-golv vid normal temp (≥10°C) |
| `battery_min_soc_cold` | 20 | int | SoC-golv vid kyla (<10°C) — BMS kan spärra |
| `cold_discharge_temp_c` | 10.0 | float | Under → använd min_soc_cold |
| `cold_lock_temp_c` | 5.0 | float | Under → blockera laddning (urladdning OK) |
| `max_discharge_kw` | 5.0 | float | Max urladdning per inverter |

### EV
| Parameter | Default | Typ | Beskrivning |
|-----------|---------|-----|-------------|
| `ev_capacity_kwh` | 92 | float | EV batteri usable (XPENG G9 = 92 kWh) |
| `ev_phase_count` | 3 | int | 1 eller 3 faser |
| `ev_min_amps` | 6 | int | Lägsta ström |
| `ev_max_amps` | 16 | int | Högsta ström |
| `ev_morning_target_soc` | 75 | int | SoC-mål vid avresa |
| `ev_departure_hour` | 6 | int | Avresetimme |
| `ev_full_charge_interval_days` | 7 | int | Max dagar mellan 100% |

### Priser
| Parameter | Default | Typ | Beskrivning |
|-----------|---------|-----|-------------|
| `grid_charge_price_threshold` | 15 | float | Max pris (öre) nätladdning |
| `grid_charge_max_soc` | 90 | int | Max SoC nätladdning |
| `discharge_price_factor` | 0.9 | float | Urladda vid > median × faktor |
| `fallback_price_ore` | 100 | float | Pris om sensor saknas |

### Vitvaror
| Parameter | Default | Typ | Beskrivning |
|-----------|---------|-----|-------------|
| `appliance_threshold_w` | 500 | float | Tröskel för detektering |
| `appliance_cooldown_min` | 5 | int | Väntetid efter vitvara |

### Plan
| Parameter | Default | Typ | Beskrivning |
|-----------|---------|-----|-------------|
| `tactical_plan_hours` | 4 | int | Taktisk plan (detaljerad, 15 min) |
| `strategic_plan_hours` | 24 | int | Strategisk plan (per timme) |
| `vision_plan_hours` | 72 | int | Grovplan (per 4h-block) |
| `plan_interval_s` | 300 | int | Taktisk omplaneringsintervall |
| `strategic_plan_interval_s` | 3600 | int | Strategisk omplanering (1h) |
| `replan_deviation_pct` | 20 | int | Avvikelse som triggar omplan |
| `replan_deviation_cycles` | 3 | int | Antal cykler (×30s) innan omplan |

---

## 7. LAW GUARDIAN — Övervakning och Efterlevnad

CARMA Box STANNAR ALDRIG vid lagbrott. Den planerar om och korrigerar.

### 7.1 Lagövervakning (varje cykel)

Varje 30s-cykel kontrollerar Guardian alla 5 lagar:

```
för varje LAG:
    status = utvärdera_lag(state)
    om status == BROTT:
        1. LOGGA: tidpunkt, lag, faktiskt värde, gränsvärde, orsak
        2. KORRIGERA: omedelbar åtgärd (se LAG-specifik åtgärd)
        3. OMPLANERA: trigga _generate_plan() med constraint
        4. REGISTRERA: spara breach_record för ML-analys
    om status == VARNING (>85% av gräns):
        1. LOGGA: proaktiv varning
        2. FÖRBERED: beräkna korrigerande åtgärd redo att verkställa
```

### 7.2 Breach Record (lagbrott-dokumentation)

Varje lagbrott dokumenteras:

```python
BreachRecord:
    timestamp: datetime
    law: str           # "LAW_1_GRID" | "LAW_2_IDLE" | "LAW_3_EV" | ...
    actual_value: float # Vad som hände
    limit_value: float  # Vad gränsen var
    duration_s: int     # Hur länge brottet pågick
    root_cause: str     # Automatisk analys (t.ex. "disk_started_no_ev_pause")
    correction: str     # Vad CARMA Box gjorde
    prevented: bool     # Kunde det förebyggts?
    plan_at_time: dict   # Vad planen sa vid tidpunkten
    actual_at_time: dict # Vad som faktiskt hände
```

### 7.3 Omplanering vid lagbrott

När en lag bryts → CARMA Box planerar om OMEDELBART:

```
1. Identifiera vilken lag som bröts
2. Analysera root cause (vad orsakade brottet?)
3. Lägg till constraint i ny plan:
   - LAG 1 brott → sänk max_ev_amps i planen
   - LAG 2 brott → forcera discharge i nästa fönster
   - LAG 3 brott → öka EV-tid eller amps i planen
4. Generera ny plan med constraints
5. Verifiera att ny plan inte bryter andra lagar
6. Aktivera ny plan
```

Rate limit: max 1 omplanering per 5 minuter.

### 7.4 Eskalering

Om samma lag bryts > 3 gånger inom 1 timme:
1. Notifiera användaren (Slack/push)
2. Logga som KRITISK
3. Aktivera konservativ mode (alla laster minimum)

---

## 8. ML OCH AUTONOMITET

### 8.1 Prediktor — Vad CARMA Box lär sig

CARMA Box samlar data och bygger prediktionsmodeller:

| Modell | Input | Output | Användning |
|--------|-------|--------|-----------|
| Huskonsumtion | veckodag, timme, temp, månad | kW | Bättre planering |
| Vitvaror | veckodag, timme, historik | sannolikhet | Undvik EV-konflikt |
| Batteritemp | urladdning_w, utetemp, tid | °C/h | Förutsäg cold lock |
| EV-beteende | veckodag, SoC vid hemkomst | kWh behov | Anpassa nattplan |
| Planprecision | plan vs utfall per timme | avvikelse% | Förbättra planner |

### 8.2 Lärande Feedback-loop

```
Plan → Execution → Mätning → Jämförelse → Justering → Bättre Plan

Varje timme:
    planned_grid_kw = plan[hour].grid_kw
    actual_grid_kw  = uppmätt medelvärde
    error = actual - planned

    om |error| > 20%:
        registrera avvikelse med kontext:
        - Vilken åtgärd pågick
        - Vilka laster var aktiva
        - Vilken temp hade batterierna

    efter 7 dagar:
        analysera mönster:
        - "Tisdag kväll har konsumtionen alltid +30% vs plan"
        - "Disk startar oftast kl 21-22 på vardagar"
        - "Kontor-batteriet tappar 2% mer vid <5°C"

    justera modell:
        - Uppdatera konsumtionsprofil
        - Lägg till vitvaruprediktion i plan
        - Korrigera batterikapacitet vid kyla
```

### 8.3 Styrningslärande — Vad fungerade?

CARMA Box registrerar varje styrbeslut med utfall:

```python
DecisionOutcome:
    timestamp: datetime
    decision: str        # "discharge_2kw_proportional"
    context: dict        # grid_kw, soc, temp, price, ev_charging
    outcome: str         # "grid_under_target" | "grid_over_target"
    law_compliance: dict # {LAW_1: True, LAW_2: True, ...}
```

ML-modellen lär sig:
- Vilka styrningar som håller lagarna
- Vilka styrningar som BRYTER lagarna
- Optimala parametrar per scenario (tid, temp, SoC, pris)

### 8.4 Autonom Parameteranpassning

CARMA Box kan automatiskt justera parametrar inom säkra gränser:

| Parameter | Auto-range | Baserat på |
|-----------|-----------|------------|
| `discharge_price_factor` | 0.7-1.2 | Historisk besparingseffekt |
| Konsumtionsprofil | ±50% per timme | 7 dagars rullande medel |
| EV SoC vid hemkomst | Adapativt | 14 dagars historik |

**Säkerhet:** Auto-justering får ALDRIG ändra LAG-parametrar
(ellevio_tak, min_soc, cold_lock_temp). Bara optimerings-parametrar.

---

## 9. WORKFLOWS

### 9.1 Natt-workflow (22:00-06:00)

```
Mål: EV ≥ target SoC kl departure_hour
     Batterier → min_soc vid soluppgång (om sol imorgon)
     Grid timmedel ≤ ellevio_tak_night_kw

Triggers: 22:00 (start) | Kabel ansluten | Prisuppdatering

Steg:
1. Beräkna EV-behov: (target_soc - current_soc) × ev_capacity_kwh
2. Beräkna tillgänglig tid: departure_hour - now
3. Beräkna amps: ev_behov_kwh / tid_h / (230V × phase_count) × 1000
4. Clamp amps: max(ev_min_amps, min(amps, ev_max_amps))
5. Beräkna total last: hus_kw + ev_kw
6. Beräkna batteri-stöd: max(0, total_last - tak_kw + margin)
7. Fördela batteri proportionellt (INV-6)
8. Starta EV + urladdning
9. Loop varje 30s:
   a. GridGuard check
   b. Om disk → pausa EV
   c. Om SoC_i < effective_min_soc_i → stoppa batteri_i
   d. Om batteri_i tomt → omfördela till batteri_j
   e. Om EV klar → stoppa EV + minska urladdning
10. Kl departure_hour: stoppa EV
```

### 9.2 Dag-workflow (06:00-22:00)

```
Mål: Fånga all sol i batteri
     Urladda vid högt pris (> median × discharge_price_factor)
     Grid timmedel ≤ ellevio_tak_day_kw

Triggers: Soluppgång | Prisändring | SoC 100%

Steg:
1. Sol producerar → charge_pv (batterier först)
2. Batterier fulla → surplus chain: EV → Miner → VP → Export
3. Sol slutar → utvärdera: urladda vid dyrt, idle vid billigt
4. Kvällspeak (17-20): aggressiv urladdning om pris > threshold
5. 21:00: Nordpool-planner hämtar morgondagens priser
6. 21:30: Generera nattplan
```

### 9.3 Disk-workflow

```
Trigger: appliance_power > appliance_threshold_w

Steg:
1. Beräkna projicerat timmedel MED disk
2. Om projicerat < tak → TILLÅT (ingen åtgärd)
3. Om projicerat > tak:
   a. Pausa EV (om aktiv)
   b. Öka urladdning om batteri tillgängligt
4. Monitor disk var 30s
5. Disk klar (< 100W i appliance_cooldown_min):
   a. Återstarta EV (om den var aktiv)
   b. Återställ urladdning till planerat

OBS: Disk-workflow kan triggas UTAN att pausa EV om timmedelvärdet
fortfarande klarar sig (t.ex. disk kör 5 min mot slutet av en timme
där medelvärdet redan är lågt).
```

### 9.4 Omplanerings-workflow

```
Trigger: LAG-brott | Avvikelse > replan_deviation_pct i replan_deviation_cycles cykler

Steg:
1. Dokumentera avvikelse (BreachRecord)
2. Analysera orsak:
   - Oförutsedd last? → uppdatera konsumtionsprofil
   - Priser ändrades? → hämta nya priser
   - Batteri cold-locked? → justera effective_min_soc
   - EV ansluten/bortkopplad? → uppdatera EV-demand
3. Generera ny plan med uppdaterade constraints
4. Verifiera ny plan mot alla lagar
5. Aktivera ny plan
6. Logga omplanering med orsak
```

---

## 10. SJÄLVLÄKNING

### 10.1 Princip
CARMA Box ska ALDRIG stanna. Vid fel → degradera gracefully, inte krascha.

### 10.2 Självläkningsmekanismer

| Mekanism | Trigger | Åtgärd |
|----------|---------|--------|
| Sensor fallback | Unavailable >60s | Använd last_known + margin |
| Adapter retry | Service call fail | Retry 1x, 5s delay, backoff |
| Plan fallback | Generation krasch | Behåll gammal plan, GridGuard aktiv |
| State recovery | HA restart | Restora från persistent store |
| Mode correction | Batteri i oväntat mode | Korrigera till planerat mode |
| Crosscharge heal | Korsladdning detekterad | Tvinga standby, omplanera |
| Oscillation damping | >10 mode-ändringar/min | Rate limit, stabilisera |
| Circuit breaker | >5 consecutive errors | Pause 60s, sedan retry |
| Config reload | Attribut saknas (AttributeError) | getattr med default |

### 10.3 Degraderad drift

Om CARMA Box inte kan köra normal drift:

```
Nivå 1: Plan unavailable → GridGuard + reaktiv styrning
Nivå 2: Adapter offline → Standby, pausa EV, logga
Nivå 3: Coordinator krasch → Watchdog restartservice (systemd)
Nivå 4: HA nere → Extern watchdog (LXC 506) larmar
```

Vid varje nivå: GridGuard körs ALLTID om möjligt.

### 10.4 Post-incident analys (automatisk)

Efter varje LAG-brott eller degraderad drift:

```python
PostIncident:
    breach_records: list[BreachRecord]  # Alla brott under incidenten
    duration_s: int                     # Tid i degraderat läge
    root_cause: str                     # Automatisk klassificering
    corrective_actions: list[str]       # Vad CARMA Box gjorde
    preventive_actions: list[str]       # ML-förslag för att undvika i framtiden
    parameters_adjusted: dict           # Parametrar som auto-justerades
```

Lagras i SQLite. Kan skickas som rapport via Insight-mail.

---

## 11. MÄTBARA KONTROLLPUNKTER

### 11.1 Realtidskontrollpunkter (varje 30s-cykel)

Varje cykel MÅSTE verifiera dessa innan nästa cykel körs:

| ID | Kontrollpunkt | Mätning | Gräns | Vid brott |
|----|--------------|---------|-------|-----------|
| CP-1 | Grid timmedel projicering | `projected_timmedel_kw` | ≤ tak × margin | GridGuard REFLEX |
| CP-2 | Batteribalans | `abs(time_to_min_1 - time_to_min_2)` | < 30 min | Omfördela |
| CP-3 | EV SoC trajectory | `projected_soc_at_departure` | ≥ target_soc | Öka amps/tid |
| CP-4 | Korsladning | `bat1_charging AND bat2_discharging` | False | Tvinga standby |
| CP-5 | Urladdning levererar | `abs(actual_discharge - planned_discharge)` | < 20% | Korrigera EMS |
| CP-6 | Adapter-svar | `adapter_response_time_ms` | < 5000 | Retry/fallback |

### 11.2 Timkontrollpunkter

Varje hel timme kontrolleras:

| ID | Kontrollpunkt | Mätning | Gräns | Vid brott |
|----|--------------|---------|-------|-----------|
| HC-1 | Ellevio timmedel | `actual_timmedel` | ≤ tak | BreachRecord + omplan |
| HC-2 | Plan vs verklighet | `plan_accuracy_%` | ≥ 70% | Omplanera |
| HC-3 | Batteri idle-tid | `idle_minutes_this_hour` | < 45 min (om available > 1 kWh) | LAG 2 varning |
| HC-4 | EV progress | `soc_gained_vs_planned` | ≥ 80% av planerat | Justera amps |

### 11.3 Dygnkontrollpunkter

Kl 06:00 varje morgon:

| ID | Kontrollpunkt | Mätning | Gräns | Vid brott |
|----|--------------|---------|-------|-----------|
| DC-1 | EV SoC vid avresa | `ev_soc` | ≥ morning_target_soc | LAG 3 brott |
| DC-2 | Max timmedel natt | `max(timmedel[22:06])` | ≤ tak_night | LAG 1 brott |
| DC-3 | Max timmedel dag | `max(timmedel[06:22])` | ≤ tak_day | LAG 1 brott |
| DC-4 | Batteri utilization | `discharge_kwh / available_kwh` | ≥ 50% (sol-dag) | LAG 2 varning |
| DC-5 | Total besparing | `kostnad_utan - kostnad_med` | > 0 kr | Logga |
| DC-6 | Antal lagbrott | `count(breach_records)` | 0 | Analysera |
| DC-7 | Plan-precision medel | `avg(plan_accuracy)` | ≥ 70% | ML-justering |

---

## 12. TESTSTRATEGI

### 12.1 Testnivåer

```
Nivå 1: Unit tests        — ren Python, ingen HA, körs vid commit
Nivå 2: Integration tests — mockad HA, körs vid PR
Nivå 3: Scenario tests    — hela natt/dag-cykler simulerade
Nivå 4: Regression tests  — verkliga incidenter reproducerade
Nivå 5: Live validation   — kontrollpunkter mot produktion
```

### 12.2 Nivå 1 — Unit Tests (krav: 100% på ny kod)

Varje funktion testas isolerat. Minst dessa:

**GridGuard:**
```
test_grid_guard_under_tak          → no action
test_grid_guard_over_tak_ev_on     → pausa EV
test_grid_guard_over_tak_ev_off    → öka urladdning
test_grid_guard_projicering        → projicerad timmedel korrekt
test_grid_guard_spike_ok           → kort spike, medel under tak
test_grid_guard_spike_too_long     → lång spike, medel över tak
test_grid_guard_sensor_unavailable → fallback to last known
```

**Proportionell urladdning:**
```
test_proportional_75_25            → kontor 75%, förråd 25%
test_proportional_cold_lock        → allt till varmt batteri
test_proportional_one_at_min_soc   → allt till det andra
test_proportional_both_at_min_soc  → ingen urladdning
test_proportional_dynamic_update   → omräkning vid SoC-ändring
test_proportional_temp_rises       → min_soc sjunker, mer kapacitet
```

**EV Controller:**
```
test_ev_3phase_6a_power            → 4.14 kW (inte 1.38)
test_ev_budget_limited             → sänk amps vid låg headroom
test_ev_budget_zero                → pausa EV
test_ev_target_reached             → stoppa
test_ev_trajectory_behind          → öka amps
test_ev_departure_hour_stop        → stäng av kl 06
```

**Planner:**
```
test_plan_discharge_solar_day      → urladda 2 kW vid sol imorgon
test_plan_discharge_winter         → minimal urladdning vid mulet
test_plan_grid_charge_cheap        → nätladda vid <15 öre
test_plan_ev_fits_in_budget        → EV amps respekterar tak
test_plan_disk_predicted           → sänk EV under disk-timme
test_plan_replan_on_deviation      → ny plan vid 20% avvikelse
```

**Law Guardian:**
```
test_breach_record_created         → korrekt BreachRecord vid brott
test_breach_triggers_replan        → omplanering vid brott
test_breach_escalation             → notis vid 3+ brott/timme
test_no_false_breach               → spike under medel = inget brott
```

### 12.3 Nivå 3 — Scenario Tests (verkliga nattscenarion)

Varje scenario simulerar en hel natt med realistisk data:

**Scenario A: "Normal vinternatt med EV"**
```
Setup: SoC K=95% F=90%, EV=55%, sol imorgon=5 kWh, priser natt=60-80 öre
       Hus=1.7 kW, EV 6A 3-fas=4.14 kW, tak natt=4 kW
       Bat temp K=8°C F=12°C

Förväntat:
  - Urladdning ~0.5 kW (vinter, spara batteri)
  - EV laddar hela natten
  - Grid ≤ 4 kW varje timme
  - EV SoC kl 06 ≥ 75%
  - Batteri SoC kl 06 ≥ 50% (behövs imorgon)

Verifiering: alla HC-* och DC-* kontrollpunkter
```

**Scenario B: "Solig vårdag, aggressiv urladdning"**
```
Setup: SoC K=97% F=96%, EV=56%, sol imorgon=38 kWh, priser natt=40-70 öre
       Bat temp K=5.9°C F=10.7°C

Förväntat:
  - Urladdning 2 kW (sol fyller imorgon)
  - K: effective_min_soc=20% (kyla <4°C → nej, 5.9>4 → 15%)
  - Båda når 15% kl ~06:30 ±30 min SAMTIDIGT
  - EV ≥ 75% kl 06
  - Grid ≤ 4 kW ALLTID

Verifiering: CP-2 (balans), DC-1, DC-2
```

**Scenario C: "Diskmaskin mitt i natten"**
```
Setup: Scenario B + disk 2 kW startar kl 23:30, körs 90 min

Förväntat:
  - EV pausas inom 30s efter disk start
  - Grid under disk: hus 1.7 + disk 2.0 - bat 2.0 = 1.7 kW ✅
  - Disk klar ~01:00 → EV återstartar
  - EV SoC fortfarande ≥ 75% kl 06 (kompenserar med mer laddning efter disk)
  - Grid ≤ 4 kW ALLA timmar inkl disk-timmen

Verifiering: HC-1 kl 00 (disk-timmen), DC-1
```

**Scenario D: "HA restart kl 02:00"**
```
Setup: Scenario B, HA startar om kl 02:00

Förväntat:
  - GridGuard aktiveras inom 30s
  - fast_charging INTE aktiverat
  - Plan restorerad eller nygenererad inom 2 min
  - Urladdning återupptas
  - EV återstartas (om den var aktiv)
  - Grid spike under restart < 30s → timmedel OK
  - Grid ≤ 4 kW alla timmar

Verifiering: HC-1 kl 02, DC-2
```

**Scenario E: "GoodWe adapter fail"**
```
Setup: Scenario B, adapter returnerar unavailable kl 01:00

Förväntat:
  - Urladdning pausas (kan inte styra)
  - EV pausas (utan batterstöd > tak)
  - Retry var 30s
  - Adapter återkommer kl 01:05 → normal drift inom 30s
  - Grid ≤ 4 kW (EV pausad under adapter-fail)

Verifiering: CP-6, HC-1
```

**Scenario F: "Korsladning — det som hände inatt 2026-03-26"**
```
Setup: SoC K=86% F=78%, disk startar, coordinator sätter K=charge (-2800W)
       Förråd urladdar 2987W. EV 4.1 kW. Hus 1.7 kW.

Förväntat med ny kod:
  - INV-2 korsladning detekteras → tvinga standby
  - GridGuard: grid = 1.7 + 4.1 + 2.8 = 8.6 kW → NÖDÅTGÄRD
  - EV pausas OMEDELBART
  - Batterier till standby
  - Grid sjunker till ~1.7 kW
  - BreachRecord skapas
  - Omplanering triggas

Verifiering: Korsladning = 0 sekunder, grid aldrig > tak
```

### 12.4 Nivå 4 — Regressionstester (verkliga incidenter)

Varje incident som inträffat ska ha ett regressionstest:

| Incident | Datum | Test |
|----------|-------|------|
| 10.6 kW grid import | 2026-03-26 | Scenario F |
| Batterier laddade istf urladdade | 2026-03-26 | test_no_fast_charge_during_discharge |
| EV pausades inte vid disk | 2026-03-26 | Scenario C |
| Plan ignorerades av executor | 2026-03-26 | test_executor_follows_plan |
| SoC=0 i plan trots 98% | 2026-03-26 | test_plan_receives_correct_soc |
| predict_24h returned None | 2026-03-26 | test_predict_none_fallback |
| solcast.power_now_kw saknas | 2026-03-26 | test_solcast_missing_attribute |
| Property i __init__ bröt init | 2026-03-26 | test_coordinator_init_completes |
| pyc-cache blockerade ny kod | 2026-03-26 | CI: rensa pyc pre-deploy |

### 12.5 Nivå 5 — Live Validation

Automatisk nattlig rapport (kl 06:30) som verifierar alla DC-* kontrollpunkter:

```
CARMA Box Nattrapport 2026-03-27
═══════════════════════════════

DC-1 EV SoC vid avresa:     87% ≥ 75%  ✅
DC-2 Max timmedel natt:     3.8 kW ≤ 4.0 kW  ✅
DC-3 Max timmedel dag:      (väntar)
DC-4 Batteri utilization:   82% ≥ 50%  ✅
DC-5 Besparing:             +14.50 kr  ✅
DC-6 Antal lagbrott:        0  ✅
DC-7 Plan-precision:        78% ≥ 70%  ✅

Batterier: K=15% F=15% (mål: 15%)  ✅
Balans: K nådde 15% kl 06:12, F kl 06:08 (diff: 4 min)  ✅
Grid max momentan: 4.2 kW kl 23:32 (disk, timmedel 3.1 kW)  ✅
EV laddning: 56% → 87%, 28.5 kWh, snitt 6.1A
Urladdning total: 15.8 kWh, K: 11.9 kWh, F: 3.9 kWh
```

### 12.6 CI/CD Krav

Inget deployas utan:

```
1. Alla unit tests PASS (nivå 1)
2. Alla scenario tests PASS (nivå 3)
3. Alla regressionstester PASS (nivå 4)
4. ruff check: 0 errors
5. py_compile: syntax OK
6. Ingen ny getattr utan default
7. Alla __init__-attribut som refereras finns
```

Deploy-pipeline:
```
git push → CI runner (LXC 514) → tests → ruff →
  om OK → merge till main → sync till ha-config →
    git pull på HA → rensa pyc → integration reload
  om FAIL → blockera merge, notifiera
```

---

## 13. AUTOMATISK ROOT CAUSE ANALYS OCH ÅTGÄRD

### 13.1 Princip

CARMA Box är sin egen drifttekniker. När något går fel ska den:
1. **Upptäcka** — inom 30 sekunder
2. **Stabilisera** — omedelbar åtgärd så att lagar inte bryts
3. **Analysera** — identifiera root cause, inte bara symptom
4. **Åtgärda** — implementera fix så att det ALDRIG händer igen
5. **Rapportera** — notifiera ägaren med analys och åtgärd

### 13.2 Händelsetyper som triggar RCA

| Typ | Trigger | Exempel |
|-----|---------|---------|
| LAG-BROTT | Kontrollpunkt misslyckas | Grid timmedel > tak |
| KRASCH | Exception i coordinator | AttributeError, TypeError |
| STALL | Inget beslut på >120s | Coordinator hänger |
| DATA-BRIST | Sensor unavailable >60s | SoC, grid, priser |
| MÅL-MISS | Dygnkontrollpunkt misslyckas | EV < target kl 06 |
| ANOMALI | Oväntat beteende | Batteri laddar när det ska urladda |
| REGRESSION | Tidigare fixat fel återkommer | Korsladning igen |

### 13.3 RCA-process (automatisk)

```
FEL DETEKTERAT
    │
    ├── 1. SNAPSHOT — spara allt state vid feltillfället
    │       - Alla sensorvärden
    │       - Aktiv plan + planned action
    │       - Faktisk action (vad som verkligen hände)
    │       - Senaste 10 beslut (reasoning chain)
    │       - Alla adapter-kommandon senaste 5 min
    │
    ├── 2. STABILISERA — omedelbar åtgärd
    │       - GridGuard: pausa EV, öka urladdning
    │       - Vid krasch: degraderad drift (standby + GridGuard)
    │       - Vid data-brist: fallback-värden
    │
    ├── 3. KLASSIFICERA — vad gick fel?
    │       Kategorier:
    │       - ADAPTER_FAIL: GoodWe/Easee svarade inte
    │       - SENSOR_STALE: Data för gammal
    │       - LOGIC_ERROR: Fel beslut trots korrekt data
    │       - PLAN_WRONG: Planen var felaktig
    │       - EXTERNAL: Yttre händelse (disk, HA restart)
    │       - STATE_CORRUPT: Inkonsistent state
    │       - REGRESSION: Tidigare fixat fel
    │
    ├── 4. ROOT CAUSE — varför?
    │       Frågekedja (5 Whys):
    │       - Vad hände? → Grid nådde 10.6 kW
    │       - Varför? → Batterierna laddade istf urladdade
    │       - Varför laddade de? → fast_charging var ON
    │       - Varför var fast_charging ON? → coordinator satte det
    │       - Varför satte coordinator det? → executor ignorerade planen
    │       → ROOT CAUSE: Plan-executor frikopplade
    │
    ├── 5. ÅTGÄRDA — vad gör vi?
    │       Omedelbara åtgärder:
    │       - Parameterändringar (auto-justering inom säkra gränser)
    │       - Constraint tillagt i planner
    │       - Watchdog-regel tillagd
    │
    │       Permanenta åtgärder (kräver koddeploy):
    │       - Logik-fix identifierad
    │       - Testfall skapat
    │       - Markerad som NEEDS_CODE_FIX i rapport
    │
    └── 6. RAPPORTERA — notifiera ägaren
```

### 13.4 Incident-rapport (automatisk, skickas omedelbart)

Varje incident genererar en strukturerad rapport:

```
══════════════════════════════════════════════
CARMA Box Incident Report
Tidpunkt: 2026-03-27 01:15:00
Allvarlighet: KRITISK
══════════════════════════════════════════════

SAMMANFATTNING
Grid timmedel nådde 5.2 kW (tak: 4.0 kW) under timme 01:00-02:00.
Batterier urladdade inte trots plan = discharge 2 kW.
EV laddade 4.1 kW utan batteristöd.

TIDSLINJE
01:00:00  Plan: discharge 2 kW, EV 6A
01:00:30  Faktisk: bat idle (-8W), EV 4.1 kW
01:01:00  GridGuard: projicerat timmedel 5.4 kW > tak 4.0
01:01:00  ÅTGÄRD: EV pausad, urladdning forcerad
01:01:30  Grid: 1.7 kW (hus) ✅
01:05:00  Adapter svarade, urladdning aktiv 2 kW
01:05:30  EV återstartad 6A
01:06:00  Grid: 3.8 kW ✅

ROOT CAUSE
Executor satte inte EMS mode korrekt — adapter-anropet
returnerade timeout. Batteriet förblev i standby.

5 WHYS
1. Grid överskred tak → EV + hus utan batteristöd
2. Batterierna urladdade inte → EMS mode oförändrad
3. EMS mode oförändrad → adapter timeout
4. Adapter timeout → GoodWe Modbus-buss upptagen
5. Modbus upptagen → concurrent calls från HA integration

OMEDELBARA ÅTGÄRDER (automatiskt utförda)
✅ EV pausad kl 01:01
✅ Retry adapter kl 01:05 — lyckades
✅ Urladdning återstartad
✅ Constraint tillagt: max 1 adapter-call per 2s

PERMANENTA ÅTGÄRDER (behöver koddeploy)
⬜ Adapter: serialisera Modbus-anrop
⬜ Testfall: test_adapter_timeout_during_discharge
⬜ Watchdog: verifiera att EMS mode matchar planerat

KONTROLLPUNKTER PÅVERKADE
HC-1 kl 01: FAIL (5.2 kW > 4.0 kW)
HC-1 kl 02: OK (3.1 kW < 4.0 kW)

LIKNANDE INCIDENTER
2026-03-26 23:36 — Liknande: batterier laddade istf urladdade (10.6 kW)
                    Root cause: fast_charging ON + plan ignorerad
                    Status: ÅTGÄRDAD i manifest v1.1

══════════════════════════════════════════════
```

### 13.5 Åtgärdsdatabas

Varje åtgärd som CARMA Box implementerat registreras:

```python
Remedy:
    incident_id: str
    timestamp: datetime
    category: str           # parameter | constraint | watchdog | code_fix_needed
    description: str        # Vad gjordes
    automatic: bool         # True = CARMA Box fixade själv
    code_change_needed: bool # True = behöver deploy
    effectiveness: float    # 0-1, mäts efter 7 dagar
    reverted: bool          # True om åtgärden orsakade nya problem
```

### 13.6 Lärande från incidenter

CARMA Box bygger en kunskapsbas av orsak → åtgärd → effektivitet:

```
Om adapter_timeout → retry med backoff (effektivitet: 0.95)
Om korsladning → standby båda + omplan (effektivitet: 1.0)
Om disk_spike → pausa EV (effektivitet: 0.98)
Om sensor_stale → fallback + margin (effektivitet: 0.85)
Om plan_wrong → omplanera med constraint (effektivitet: 0.75)
```

Ineffektiva åtgärder (< 0.7) flaggas för manuell granskning.

### 13.7 Notifieringskanaler

| Allvarlighet | Kanal | Timing |
|-------------|-------|--------|
| KRITISK (LAG 1 brott) | Push + Slack + Email | Omedelbart |
| HÖG (LAG 2-5 brott) | Slack + Email | Inom 5 min |
| MEDEL (varning, nära brott) | Nattrapport | Kl 06:30 |
| LÅG (info, optimering) | Veckorapport | Måndag 08:00 |

| Parameter | Default | Beskrivning |
|-----------|---------|-------------|
| `notify_critical_channel` | slack+push | Kanal för kritiska |
| `notify_email` | (config) | Email för rapporter |
| `incident_retention_days` | 90 | Hur länge incidenter sparas |

---

## 14. VERSIONSHISTORIK

| Version | Datum | Ändring |
|---------|-------|---------|
| 1.0 | 2026-03-27 | Första manifestet — lagar, invarianter, arkitektur |
| 1.1 | 2026-03-27 | Tillagt: workflows, ML, Guardian, självläkning, dynamisk min_soc |
