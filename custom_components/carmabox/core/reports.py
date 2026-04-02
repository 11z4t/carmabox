"""HTML report generation for Carmabox energy statistics.

Pure Python functions for generating daily and weekly reports with inline styles.
No external dependencies required.
"""

from datetime import date
from typing import Any


def generate_daily_report_html(
    date_obj: date,
    total_pv_kwh: float,
    total_consumption_kwh: float,
    grid_import_kwh: float,
    grid_export_kwh: float,
    battery_cycles: float,
    ev_charged_kwh: float,
    savings_kr: float,
    peak_kw: float,
    ellevio_cost_kr: float,
) -> str:
    """Generate HTML report for daily energy statistics.

    Args:
        date_obj: Date for the report
        total_pv_kwh: Total PV production in kWh
        total_consumption_kwh: Total household consumption in kWh
        grid_import_kwh: Grid import in kWh
        grid_export_kwh: Grid export in kWh
        battery_cycles: Number of battery charge/discharge cycles
        ev_charged_kwh: EV charging energy in kWh
        savings_kr: Estimated savings in SEK
        peak_kw: Peak power consumption in kW
        ellevio_cost_kr: Ellevio grid cost in SEK

    Returns:
        HTML string with inline styles
    """
    # Calculate derived metrics
    self_consumption_pct = 0.0
    if total_pv_kwh > 0:
        self_consumption_pct = ((total_pv_kwh - grid_export_kwh) / total_pv_kwh) * 100

    self_sufficiency_pct = 0.0
    if total_consumption_kwh > 0:
        self_sufficiency_pct = (
            (total_consumption_kwh - grid_import_kwh) / total_consumption_kwh
        ) * 100

    # Format date in Swedish
    date_str = date_obj.strftime("%Y-%m-%d")
    weekday_names = ["Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"]
    weekday = weekday_names[date_obj.weekday()]

    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daglig Energirapport - {date_str}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont,
                'Segoe UI', Roboto, 'Helvetica Neue', Arial,
                sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 28px;
            margin-bottom: 10px;
            font-weight: 600;
        }}
        .header p {{
            font-size: 16px;
            opacity: 0.9;
        }}
        .content {{
            padding: 30px;
        }}
        .metric-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .metric-card {{
            background: #f8f9fa;
            border-radius: 12px;
            padding: 20px;
            text-align: center;
            border-left: 4px solid #667eea;
        }}
        .metric-card.positive {{
            border-left-color: #10b981;
        }}
        .metric-card.warning {{
            border-left-color: #f59e0b;
        }}
        .metric-card.negative {{
            border-left-color: #ef4444;
        }}
        .metric-label {{
            font-size: 13px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
            font-weight: 500;
        }}
        .metric-value {{
            font-size: 32px;
            font-weight: 700;
            color: #1f2937;
            line-height: 1;
        }}
        .metric-unit {{
            font-size: 16px;
            color: #6b7280;
            font-weight: 400;
            margin-left: 4px;
        }}
        .section-title {{
            font-size: 20px;
            font-weight: 600;
            color: #1f2937;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e5e7eb;
        }}
        .summary-row {{
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid #e5e7eb;
        }}
        .summary-row:last-child {{
            border-bottom: none;
        }}
        .summary-label {{
            color: #6b7280;
            font-size: 15px;
        }}
        .summary-value {{
            font-weight: 600;
            color: #1f2937;
            font-size: 15px;
        }}
        .footer {{
            background: #f8f9fa;
            padding: 20px 30px;
            text-align: center;
            color: #6b7280;
            font-size: 14px;
        }}
        @media (max-width: 600px) {{
            body {{
                padding: 10px;
            }}
            .header h1 {{
                font-size: 24px;
            }}
            .content {{
                padding: 20px;
            }}
            .metric-grid {{
                grid-template-columns: 1fr;
                gap: 15px;
            }}
            .metric-value {{
                font-size: 28px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚡ Daglig Energirapport</h1>
            <p>{weekday}, {date_str}</p>
        </div>

        <div class="content">
            <div class="section-title">📊 Nyckeltal</div>
            <div class="metric-grid">
                <div class="metric-card positive">
                    <div class="metric-label">☀️ Solproduktion</div>
                    <div class="metric-value">{total_pv_kwh:.1f}<span
                        class="metric-unit">kWh</span></div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">🏠 Förbrukning</div>
                    <div class="metric-value">{total_consumption_kwh:.1f}<span
                        class="metric-unit">kWh</span></div>
                </div>
                <div class="metric-card warning">
                    <div class="metric-label">⚡ Max Effekt</div>
                    <div class="metric-value">{peak_kw:.2f}<span class="metric-unit">kW</span></div>
                </div>
                <div class="metric-card positive">
                    <div class="metric-label">💰 Besparingar</div>
                    <div class="metric-value">{savings_kr:.0f}<span
                        class="metric-unit">kr</span></div>
                </div>
            </div>

            <div class="section-title">📈 Självförsörjning</div>
            <div class="metric-grid">
                <div class="metric-card positive">
                    <div class="metric-label">🔋 Egenförbrukning</div>
                    <div class="metric-value"
                        >{self_consumption_pct:.0f}<span class="metric-unit">%</span></div>
                </div>
                <div class="metric-card positive">
                    <div class="metric-label">🏡 Självförsörjning</div>
                    <div class="metric-value"
                        >{self_sufficiency_pct:.0f}<span class="metric-unit">%</span></div>
                </div>
            </div>

            <div class="section-title">🔌 Energiflöden</div>
            <div class="summary-row">
                <span class="summary-label">📥 Import från nät</span>
                <span class="summary-value">{grid_import_kwh:.2f} kWh</span>
            </div>
            <div class="summary-row">
                <span class="summary-label">📤 Export till nät</span>
                <span class="summary-value">{grid_export_kwh:.2f} kWh</span>
            </div>
            <div class="summary-row">
                <span class="summary-label">🔋 Batteriladdningar</span>
                <span class="summary-value">{battery_cycles:.2f} cykler</span>
            </div>
            <div class="summary-row">
                <span class="summary-label">🚗 EV-laddning</span>
                <span class="summary-value">{ev_charged_kwh:.2f} kWh</span>
            </div>

            <div class="section-title">💸 Kostnader</div>
            <div class="summary-row">
                <span class="summary-label">🏢 Ellevio nätavgift</span>
                <span class="summary-value">{ellevio_cost_kr:.2f} kr</span>
            </div>
            <div class="summary-row">
                <span class="summary-label">💰 Total besparing</span>
                <span class="summary-value">{savings_kr:.2f} kr</span>
            </div>
        </div>

        <div class="footer">
            Genererad av Carmabox Energy Management System
        </div>
    </div>
</body>
</html>"""

    return html


def generate_weekly_report_html(
    week_number: int,
    daily_summaries: list[dict[str, Any]],
    total_savings_kr: float,
    avg_peak_kw: float,
    pv_total_kwh: float,
) -> str:
    """Generate HTML report for weekly energy statistics.

    Args:
        week_number: ISO week number
        daily_summaries: List of daily summary dicts with keys:
            - date: date object
            - pv_kwh: float
            - consumption_kwh: float
            - grid_import_kwh: float
            - savings_kr: float
        total_savings_kr: Total savings for the week in SEK
        avg_peak_kw: Average peak power for the week in kW
        pv_total_kwh: Total PV production for the week in kWh

    Returns:
        HTML string with inline styles
    """
    # Calculate weekly aggregates
    total_consumption_kwh = sum(d.get("consumption_kwh", 0) for d in daily_summaries)
    total_grid_import_kwh = sum(d.get("grid_import_kwh", 0) for d in daily_summaries)

    avg_self_sufficiency_pct = 0.0
    if total_consumption_kwh > 0:
        avg_self_sufficiency_pct = (
            (total_consumption_kwh - total_grid_import_kwh) / total_consumption_kwh
        ) * 100

    # Find best and worst days
    best_day = max(daily_summaries, key=lambda d: d.get("pv_kwh", 0), default=None)
    worst_day = min(daily_summaries, key=lambda d: d.get("pv_kwh", 0), default=None)

    # Pre-compute best/worst day values for template
    best_pv = best_day.get("pv_kwh", 0) if best_day else 0
    _best_date = best_day.get("date") if best_day else None
    best_date_str = _best_date.strftime("%Y-%m-%d") if _best_date is not None else "-"
    worst_pv = worst_day.get("pv_kwh", 0) if worst_day else 0
    _worst_date = worst_day.get("date") if worst_day else None
    worst_date_str = _worst_date.strftime("%Y-%m-%d") if _worst_date is not None else "-"

    # Get date range
    if daily_summaries:
        start_date = daily_summaries[0].get("date")
        end_date = daily_summaries[-1].get("date")
        if start_date is not None and end_date is not None:
            date_range = f"{start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')}"
        else:
            date_range = "Ingen data"
    else:
        date_range = "Ingen data"

    # Generate daily rows HTML
    daily_rows_html = ""
    for day in daily_summaries:
        day_date = day.get("date")
        if not day_date:
            continue

        weekday_names = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
        weekday = weekday_names[day_date.weekday()]
        date_str = day_date.strftime("%Y-%m-%d")

        pv = day.get("pv_kwh", 0)
        consumption = day.get("consumption_kwh", 0)
        grid = day.get("grid_import_kwh", 0)
        savings = day.get("savings_kr", 0)

        daily_rows_html += f"""
            <div class="daily-row">
                <div class="daily-date">{weekday}<br><small>{date_str}</small></div>
                <div class="daily-stat">
                    <div class="stat-value">{pv:.1f}</div>
                    <div class="stat-label">kWh PV</div>
                </div>
                <div class="daily-stat">
                    <div class="stat-value">{consumption:.1f}</div>
                    <div class="stat-label">kWh förbrukning</div>
                </div>
                <div class="daily-stat">
                    <div class="stat-value">{grid:.1f}</div>
                    <div class="stat-label">kWh import</div>
                </div>
                <div class="daily-stat">
                    <div class="stat-value">{savings:.0f}</div>
                    <div class="stat-label">kr sparade</div>
                </div>
            </div>"""

    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Veckorapport - Vecka {week_number}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont,
                'Segoe UI', Roboto, 'Helvetica Neue', Arial,
                sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 30px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 32px;
            margin-bottom: 10px;
            font-weight: 600;
        }}
        .header p {{
            font-size: 16px;
            opacity: 0.9;
        }}
        .content {{
            padding: 30px;
        }}
        .metric-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }}
        .metric-card {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border-radius: 12px;
            padding: 25px;
            text-align: center;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
        }}
        .metric-label {{
            font-size: 14px;
            opacity: 0.9;
            margin-bottom: 10px;
            font-weight: 500;
        }}
        .metric-value {{
            font-size: 36px;
            font-weight: 700;
            line-height: 1;
        }}
        .metric-unit {{
            font-size: 18px;
            font-weight: 400;
            margin-left: 4px;
            opacity: 0.9;
        }}
        .section-title {{
            font-size: 22px;
            font-weight: 600;
            color: #1f2937;
            margin-bottom: 25px;
            padding-bottom: 12px;
            border-bottom: 3px solid #667eea;
        }}
        .daily-row {{
            display: grid;
            grid-template-columns: 120px repeat(4, 1fr);
            gap: 15px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
            margin-bottom: 15px;
            align-items: center;
        }}
        .daily-date {{
            font-weight: 600;
            color: #667eea;
            font-size: 16px;
        }}
        .daily-date small {{
            font-size: 12px;
            color: #6b7280;
            font-weight: 400;
        }}
        .daily-stat {{
            text-align: center;
        }}
        .stat-value {{
            font-size: 20px;
            font-weight: 700;
            color: #1f2937;
            margin-bottom: 4px;
        }}
        .stat-label {{
            font-size: 12px;
            color: #6b7280;
        }}
        .highlight-box {{
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            padding: 25px;
            border-radius: 12px;
            margin-bottom: 30px;
        }}
        .highlight-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
        }}
        .highlight-item {{
            text-align: center;
        }}
        .highlight-label {{
            font-size: 13px;
            opacity: 0.9;
            margin-bottom: 8px;
        }}
        .highlight-value {{
            font-size: 28px;
            font-weight: 700;
        }}
        .footer {{
            background: #f8f9fa;
            padding: 25px 30px;
            text-align: center;
            color: #6b7280;
            font-size: 14px;
        }}
        @media (max-width: 768px) {{
            body {{
                padding: 10px;
            }}
            .header h1 {{
                font-size: 26px;
            }}
            .content {{
                padding: 20px;
            }}
            .metric-grid {{
                grid-template-columns: 1fr;
            }}
            .daily-row {{
                grid-template-columns: 1fr;
                gap: 10px;
                padding: 15px;
            }}
            .daily-date {{
                border-bottom: 1px solid #e5e7eb;
                padding-bottom: 10px;
            }}
            .highlight-grid {{
                grid-template-columns: 1fr;
                gap: 15px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📅 Veckorapport - Vecka {week_number}</h1>
            <p>{date_range}</p>
        </div>

        <div class="content">
            <div class="highlight-box">
                <div class="highlight-grid">
                    <div class="highlight-item">
                        <div class="highlight-label">💰 Total Besparing</div>
                        <div class="highlight-value">{total_savings_kr:.0f} kr</div>
                    </div>
                    <div class="highlight-item">
                        <div class="highlight-label">☀️ Total Solproduktion</div>
                        <div class="highlight-value">{pv_total_kwh:.1f} kWh</div>
                    </div>
                    <div class="highlight-item">
                        <div class="highlight-label">🏡 Självförsörjning</div>
                        <div class="highlight-value">{avg_self_sufficiency_pct:.0f}%</div>
                    </div>
                </div>
            </div>

            <div class="section-title">📊 Veckans Översikt</div>
            <div class="metric-grid">
                <div class="metric-card">
                    <div class="metric-label">🏠 Total Förbrukning</div>
                    <div class="metric-value">{total_consumption_kwh:.1f}<span
                        class="metric-unit">kWh</span></div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">📥 Total Import</div>
                    <div class="metric-value">{total_grid_import_kwh:.1f}<span
                        class="metric-unit">kWh</span></div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">⚡ Genomsnittlig Topp</div>
                    <div class="metric-value">{avg_peak_kw:.2f}<span
                        class="metric-unit">kW</span></div>
                </div>
            </div>

            <div class="section-title">📆 Daglig Uppdelning</div>
            {daily_rows_html}

            <div class="section-title">🏆 Veckans Höjdpunkter</div>
            <div class="metric-grid">
                <div class="metric-card">
                    <div class="metric-label">☀️ Bästa Soldagen</div>
                    <div class="metric-value">{best_pv:.1f}<span
                        class="metric-unit">kWh</span></div>
                    <div class="metric-label"
                        style="margin-top: 10px; font-size: 12px;">
                        {best_date_str}
                    </div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">🌥️ Lägsta Soldagen</div>
                    <div class="metric-value">{worst_pv:.1f}<span
                        class="metric-unit">kWh</span></div>
                    <div class="metric-label"
                        style="margin-top: 10px; font-size: 12px;">
                        {worst_date_str}
                    </div>
                </div>
            </div>
        </div>

        <div class="footer">
            Genererad av Carmabox Energy Management System<br>
            Vecka {week_number} • {len(daily_summaries)} dagar analyserade
        </div>
    </div>
</body>
</html>"""

    return html
