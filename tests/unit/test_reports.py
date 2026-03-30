"""Unit tests for HTML report generation."""

import re
from datetime import date, timedelta

from custom_components.carmabox.core.reports import (
    generate_daily_report_html,
    generate_weekly_report_html,
)


class TestDailyReportGeneration:
    """Test suite for daily report HTML generation."""

    def test_daily_report_basic_structure(self):
        """Test that daily report contains all required sections."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=25.5,
            total_consumption_kwh=30.2,
            grid_import_kwh=8.5,
            grid_export_kwh=3.8,
            battery_cycles=1.5,
            ev_charged_kwh=15.0,
            savings_kr=125.50,
            peak_kw=5.2,
            ellevio_cost_kr=45.30,
        )

        # Check DOCTYPE and HTML structure
        assert "<!DOCTYPE html>" in html
        assert '<html lang="sv">' in html
        assert "<title>Daglig Energirapport" in html

        # Check main sections
        assert "⚡ Daglig Energirapport" in html
        assert "📊 Nyckeltal" in html
        assert "📈 Självförsörjning" in html
        assert "🔌 Energiflöden" in html
        assert "💸 Kostnader" in html

        # Check responsive viewport meta tag
        assert '<meta name="viewport"' in html

    def test_daily_report_swedish_date_formatting(self):
        """Test that date is formatted correctly in Swedish."""
        # Test Monday
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),  # Monday
            total_pv_kwh=20.0,
            total_consumption_kwh=25.0,
            grid_import_kwh=5.0,
            grid_export_kwh=0.0,
            battery_cycles=1.0,
            ev_charged_kwh=10.0,
            savings_kr=100.0,
            peak_kw=4.5,
            ellevio_cost_kr=40.0,
        )

        assert "Måndag" in html
        assert "2026-03-30" in html

        # Test Sunday
        html_sunday = generate_daily_report_html(
            date_obj=date(2026, 4, 5),  # Sunday
            total_pv_kwh=20.0,
            total_consumption_kwh=25.0,
            grid_import_kwh=5.0,
            grid_export_kwh=0.0,
            battery_cycles=1.0,
            ev_charged_kwh=10.0,
            savings_kr=100.0,
            peak_kw=4.5,
            ellevio_cost_kr=40.0,
        )

        assert "Söndag" in html_sunday

    def test_daily_report_metric_values(self):
        """Test that all metric values are correctly displayed."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=25.5,
            total_consumption_kwh=30.2,
            grid_import_kwh=8.5,
            grid_export_kwh=3.8,
            battery_cycles=1.5,
            ev_charged_kwh=15.0,
            savings_kr=125.50,
            peak_kw=5.2,
            ellevio_cost_kr=45.30,
        )

        # Check PV production
        assert "25.5" in html and "kWh" in html

        # Check consumption
        assert "30.2" in html

        # Check peak power
        assert "5.20" in html and "kW" in html

        # Check savings
        assert "125" in html or "126" in html  # Rounded to 0 decimals

        # Check grid import/export
        assert "8.50" in html
        assert "3.80" in html

        # Check battery cycles
        assert "1.50" in html

        # Check EV charging
        assert "15.00" in html

        # Check Ellevio cost
        assert "45.30" in html

    def test_daily_report_calculated_percentages(self):
        """Test that self-consumption and self-sufficiency are calculated correctly."""
        # Scenario: 30 kWh PV, 25 kWh consumption, 5 kWh export, 0 kWh import
        # Self-consumption: (30-5)/30 = 83.3%
        # Self-sufficiency: (25-0)/25 = 100%
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=30.0,
            total_consumption_kwh=25.0,
            grid_import_kwh=0.0,
            grid_export_kwh=5.0,
            battery_cycles=1.0,
            ev_charged_kwh=10.0,
            savings_kr=150.0,
            peak_kw=4.0,
            ellevio_cost_kr=40.0,
        )

        # Self-consumption should be ~83%
        assert re.search(r"8[23]<span class=\"metric-unit\">%</span>", html)

        # Self-sufficiency should be 100%
        assert re.search(r"100<span class=\"metric-unit\">%</span>", html)

    def test_daily_report_zero_pv_handling(self):
        """Test report generation with zero PV production (nighttime/cloudy day)."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=0.0,
            total_consumption_kwh=20.0,
            grid_import_kwh=20.0,
            grid_export_kwh=0.0,
            battery_cycles=0.0,
            ev_charged_kwh=0.0,
            savings_kr=0.0,
            peak_kw=3.5,
            ellevio_cost_kr=50.0,
        )

        # Should not crash and should show 0 values
        assert "0.0" in html or "0" in html
        assert "☀️ Solproduktion" in html

        # Self-consumption and self-sufficiency should be 0%
        assert re.search(r"0<span class=\"metric-unit\">%</span>", html)

    def test_daily_report_inline_styles(self):
        """Test that report uses inline styles for email compatibility."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=20.0,
            total_consumption_kwh=25.0,
            grid_import_kwh=5.0,
            grid_export_kwh=0.0,
            battery_cycles=1.0,
            ev_charged_kwh=10.0,
            savings_kr=100.0,
            peak_kw=4.0,
            ellevio_cost_kr=40.0,
        )

        # Check that styles are embedded in <style> tag
        assert "<style>" in html
        assert "font-family:" in html
        assert "background:" in html
        assert ".container" in html
        assert ".metric-card" in html

        # Check mobile responsiveness
        assert "@media" in html
        assert "max-width: 600px" in html

    def test_daily_report_swedish_characters(self):
        """Test that Swedish characters (åäö) are properly handled."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=20.0,
            total_consumption_kwh=25.0,
            grid_import_kwh=5.0,
            grid_export_kwh=0.0,
            battery_cycles=1.0,
            ev_charged_kwh=10.0,
            savings_kr=100.0,
            peak_kw=4.0,
            ellevio_cost_kr=40.0,
        )

        # Check charset declaration
        assert '<meta charset="UTF-8">' in html

        # Check Swedish labels
        assert "Förbrukning" in html
        assert "Självförsörjning" in html
        assert "Egenförbrukning" in html
        assert "Måndag" in html
        assert "nätavgift" in html


class TestWeeklyReportGeneration:
    """Test suite for weekly report HTML generation."""

    def test_weekly_report_basic_structure(self):
        """Test that weekly report contains all required sections."""
        daily_summaries = [
            {
                "date": date(2026, 3, 24),
                "pv_kwh": 20.0,
                "consumption_kwh": 25.0,
                "grid_import_kwh": 5.0,
                "savings_kr": 80.0,
            },
            {
                "date": date(2026, 3, 25),
                "pv_kwh": 25.0,
                "consumption_kwh": 28.0,
                "grid_import_kwh": 3.0,
                "savings_kr": 100.0,
            },
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=180.0,
            avg_peak_kw=4.5,
            pv_total_kwh=45.0,
        )

        # Check DOCTYPE and HTML structure
        assert "<!DOCTYPE html>" in html
        assert '<html lang="sv">' in html
        assert "<title>Veckorapport - Vecka 13" in html

        # Check main sections
        assert "📅 Veckorapport - Vecka 13" in html
        assert "📊 Veckans Översikt" in html
        assert "📆 Daglig Uppdelning" in html
        assert "🏆 Veckans Höjdpunkter" in html

    def test_weekly_report_date_range(self):
        """Test that date range is correctly displayed."""
        daily_summaries = [
            {
                "date": date(2026, 3, 24),
                "pv_kwh": 20.0,
                "consumption_kwh": 25.0,
                "grid_import_kwh": 5.0,
                "savings_kr": 80.0,
            },
            {
                "date": date(2026, 3, 30),
                "pv_kwh": 25.0,
                "consumption_kwh": 28.0,
                "grid_import_kwh": 3.0,
                "savings_kr": 100.0,
            },
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=180.0,
            avg_peak_kw=4.5,
            pv_total_kwh=45.0,
        )

        assert "2026-03-24 - 2026-03-30" in html

    def test_weekly_report_metric_aggregation(self):
        """Test that weekly metrics are correctly aggregated."""
        daily_summaries = [
            {
                "date": date(2026, 3, 24),
                "pv_kwh": 20.0,
                "consumption_kwh": 25.0,
                "grid_import_kwh": 5.0,
                "savings_kr": 80.0,
            },
            {
                "date": date(2026, 3, 25),
                "pv_kwh": 25.0,
                "consumption_kwh": 30.0,
                "grid_import_kwh": 5.0,
                "savings_kr": 100.0,
            },
            {
                "date": date(2026, 3, 26),
                "pv_kwh": 30.0,
                "consumption_kwh": 28.0,
                "grid_import_kwh": 0.0,
                "savings_kr": 120.0,
            },
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=300.0,
            avg_peak_kw=4.8,
            pv_total_kwh=75.0,
        )

        # Total savings
        assert "300" in html

        # Total PV
        assert "75.0" in html or "75" in html

        # Total consumption (25+30+28 = 83)
        assert "83.0" in html or "83" in html

        # Total import (5+5+0 = 10)
        assert "10.0" in html or "10" in html

        # Average peak
        assert "4.8" in html or "4.80" in html

    def test_weekly_report_daily_rows(self):
        """Test that daily rows are generated for each day."""
        daily_summaries = [
            {
                "date": date(2026, 3, 23),  # Monday
                "pv_kwh": 20.0,
                "consumption_kwh": 25.0,
                "grid_import_kwh": 5.0,
                "savings_kr": 80.0,
            },
            {
                "date": date(2026, 3, 24),  # Tuesday
                "pv_kwh": 25.0,
                "consumption_kwh": 30.0,
                "grid_import_kwh": 3.0,
                "savings_kr": 100.0,
            },
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=180.0,
            avg_peak_kw=4.5,
            pv_total_kwh=45.0,
        )

        # Check that both days are present
        assert "Mån" in html
        assert "Tis" in html
        assert "2026-03-23" in html
        assert "2026-03-24" in html

        # Check daily values
        assert "20.0" in html  # Monday PV
        assert "25.0" in html  # Tuesday PV

    def test_weekly_report_best_and_worst_days(self):
        """Test that best and worst solar days are identified."""
        daily_summaries = [
            {
                "date": date(2026, 3, 24),
                "pv_kwh": 15.0,
                "consumption_kwh": 25.0,
                "grid_import_kwh": 10.0,
                "savings_kr": 60.0,
            },
            {
                "date": date(2026, 3, 25),
                "pv_kwh": 35.0,  # Best day
                "consumption_kwh": 28.0,
                "grid_import_kwh": 0.0,
                "savings_kr": 140.0,
            },
            {
                "date": date(2026, 3, 26),
                "pv_kwh": 5.0,  # Worst day
                "consumption_kwh": 30.0,
                "grid_import_kwh": 25.0,
                "savings_kr": 20.0,
            },
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=220.0,
            avg_peak_kw=5.0,
            pv_total_kwh=55.0,
        )

        # Check best day (35 kWh on 2026-03-25)
        assert "35.0" in html
        assert "2026-03-25" in html

        # Check worst day (5 kWh on 2026-03-26)
        assert "5.0" in html
        assert "2026-03-26" in html

        # Check section heading
        assert "🏆 Veckans Höjdpunkter" in html
        assert "☀️ Bästa Soldagen" in html
        assert "🌥️ Lägsta Soldagen" in html

    def test_weekly_report_empty_summaries(self):
        """Test report generation with empty daily summaries."""
        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=[],
            total_savings_kr=0.0,
            avg_peak_kw=0.0,
            pv_total_kwh=0.0,
        )

        # Should not crash
        assert "Vecka 13" in html
        assert "Ingen data" in html
        assert "0 dagar analyserade" in html

    def test_weekly_report_self_sufficiency_calculation(self):
        """Test that weekly self-sufficiency is calculated correctly."""
        # Scenario: 100 kWh consumption, 20 kWh import = 80% self-sufficiency
        daily_summaries = [
            {
                "date": date(2026, 3, 24),
                "pv_kwh": 40.0,
                "consumption_kwh": 50.0,
                "grid_import_kwh": 10.0,
                "savings_kr": 160.0,
            },
            {
                "date": date(2026, 3, 25),
                "pv_kwh": 40.0,
                "consumption_kwh": 50.0,
                "grid_import_kwh": 10.0,
                "savings_kr": 160.0,
            },
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=320.0,
            avg_peak_kw=5.0,
            pv_total_kwh=80.0,
        )

        # Self-sufficiency should be 80%
        assert "80%" in html

    def test_weekly_report_mobile_responsive(self):
        """Test that weekly report includes mobile responsive styles."""
        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=[
                {
                    "date": date(2026, 3, 24),
                    "pv_kwh": 20.0,
                    "consumption_kwh": 25.0,
                    "grid_import_kwh": 5.0,
                    "savings_kr": 80.0,
                }
            ],
            total_savings_kr=80.0,
            avg_peak_kw=4.5,
            pv_total_kwh=20.0,
        )

        # Check responsive meta tag
        assert '<meta name="viewport"' in html

        # Check media query for mobile
        assert "@media" in html
        assert "max-width: 768px" in html

    def test_weekly_report_day_count_in_footer(self):
        """Test that footer shows correct number of days analyzed."""
        daily_summaries = [
            {
                "date": date(2026, 3, 24 + i),
                "pv_kwh": 20.0,
                "consumption_kwh": 25.0,
                "grid_import_kwh": 5.0,
                "savings_kr": 80.0,
            }
            for i in range(7)
        ]

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=560.0,
            avg_peak_kw=4.5,
            pv_total_kwh=140.0,
        )

        assert "7 dagar analyserade" in html

    def test_weekly_report_swedish_weekday_abbreviations(self):
        """Test that Swedish weekday abbreviations are used."""
        # Test all 7 days of the week
        daily_summaries = []
        start_date = date(2026, 3, 23)  # Monday

        for i in range(7):
            daily_summaries.append(
                {
                    "date": start_date + timedelta(days=i),
                    "pv_kwh": 20.0,
                    "consumption_kwh": 25.0,
                    "grid_import_kwh": 5.0,
                    "savings_kr": 80.0,
                }
            )

        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=daily_summaries,
            total_savings_kr=560.0,
            avg_peak_kw=4.5,
            pv_total_kwh=140.0,
        )

        # Check all Swedish weekday abbreviations
        weekdays = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
        for weekday in weekdays:
            assert weekday in html


class TestReportEdgeCases:
    """Test edge cases and error handling."""

    def test_daily_report_high_values(self):
        """Test daily report with very high energy values."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=150.5,
            total_consumption_kwh=200.8,
            grid_import_kwh=75.3,
            grid_export_kwh=25.0,
            battery_cycles=5.5,
            ev_charged_kwh=80.0,
            savings_kr=1250.75,
            peak_kw=15.8,
            ellevio_cost_kr=450.50,
        )

        # Should handle large numbers correctly
        assert "150.5" in html
        assert "200.8" in html
        assert "15.8" in html or "15.80" in html
        assert "1250" in html or "1251" in html

    def test_weekly_report_single_day(self):
        """Test weekly report with only one day of data."""
        html = generate_weekly_report_html(
            week_number=13,
            daily_summaries=[
                {
                    "date": date(2026, 3, 24),
                    "pv_kwh": 20.0,
                    "consumption_kwh": 25.0,
                    "grid_import_kwh": 5.0,
                    "savings_kr": 80.0,
                }
            ],
            total_savings_kr=80.0,
            avg_peak_kw=4.5,
            pv_total_kwh=20.0,
        )

        # Should work with single day
        assert "1 dagar analyserade" in html
        assert "2026-03-24" in html

    def test_decimal_precision_consistency(self):
        """Test that decimal places are consistent across the report."""
        html = generate_daily_report_html(
            date_obj=date(2026, 3, 30),
            total_pv_kwh=25.567,
            total_consumption_kwh=30.234,
            grid_import_kwh=8.567,
            grid_export_kwh=3.891,
            battery_cycles=1.567,
            ev_charged_kwh=15.234,
            savings_kr=125.567,
            peak_kw=5.234,
            ellevio_cost_kr=45.321,
        )

        # kWh values should have 1 decimal (main metrics) or 2 (details)
        assert "25.6" in html  # PV production
        assert "30.2" in html  # Consumption

        # kW values should have 2 decimals
        assert "5.23" in html  # Peak

        # Currency rounded to 0 decimals (main) or 2 (details)
        assert "126" in html or "125" in html  # Savings (rounded)
        assert "45.32" in html  # Ellevio cost
