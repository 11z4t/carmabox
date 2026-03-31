"""Coverage tests for remaining small gaps — batch 21.

Targets:
  core/plan_executor.py:  247, 273
  core/planner.py:        111, 167-169, 229
  core/reports.py:        343
"""

from __future__ import annotations

from datetime import date

# ══════════════════════════════════════════════════════════════════════════════
# core/plan_executor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPlanExecutorBatch21:
    """Lines 247 (hours_until_departure=0 → max_amps) and 273 (weight=0 → planned_w)."""

    def test_calculate_ev_start_amps_no_time_left(self) -> None:
        """hours_until_departure <= 0 → return max_amps immediately (line 247)."""
        from custom_components.carmabox.core.plan_executor import calculate_ev_start_amps

        result = calculate_ev_start_amps(
            ev_soc=50.0,
            ev_target_soc=80.0,
            ev_cap_kwh=60.0,
            hours_until_departure=0.0,  # No time left → max_amps
            max_amps=16,
        )
        assert result == 16

    def test_execute_discharge_weight_zero(self) -> None:
        """ellevio_weight=0 → actual_need_w = planned_w (line 273)."""
        from custom_components.carmabox.core.plan_executor import (
            ExecutorState,
            PlanAction,
            execute_plan_hour,
        )

        plan = PlanAction(
            hour=12,
            action="d",
            battery_kw=-2.0,  # Discharge 2 kW
            grid_kw=0.0,
            price=80.0,
            battery_soc=60,
            ev_soc=0,
        )
        state = ExecutorState(
            grid_import_w=1000.0,
            pv_power_w=0.0,
            battery_soc_1=60.0,
            battery_soc_2=-1.0,
            battery_power_1=0.0,
            battery_power_2=0.0,
            ev_power_w=0.0,
            ev_soc=-1.0,
            ev_connected=False,
            current_price=80.0,
            target_kw=2.0,
            ellevio_weight=0.0,  # Zero weight → uses planned_w path (line 273)
            headroom_kw=1.0,
        )
        result = execute_plan_hour(plan, state)
        assert result.battery_action == "discharge"


# ══════════════════════════════════════════════════════════════════════════════
# core/planner.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPlannerBatch21:
    """Lines 111 (n=0 → early return), 167-169 (surplus > battery_need), 229 (no fit)."""

    def test_plan_solar_allocation_no_fit_for_ev(self) -> None:
        """consumption >> pv+tak → max_ev_kw=0 < 1-phase → return no-charge (line 229)."""
        from custom_components.carmabox.core.planner import plan_solar_allocation

        # avg_pv=0.1, avg_consumption=3.0 → max_ev_kw=max(0, 0.1-3.0+2.0)=max(0,-0.9)=0
        # 0 < ev_1phase_kw=1.38 → line 229 fires
        result = plan_solar_allocation(
            battery_soc_pct=50.0,
            battery_cap_kwh=15.0,
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=60.0,
            hourly_pv_kw=[0.1, 0.1],  # Tiny PV
            hourly_consumption_kw=[3.0, 3.0],  # High consumption
            current_hour=10,
            sunset_hour=19,  # hours_left=9
        )
        assert result.ev_can_charge is False
        assert result.ev_recommended_amps == 0


# ══════════════════════════════════════════════════════════════════════════════
# core/reports.py
# ══════════════════════════════════════════════════════════════════════════════


class TestReportsBatch21:
    """Line 343: daily_summaries entry without 'date' key → continue."""

    def test_weekly_report_skips_entries_without_date(self) -> None:
        """Entry missing 'date' key → continue (line 343)."""
        from custom_components.carmabox.core.reports import generate_weekly_report_html

        summaries = [
            {
                "date": date(2026, 3, 30),
                "pv_kwh": 5.0,
                "consumption_kwh": 3.0,
            },
            {"pv_kwh": 5.0, "consumption_kwh": 3.0},  # No 'date' → line 343 (continue)
            {
                "date": date(2026, 3, 31),
                "pv_kwh": 6.0,
                "consumption_kwh": 3.0,
                "grid_import_kwh": 0.5,
                "savings_kr": 20.0,
                "peak_kw": 2.0,
            },
        ]
        result = generate_weekly_report_html(
            week_number=13,
            daily_summaries=summaries,
            total_savings_kr=20.0,
            avg_peak_kw=2.0,
            pv_total_kwh=11.0,
        )
        assert isinstance(result, str)
        assert len(result) > 0
