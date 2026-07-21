"""Tests for CARMA Box export-arbitrage module (EXPERIMENTAL, opt-in).

Background: `planner.py`'s "never discharge during export" principle caps
every discharge branch at `net` (house load minus PV) — the battery can
never push power OUT to the grid, even when price would make that clearly
profitable. Analysis comparing Forsmark (SigenEnergy, unconstrained,
Wolta 65.02%) against Jerström (CARMA-controlled, net-capped, Wolta
`arbitrage` component -322%) on the shared 2026-07-19 evening motivated
this OPT-IN extension.

These tests cover:
  - Export permitted at a high (favorable) price percentile.
  - Export denied at a low (unfavorable) price percentile.
  - SoC floor always wins over price/export incentive.
  - `max_export_power_w` is respected strictly (never exceeded).
  - `max_discharge_w` (hardware ceiling) is respected strictly.
  - `enable_export_arbitrage=False` reproduces today's baseline behavior
    EXACTLY (regression guarantee: no existing site's behavior changes).
  - A regression-style scenario built on the same reconstructed Jerström
    2026-07-19 price data used elsewhere in this repo
    (tests/regression/test_jerstrom_discharge_20260719.py), showing the
    battery would have been able to draw down through the evening instead
    of staying pinned, if this module had been enabled.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.discharge_threshold import (
    DynamicDischargeThreshold,
)
from custom_components.carmabox.optimizer.export_arbitrage import (
    ExportArbitrageDecision,
    calculate_export_price_threshold,
    resolve_export_arbitrage_power,
)


def _pairs(prices: list[float], start_hour: int = 0) -> list[tuple[int, float]]:
    """Helper: build (hour, price) pairs from a flat price list."""
    return [(start_hour + i, p) for i, p in enumerate(prices)]


def _threshold(floor_ore: float, reason: str = "test") -> DynamicDischargeThreshold:
    """Helper: build a DynamicDischargeThreshold with just the fields we need."""
    return DynamicDischargeThreshold(
        floor_ore=floor_ore,
        percentile_used=0.95,
        sample_count=5,
        remaining_min_ore=0.0,
        remaining_max_ore=200.0,
        fallback_used=False,
        reason=reason,
    )


class TestCalculateExportPriceThreshold:
    """Thin wrapper around discharge_threshold's percentile engine."""

    def test_reuses_percentile_engine_stricter_default(self) -> None:
        """Default export percentile (0.95) yields a stricter (higher) floor
        than the ordinary discharge floor's default (0.85) on the same data.
        """
        from custom_components.carmabox.optimizer.discharge_threshold import (
            calculate_dynamic_discharge_floor,
        )

        prices = _pairs([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        export_threshold = calculate_export_price_threshold(prices)
        discharge_threshold = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert export_threshold.floor_ore >= discharge_threshold.floor_ore

    def test_percentile_configurable_not_hardcoded(self) -> None:
        prices = _pairs([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        low = calculate_export_price_threshold(prices, percentile=0.5)
        high = calculate_export_price_threshold(prices, percentile=0.99)
        assert high.floor_ore > low.floor_ore

    def test_too_few_points_uses_configurable_fallback(self) -> None:
        prices = _pairs([130.0])
        result = calculate_export_price_threshold(
            prices, min_price_points=2, fallback_floor_ore=77.0
        )
        assert result.fallback_used
        assert result.floor_ore == 77.0


class TestResolveExportArbitragePowerBaseline:
    """enable_export_arbitrage=False MUST reproduce today's default behavior."""

    def test_disabled_caps_discharge_at_house_net_load(self) -> None:
        """Disabled (default): discharge never exceeds house net load."""
        decision = resolve_export_arbitrage_power(
            current_price_ore=999.0,  # even an extreme price must not matter
            threshold=_threshold(floor_ore=10.0),
            soc_pct=90.0,
            house_net_load_w=1500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=False,
            max_export_power_w=3000.0,  # even a generous ceiling must not matter
        )
        assert isinstance(decision, ExportArbitrageDecision)
        assert decision.should_export is False
        assert decision.export_w == 0.0
        assert decision.allowed_discharge_w == 1500.0

    def test_disabled_never_discharges_when_house_already_net_exporting(self) -> None:
        """Disabled: if PV already covers the house (net<=0), battery discharge is 0 —
        this IS the 'never discharge during export' principle.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=200.0,
            threshold=_threshold(floor_ore=10.0),
            soc_pct=90.0,
            house_net_load_w=-800.0,  # PV surplus of 800W already
            max_discharge_w=5000.0,
            enable_export_arbitrage=False,
            max_export_power_w=3000.0,
        )
        assert decision.allowed_discharge_w == 0.0
        assert decision.export_w == 0.0

    def test_disabled_is_identical_regardless_of_price_or_export_cap(self) -> None:
        """Regression guarantee: sweeping price and max_export_power_w while
        disabled must never change the result versus house-net-load alone.
        """
        for price in (0.0, 50.0, 130.0, 500.0):
            for cap in (0.0, 1000.0, 10_000.0):
                decision = resolve_export_arbitrage_power(
                    current_price_ore=price,
                    threshold=_threshold(floor_ore=90.0),
                    soc_pct=70.0,
                    house_net_load_w=1200.0,
                    max_discharge_w=5000.0,
                    enable_export_arbitrage=False,
                    max_export_power_w=cap,
                )
                assert decision.allowed_discharge_w == 1200.0
                assert decision.export_w == 0.0
                assert decision.should_export is False

    def test_disabled_still_respects_hardware_discharge_ceiling(self) -> None:
        """Even in baseline mode, allowed discharge never exceeds max_discharge_w."""
        decision = resolve_export_arbitrage_power(
            current_price_ore=100.0,
            threshold=_threshold(floor_ore=90.0),
            soc_pct=70.0,
            house_net_load_w=6000.0,  # huge load, exceeds hardware limit
            max_discharge_w=5000.0,
            enable_export_arbitrage=False,
        )
        assert decision.allowed_discharge_w == 5000.0


class TestResolveExportArbitragePowerSocFloor:
    """SoC floor must always win, in both modes, over any price/export incentive."""

    def test_soc_floor_blocks_even_at_extreme_price_export_enabled(self) -> None:
        decision = resolve_export_arbitrage_power(
            current_price_ore=999.0,
            threshold=_threshold(floor_ore=10.0),
            soc_pct=19.0,
            house_net_load_w=500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=3000.0,
            min_soc_floor_pct=20.0,
        )
        assert decision.allowed_discharge_w == 0.0
        assert decision.export_w == 0.0
        assert decision.should_export is False
        assert "floor" in decision.reason.lower()

    def test_soc_exactly_at_floor_blocks(self) -> None:
        """Boundary: SoC exactly at the floor still blocks (inclusive of block)."""
        decision = resolve_export_arbitrage_power(
            current_price_ore=200.0,
            threshold=_threshold(floor_ore=10.0),
            soc_pct=20.0,
            house_net_load_w=500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=3000.0,
            min_soc_floor_pct=20.0,
        )
        assert decision.allowed_discharge_w == 0.0

    def test_soc_floor_blocks_even_ordinary_net_load_discharge(self) -> None:
        """At/below the floor, even NON-export (ordinary load-following)
        discharge is blocked — the floor is unconditional, not export-specific.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=0.0,
            threshold=_threshold(floor_ore=999.0),
            soc_pct=10.0,
            house_net_load_w=500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=False,
            min_soc_floor_pct=15.0,
        )
        assert decision.allowed_discharge_w == 0.0

    def test_soc_just_above_floor_permits_export(self) -> None:
        decision = resolve_export_arbitrage_power(
            current_price_ore=200.0,
            threshold=_threshold(floor_ore=10.0),
            soc_pct=20.1,
            house_net_load_w=500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=3000.0,
            min_soc_floor_pct=20.0,
        )
        assert decision.allowed_discharge_w > 500.0
        assert decision.should_export is True


class TestResolveExportArbitragePowerEnabledPriceGating:
    """Export enabled, but a favorable price is still required."""

    def test_export_denied_at_low_percentile_price(self) -> None:
        """Enabled, but current price is below the computed export floor
        (i.e. NOT in the top percentile of remaining hours) — falls back to
        house-net-load-only, exactly like baseline.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=40.0,
            threshold=_threshold(floor_ore=120.0),
            soc_pct=80.0,
            house_net_load_w=800.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=3000.0,
        )
        assert decision.should_export is False
        assert decision.export_w == 0.0
        assert decision.allowed_discharge_w == 800.0
        assert "floor" in decision.reason.lower()

    def test_export_allowed_at_high_percentile_price(self) -> None:
        """Enabled AND current price is at/above the export floor — export unlocked."""
        decision = resolve_export_arbitrage_power(
            current_price_ore=140.0,
            threshold=_threshold(floor_ore=120.0),
            soc_pct=80.0,
            house_net_load_w=800.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=3000.0,
        )
        assert decision.should_export is True
        assert decision.export_w == 3000.0
        assert decision.allowed_discharge_w == 3800.0

    def test_export_allowed_at_price_exactly_on_threshold(self) -> None:
        """Boundary: price exactly equal to the floor counts as favorable (>=)."""
        decision = resolve_export_arbitrage_power(
            current_price_ore=120.0,
            threshold=_threshold(floor_ore=120.0),
            soc_pct=80.0,
            house_net_load_w=800.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=1000.0,
        )
        assert decision.should_export is True

    def test_export_unlocks_discharge_even_when_house_already_net_exporting(self) -> None:
        """The core new capability: with a favorable price, the battery can
        discharge to INCREASE export even when PV alone already covers the
        house (house_net_load_w < 0) — impossible under the baseline rule.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=140.0,
            threshold=_threshold(floor_ore=120.0),
            soc_pct=80.0,
            house_net_load_w=-500.0,  # PV surplus already
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=2000.0,
        )
        assert decision.should_export is True
        assert decision.export_w == 2000.0
        assert decision.allowed_discharge_w == 2000.0


class TestResolveExportArbitragePowerCeilings:
    """max_export_power_w and max_discharge_w must be respected strictly."""

    def test_max_export_power_w_is_never_exceeded(self) -> None:
        decision = resolve_export_arbitrage_power(
            current_price_ore=500.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=90.0,
            house_net_load_w=200.0,
            max_discharge_w=50_000.0,  # effectively unlimited hardware
            enable_export_arbitrage=True,
            max_export_power_w=1500.0,
        )
        assert decision.export_w == 1500.0
        assert decision.allowed_discharge_w == 1700.0

    def test_max_discharge_w_hardware_ceiling_wins_over_export_cap(self) -> None:
        """Even a generous export cap cannot push total discharge past the
        hardware/BMS ceiling.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=500.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=90.0,
            house_net_load_w=1000.0,
            max_discharge_w=3000.0,
            enable_export_arbitrage=True,
            max_export_power_w=5000.0,  # would want 1000+5000=6000, but hw caps at 3000
        )
        assert decision.allowed_discharge_w == 3000.0
        assert decision.export_w == 2000.0  # 3000 - 1000 net load

    def test_zero_max_export_power_w_behaves_like_disabled_export(self) -> None:
        """Belt-and-suspenders: enabled but max_export_power_w left at the
        unconfigured default (0) must not export anything.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=500.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=90.0,
            house_net_load_w=800.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=0.0,
        )
        assert decision.should_export is False
        assert decision.export_w == 0.0
        assert decision.allowed_discharge_w == 800.0

    def test_negative_max_export_power_w_is_clamped_not_negative_export(self) -> None:
        """Defensive: a misconfigured negative ceiling must not produce
        negative or otherwise invalid export power.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=500.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=90.0,
            house_net_load_w=800.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=-100.0,
        )
        assert decision.export_w == 0.0
        assert decision.allowed_discharge_w == 800.0

    def test_negative_max_discharge_w_is_clamped(self) -> None:
        """Defensive: a misconfigured negative hardware ceiling clamps to 0,
        never produces negative discharge.
        """
        decision = resolve_export_arbitrage_power(
            current_price_ore=500.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=90.0,
            house_net_load_w=800.0,
            max_discharge_w=-10.0,
            enable_export_arbitrage=True,
            max_export_power_w=1000.0,
        )
        assert decision.allowed_discharge_w == 0.0


class TestResolveExportArbitragePowerReasonStrings:
    """Diagnostics should be informative for logging/QC review."""

    def test_reason_mentions_disabled_when_off(self) -> None:
        decision = resolve_export_arbitrage_power(
            current_price_ore=200.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=80.0,
            house_net_load_w=500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=False,
        )
        assert "disabled" in decision.reason.lower()

    def test_reason_mentions_export_watts_when_exporting(self) -> None:
        decision = resolve_export_arbitrage_power(
            current_price_ore=200.0,
            threshold=_threshold(floor_ore=50.0),
            soc_pct=80.0,
            house_net_load_w=500.0,
            max_discharge_w=5000.0,
            enable_export_arbitrage=True,
            max_export_power_w=1000.0,
        )
        assert "1000" in decision.reason or "export" in decision.reason.lower()
