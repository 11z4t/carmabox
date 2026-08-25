# CARMA P1 Lokal — design

**Från:** 904-Bolt
**Datum:** 2026-08-24
**Status:** DESIGN, delvis live på Sandgränd + Jerström som interimslösning. Skickad till 901
för QC innan vidare utrullning/config_flow byggs.

## Bakgrund

Sandgränd och Jerström har fysiska HomeWizard P1-mätare (elmätare via P1-porten) redan
installerade och anslutna till respektive site-nätverk. Den officiella `homewizard`-
integrationen i HA kan inte parkopplas mot dem: dess `/api/user`-endpoint (som skapar den
autentiserade token:en efter knapptryckning) svarar **404 "Nothing matches the given URI"**
på båda enheterna, oavsett om "Lokal API" är påslaget i HomeWizard-appen och knappen tryckts
(verifierat upprepade gånger, båda siterna, firmware 6.0503). Sannolikt saknar den firmware-
versionen den endpointen helt.

Samtidigt svarar det **oautentiserade** `/api/v1/data`-endpointet på båda enheterna direkt,
utan parkoppling, med full realtidsdata (effekt totalt + per fas, spänning, ström, total
import/export).

## Vad CARMA P1 Lokal är

En egen, varumärkesneutral custom component (`carma_p1_local`) som läser P1-mätardata via det
öppna endpointet istället för att förlita sig på tillverkarens parkopplingsflöde. Tänkt att bli
en generell "lokal P1-mätare"-integration där HomeWizard är det första (och hittills enda)
stödda märket/protokollet — inte en permanent HomeWizard-specifik lösning.

## Nuvarande status (interim, EJ config_flow än)

- YAML-baserad platform-setup (`sensor: - platform: carma_p1_local`), ingen UI/config_flow.
- `DataUpdateCoordinator`, pollning 1s (satt av Börje 2026-08-24, HomeWizards P1 klarar generellt
  ~1 req/s lokal pollning enligt community-praxis).
- Entiteter grupperade under en `DeviceInfo` (manufacturer="HomeWizard", model/serial/firmware
  hämtat en gång vid start från `/api` roten) — ger enhetskort i HA, till skillnad från en ren
  REST-sensor-lösning.
- 13 sensorer: effekt (total + 3 faser), spänning (3 faser), ström (3 faser), total import/export
  kWh, WiFi-signal.
- Live deployad: Sandgränd (`host: 10.0.0.18`), Jerström (`host: 192.168.1.7`).

## Öppna frågor för 901:s QC

1. **Säkerhet**: `/api/v1/data` kräver ingen autentisering alls på dessa enheter — vem som helst
   på sitens LAN kan läsa förbrukningsdata. Är det en risk vi accepterar (LAN-internt, samma
   trust-nivå som andra lokala integrationer), eller bör vi kräva något ytterligare skydd?
2. **config_flow**: Börje vill kunna ändra host/scan_interval via UI istället för YAML. Ska
   byggas efter QC av grunddesignen — inte gjort än.
3. **Namngivning/scope**: är `carma_p1_local` rätt nivå av generalitet, eller ska den heta något
   mer specifikt tills fler märken/protokoll faktiskt stöds?
4. **Felhantering vid enhet nere**: `UpdateFailed` propageras via coordinatorn (entiteter blir
   `unavailable`), men ingen retry-backoff utöver `scan_interval`. Tillräckligt, eller bör den
   ha exponentiell backoff vid längre avbrott?
5. **Bör den ersätta eller komplettera** ett framtida-fixat officiellt `homewizard`-flöde, om
   HomeWizard släpper firmware som löser `/api/user`-404:an? (Föreslår: komplettera, med
   fallback-logik som föredrar officiell integration om den lyckas parkopplas.)

## Relaterat

- Löser samtidigt en separat, redan känd bugg: Wiklander-siternas `site_energy_day`-aggregering
  (Hub-sidan) visar felaktigt 0 kWh import/export varje dag eftersom dess Huawei-baserade
  fallback-sensorer (`net_import_kwh_today`) är trasiga. P1-mätarens data är en oberoende,
  korrekt källa som kan ersätta den fallbacken — separat uppföljningsarbete, inte del av denna
  design.
