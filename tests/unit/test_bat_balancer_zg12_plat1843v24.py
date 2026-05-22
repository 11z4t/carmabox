"""Tests for PLAT-1843-V24: ZG-12 anti-export, EVENING_DISCHARGE, AC61/AC62.

T-S20: EVENING_DISCHARGE aktiveringsvillkor (§5b.1)
T-S21: ZG-12 anti-export — Σ ems_power_limit ≤ |house_deficit| ALLTID
T-S22: SoC-floor — bank under eve_min_soc stoppas, annan tar över
T-B14: charging_active / discharging_active (BAT_BALANCER §3.2, AC61)
T-S17: last-shedding spillover_w==0 prereq (AC62 V3-B2 fix)
T-ZG12-1: ZG-12 cap korrekt vid proportionell fördelning (kontor dominant)
T-ZG12-2: ZG-12 cap = 0 när surplus ≥ 0 (ingen urladdning behövs)
"""

from __future__ import annotations

# ── ZG-12 cap-logik (speglar bat_balancer_zg12.yaml) ──────────────────────


def compute_zg12_discharge_cap_w(
    surplus_now_w: float,
    direction: str,
) -> int:
    """
    ZG-12: max(0, |surplus_now_w|) vid direction=DISCHARGE.
    Speglar sensor.bat_balancer_zg12_discharge_cap_w.
    """
    if direction != "DISCHARGE":
        return 0
    if surplus_now_w < 0:
        return int(abs(surplus_now_w))
    # Surplus ≥ 0 men direction=DISCHARGE: transient/MANUAL → cap=0 (ingen urladdning)
    return 0


def compute_zg12_limits(
    surplus_now_w: float,
    direction: str,
    dist_k: float,
    dist_f: float,
) -> tuple[int, int]:
    """
    Beräkna per-bank ZG-12-capped limits.
    Returnerar (limit_kontor_w, limit_forrad_w).
    Speglar sensor.bat_balancer_zg12_limit_kontor_w + _forrad_w.
    """
    cap_total = compute_zg12_discharge_cap_w(surplus_now_w, direction)

    if direction != "DISCHARGE":
        return (0, 0)

    raw_k = abs(dist_k) if dist_k < 0 else 0.0
    raw_f = abs(dist_f) if dist_f < 0 else 0.0
    total_raw = raw_k + raw_f

    if total_raw <= 0:
        return (0, 0)

    share_k = cap_total * (raw_k / total_raw)
    limit_k = int(min(raw_k, share_k))

    remaining_cap = max(cap_total - limit_k, 0)
    limit_f = int(min(raw_f, remaining_cap))

    return (limit_k, limit_f)


# ── EVENING_DISCHARGE aktiveringsvillkor (speglar evening_discharge.yaml) ─


def can_activate_evening_discharge(
    eve_discharge_enabled: bool,
    surplus_now_w: float,
    eve_discharge_trigger_w: float,  # default -200
    pv_forecast_tomorrow_kwh: float,
    eve_forecast_factor: float,  # default 1.2
    cap_total_kwh: float,  # bat_capacity_kontor_kwh + bat_capacity_forrad_kwh
    avg_soc: float,
    eve_min_soc_pct: float,  # default 25
    soc_k: float,
    soc_f: float,
    current_strategy: str,
) -> bool:
    """Speglar aktiveringsvillkoren i carma_evening_discharge_activate."""
    if not eve_discharge_enabled:
        return False
    if surplus_now_w >= eve_discharge_trigger_w:
        return False
    # §5b.1 villkor 3: forecast ≥ factor × bat_kwh_to_100
    bat_kwh_to_100 = (100 - avg_soc) / 100 * cap_total_kwh
    if pv_forecast_tomorrow_kwh < eve_forecast_factor * bat_kwh_to_100:
        return False
    # §5b.1 villkor 4: minst en bank > min_soc
    if soc_k <= eve_min_soc_pct and soc_f <= eve_min_soc_pct:
        return False
    # §5b.1 villkor 5: inga högre-prio strategier
    blocked_strategies = {"OFF_GRID", "PV_SURPLUS_MIDDAY", "PV_SURPLUS_MIDDAY_EV_PRIO"}
    if current_strategy in blocked_strategies:
        return False
    return True


# ── charging_active / discharging_active (speglar bat_balancer_desired_actual.yaml) ─


def compute_charging_active(bat_k_w: float, bat_f_w: float, threshold_w: float) -> bool:
    """True när minst en bank laddar > threshold_w. Positiv = laddning (GoodWe-konvention)."""
    return bat_k_w > threshold_w or bat_f_w > threshold_w


def compute_discharging_active(bat_k_w: float, bat_f_w: float, threshold_w: float) -> bool:
    """True när minst en bank laddar ur > threshold_w. Negativ = urladdning."""
    return bat_k_w < -threshold_w or bat_f_w < -threshold_w


# ── T-S21: ZG-12 anti-export ─────────────────────────────────────────────


class TestZG12AntiExport:
    """T-S21: Σ ems_power_limit ≤ |house_deficit| vid simulerade deficit-värden."""

    def test_sigma_never_exceeds_deficit(self):
        """ZG-12 kärn-invariant: Σ limit ≤ cap_total (= |house_deficit|)."""
        test_cases = [
            # surplus_now, dist_k, dist_f
            (-2500.0, -1800.0, -700.0),
            (-1000.0, -800.0, -200.0),
            (-3000.0, -2000.0, -1000.0),
            (-500.0, -400.0, -100.0),
            (-1500.0, -1000.0, -500.0),
        ]
        for surplus, dist_k, dist_f in test_cases:
            cap = compute_zg12_discharge_cap_w(surplus, "DISCHARGE")
            lk, lf = compute_zg12_limits(surplus, "DISCHARGE", dist_k, dist_f)
            sigma = lk + lf
            assert sigma <= cap + 1, (  # +1 för int-avrundning
                f"ZG-12 BROTT: Σ={sigma}W > cap={cap}W "
                f"(surplus={surplus}, dist_k={dist_k}, dist_f={dist_f})"
            )

    def test_positive_surplus_gives_zero_cap(self):
        """T-ZG12-2: Om surplus ≥ 0 → cap = 0 → ingen urladdning tillåts."""
        cap = compute_zg12_discharge_cap_w(500.0, "DISCHARGE")
        assert cap == 0

    def test_zero_surplus_gives_zero_cap(self):
        """Exakt noll-surplus → ingen urladdning."""
        cap = compute_zg12_discharge_cap_w(0.0, "DISCHARGE")
        assert cap == 0

    def test_cap_equals_absolute_deficit(self):
        """Cap = exakt |surplus_now_w| vid deficit."""
        cap = compute_zg12_discharge_cap_w(-2000.0, "DISCHARGE")
        assert cap == 2000

    def test_not_discharge_direction_gives_zero(self):
        """T-ZG12-2: direction ≠ DISCHARGE → cap = 0 (ZG-11 charge_pv-invariant)."""
        assert compute_zg12_discharge_cap_w(-3000.0, "CHARGE") == 0
        assert compute_zg12_discharge_cap_w(-3000.0, "STANDBY") == 0

    def test_proportional_split_respects_cap(self):
        """T-ZG12-1: Proportionell split — kontor dominant ger stor andel till kontor."""
        surplus = -2000.0
        dist_k = -1500.0  # 75% av distribution
        dist_f = -500.0  # 25%
        cap = compute_zg12_discharge_cap_w(surplus, "DISCHARGE")  # = 2000
        lk, lf = compute_zg12_limits(surplus, "DISCHARGE", dist_k, dist_f)
        # Kontor ska få ca 75% av cap
        assert lk > lf, f"Kontor (dominant) borde få mer: lk={lk}, lf={lf}"
        assert lk + lf <= cap + 1
        assert lk <= 1500  # aldrig mer än rå distribution

    def test_limits_never_exceed_raw_distribution(self):
        """ZG-12 cap kan aldrig HÖJA limit utöver distribution."""
        # Scenario: cap >> distribution → limit = distribution
        surplus = -10000.0  # mycket stort underskott
        dist_k = -1000.0
        dist_f = -500.0
        lk, lf = compute_zg12_limits(surplus, "DISCHARGE", dist_k, dist_f)
        assert lk <= 1000, f"limit_k={lk} > raw_dist_k=1000"
        assert lf <= 500, f"limit_f={lf} > raw_dist_f=500"

    def test_bm1_scenario_5300w_export_blocked(self):
        """BM-1 regression: hardcoded 3000W per bank → 6 kW vid 2.5 kW behov.
        Med ZG-12: max 2500W total → ingen export.
        """
        house_deficit_w = 2500.0
        surplus_now_w = -house_deficit_w
        # Utan ZG-12 (gammal bugg): 3000+3000 = 6000W → 3500W export
        # Med ZG-12: max 2500W total
        dist_k = -3000.0  # vad balancer ville ge kontor
        dist_f = -3000.0  # vad balancer ville ge förråd
        cap = compute_zg12_discharge_cap_w(surplus_now_w, "DISCHARGE")
        lk, lf = compute_zg12_limits(surplus_now_w, "DISCHARGE", dist_k, dist_f)
        sigma = lk + lf
        assert cap == 2500
        assert sigma <= 2500 + 1, f"Export blockerad: sigma={sigma}W ≤ 2500W"
        assert sigma < 6000, "BM-1 fix: 6 kW export blockerad"


# ── T-S20: EVENING_DISCHARGE aktivering ──────────────────────────────────


class TestEveningDischargeActivation:
    """T-S20: EVENING_DISCHARGE aktiveringsvillkor §5b.1."""

    def _base_params(self):
        return dict(
            eve_discharge_enabled=True,
            surplus_now_w=-500.0,
            eve_discharge_trigger_w=-200.0,
            pv_forecast_tomorrow_kwh=20.0,
            eve_forecast_factor=1.2,
            cap_total_kwh=20.0,  # 15 + 5 kWh
            avg_soc=60.0,
            eve_min_soc_pct=25.0,
            soc_k=65.0,
            soc_f=55.0,
            current_strategy="IDLE",
        )

    def test_all_conditions_met_activates(self):
        """T-S20: Alla villkor uppfyllda → aktivering."""
        assert can_activate_evening_discharge(**self._base_params()) is True

    def test_disabled_blocks_activation(self):
        """Master-switch off → ingen aktivering."""
        params = self._base_params()
        params["eve_discharge_enabled"] = False
        assert can_activate_evening_discharge(**params) is False

    def test_surplus_positive_no_activation(self):
        """Surplus ≥ trigger_w → inte aktivera (deficit saknas)."""
        params = self._base_params()
        params["surplus_now_w"] = 0.0
        assert can_activate_evening_discharge(**params) is False

    def test_surplus_exactly_at_trigger_no_activation(self):
        """Surplus == trigger_w → inte aktivera (måste vara < trigger)."""
        params = self._base_params()
        params["surplus_now_w"] = -200.0  # == trigger
        assert can_activate_evening_discharge(**params) is False

    def test_forecast_too_low_blocks(self):
        """Forecast < factor × bat_kwh_to_100 → imorgon räcker inte → ingen aktivering."""
        params = self._base_params()
        # avg_soc=60, cap=20 → bat_kwh_to_100 = 0.4 × 20 = 8 kWh
        # factor × bat_kwh_to_100 = 1.2 × 8 = 9.6 kWh → forecast måste vara ≥ 9.6
        params["pv_forecast_tomorrow_kwh"] = 5.0  # för låg
        assert can_activate_evening_discharge(**params) is False

    def test_soc_both_below_min_blocks(self):
        """Båda banker under min_soc → ingen aktivering (floor-skydd)."""
        params = self._base_params()
        params["soc_k"] = 20.0
        params["soc_f"] = 20.0
        assert can_activate_evening_discharge(**params) is False

    def test_one_bank_above_min_allows(self):
        """En bank > min_soc räcker för aktivering."""
        params = self._base_params()
        params["soc_k"] = 20.0  # under min
        params["soc_f"] = 50.0  # över min
        assert can_activate_evening_discharge(**params) is True

    def test_higher_prio_strategy_blocks(self):
        """OFF_GRID, MIDDAY, EV_PRIO blockerar EVENING_DISCHARGE (§3 precedens)."""
        for strategy in ["OFF_GRID", "PV_SURPLUS_MIDDAY", "PV_SURPLUS_MIDDAY_EV_PRIO"]:
            params = self._base_params()
            params["current_strategy"] = strategy
            assert (
                can_activate_evening_discharge(**params) is False
            ), f"Strategi {strategy} borde blockera EVENING_DISCHARGE"

    def test_idle_strategy_allows(self):
        """IDLE-strategi tillåter EVENING_DISCHARGE (lägre precedens)."""
        params = self._base_params()
        params["current_strategy"] = "IDLE"
        assert can_activate_evening_discharge(**params) is True


# ── T-S22: SoC-floor enforcement ─────────────────────────────────────────


class TestEveDischargeFloorSoc:
    """T-S22: SoC-floor — bank under min_soc stoppas."""

    def test_both_below_min_blocks(self):
        """Båda banker under floor → deaktivering-villkor uppfyllt."""
        soc_k, soc_f, min_soc = 20.0, 22.0, 25.0
        # should_deactivate = soc_k <= min_soc AND soc_f <= min_soc
        should_deactivate = soc_k <= min_soc and soc_f <= min_soc
        assert should_deactivate is True

    def test_one_bank_above_floor_continues(self):
        """En bank kvar > floor → deaktivering ännu inte."""
        soc_k, soc_f, min_soc = 15.0, 40.0, 25.0
        should_deactivate = soc_k <= min_soc and soc_f <= min_soc
        assert should_deactivate is False


# ── T-B14: charging_active / discharging_active ───────────────────────────


class TestBatBalancerActiveSignals:
    """T-B14: BAT_BALANCER §3.2 AC61 — charging_active + discharging_active."""

    def test_charging_active_when_bat_above_threshold(self):
        """charging_active=True när minst en bank laddar > threshold."""
        assert compute_charging_active(800.0, 200.0, 100.0) is True
        assert compute_charging_active(0.0, 500.0, 100.0) is True

    def test_charging_active_false_below_threshold(self):
        """Båda banker under threshold → charging_active=False."""
        assert compute_charging_active(50.0, 80.0, 100.0) is False

    def test_charging_active_false_when_discharging(self):
        """Urladdning → charging_active=False (negativa värden)."""
        assert compute_charging_active(-500.0, -300.0, 100.0) is False

    def test_discharging_active_when_bat_below_negative_threshold(self):
        """discharging_active=True när minst en bank laddar ur > threshold."""
        assert compute_discharging_active(-800.0, -200.0, 100.0) is True
        assert compute_discharging_active(0.0, -500.0, 100.0) is True

    def test_discharging_active_false_when_charging(self):
        """Laddning → discharging_active=False."""
        assert compute_discharging_active(500.0, 300.0, 100.0) is False

    def test_threshold_respected(self):
        """Exakt threshold → False (kräver STRIKT >)."""
        threshold = 100.0
        assert compute_charging_active(100.0, 50.0, threshold) is False
        assert compute_discharging_active(-100.0, -50.0, threshold) is False

    def test_mutual_exclusion(self):
        """charging och discharging kan ALDRIG vara True samtidigt (normsöv ZG-8a)."""
        # Båda banker: en laddar, en laddar ur — violation, men signal ska vara korrekt
        bat_k, bat_f, threshold = 500.0, -500.0, 100.0
        charging = compute_charging_active(bat_k, bat_f, threshold)
        discharging = compute_discharging_active(bat_k, bat_f, threshold)
        # I mixed state: kontor laddar, förråd laddar ur → båda kan vara True
        # (detta är en ZG-8a-violation som emergency hanterar)
        assert charging is True  # kontor laddar
        assert discharging is True  # förråd laddar ur


# ── T-S17 uppdaterad: spillover_w==0 prereq (AC62) ───────────────────────


class TestLastSheddingSpilloverPrereq:
    """T-S17 update: §4.4.1 mutual-exclusion prereq — spillover_w==0 (AC62)."""

    def _last_shedding_allowed(
        self,
        spillover_w: float,
        overflow_threshold_w: float,
        charging_active: bool,
        direction: str,
        surplus_w: float,
        actual_bat_w: float,
        deadband_w: float,
    ) -> bool:
        """Speglar alla conditions i carma_last_shedding."""
        # AC62: spillover prereq
        if spillover_w > overflow_threshold_w:
            return False
        # direction=CHARGE
        if direction != "CHARGE":
            return False
        # charging_active (AC61 prereq)
        if not charging_active:
            return False
        # Bat tar inte allt surplus
        if not (surplus_w > deadband_w and actual_bat_w < (surplus_w - deadband_w)):
            return False
        return True

    def test_normal_case_shedding_allowed(self):
        """Normal bat-prio shedding: spillover=0, bat laddar men tar inte allt."""
        assert (
            self._last_shedding_allowed(
                spillover_w=0.0,
                overflow_threshold_w=200.0,
                charging_active=True,
                direction="CHARGE",
                surplus_w=2000.0,
                actual_bat_w=1000.0,
                deadband_w=100.0,
            )
            is True
        )

    def test_spillover_blocks_shedding(self):
        """V3-B2 fix: spillover > threshold → bat BMS-cappad → shedding hjälper ej."""
        assert (
            self._last_shedding_allowed(
                spillover_w=500.0,  # > threshold 200
                overflow_threshold_w=200.0,
                charging_active=True,
                direction="CHARGE",
                surplus_w=2000.0,
                actual_bat_w=1000.0,
                deadband_w=100.0,
            )
            is False
        )

    def test_spillover_at_threshold_allows(self):
        """spillover == threshold → exakt på gräns → shedding tillåts (≤ villkor)."""
        assert (
            self._last_shedding_allowed(
                spillover_w=200.0,  # == threshold
                overflow_threshold_w=200.0,
                charging_active=True,
                direction="CHARGE",
                surplus_w=2000.0,
                actual_bat_w=1000.0,
                deadband_w=100.0,
            )
            is True
        )

    def test_not_charging_blocks_shedding(self):
        """Bat laddar inte (charging_active=False) → shedding meningslös."""
        assert (
            self._last_shedding_allowed(
                spillover_w=0.0,
                overflow_threshold_w=200.0,
                charging_active=False,
                direction="CHARGE",
                surplus_w=2000.0,
                actual_bat_w=0.0,
                deadband_w=100.0,
            )
            is False
        )

    def test_discharge_direction_blocks_shedding(self):
        """direction=DISCHARGE → shedning ska ALDRIG ske (ZG-12-domän)."""
        assert (
            self._last_shedding_allowed(
                spillover_w=0.0,
                overflow_threshold_w=200.0,
                charging_active=False,
                direction="DISCHARGE",
                surplus_w=-2000.0,
                actual_bat_w=-1000.0,
                deadband_w=100.0,
            )
            is False
        )

    def test_bat_taking_all_surplus_no_shedding(self):
        """Bat tar redan allt surplus (actual ≈ surplus) → shedning onödig."""
        assert (
            self._last_shedding_allowed(
                spillover_w=0.0,
                overflow_threshold_w=200.0,
                charging_active=True,
                direction="CHARGE",
                surplus_w=1000.0,
                actual_bat_w=980.0,  # tar nästan allt (deficit = 20W < deadband 100W)
                deadband_w=100.0,
            )
            is False
        )
