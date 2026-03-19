/**
 * CARMA Box — Custom Lovelace Card v2.0
 *
 * Features:
 * - Color-coded 24h plan bar (charge/discharge/grid charge/idle)
 * - Hover/tap tooltip with hour, price, SoC, action
 * - SVG SoC curve overlay
 * - EV Start/Stop buttons (calls HA services)
 * - Dark mode via HA CSS variables
 * - Responsive: grid → stack on mobile
 * - Config editor with entity prefix + section toggles
 *
 * No build step. Pure vanilla JS.
 */

const ACTION_COLORS = {
  c: { bg: "#4caf50", label: "Ladda", icon: "mdi:battery-charging" },
  d: { bg: "#2196f3", label: "Urladdning", icon: "mdi:battery-arrow-down" },
  g: { bg: "#ff9800", label: "Nätladdning", icon: "mdi:transmission-tower-import" },
  i: { bg: "#9e9e9e", label: "Vila", icon: "mdi:pause-circle-outline" },
};

const STATUS_MAP = {
  idle: { color: "#9e9e9e", icon: "mdi:pause-circle-outline", label: "Vilar" },
  charging: { color: "#4caf50", icon: "mdi:battery-charging", label: "Laddar" },
  charging_pv: { color: "#ff9800", icon: "mdi:solar-power", label: "Solladdar" },
  discharging: { color: "#2196f3", icon: "mdi:battery-arrow-down", label: "Urladdning" },
  standby: { color: "#607d8b", icon: "mdi:battery-check", label: "Standby" },
  unknown: { color: "#9e9e9e", icon: "mdi:help-circle-outline", label: "Okand" },
};

class CarmaboxCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tooltipEl = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    this._render();
  }

  setConfig(config) {
    this._config = {
      entity_prefix: "sensor.carmabox",
      show_ev: true,
      show_savings: true,
      show_plan: true,
      show_soc_curve: true,
      ev_current_entity: "",
      ...config,
    };
  }

  getCardSize() {
    return 5;
  }

  static getConfigElement() {
    return document.createElement("carmabox-card-editor");
  }

  static getStubConfig() {
    return { entity_prefix: "sensor.carmabox" };
  }

  _entity(suffix) {
    return `${this._config.entity_prefix}_${suffix}`;
  }

  _state(suffix) {
    const s = this._hass.states[this._entity(suffix)];
    return s ? s.state : null;
  }

  _attr(suffix, attr) {
    const s = this._hass.states[this._entity(suffix)];
    return s && s.attributes ? s.attributes[attr] : null;
  }

  _fmt(val, unit, fallback) {
    if (val == null || val === "None" || val === "unknown" || val === "unavailable") return fallback || "\u2014";
    return `${val}${unit || ""}`;
  }

  _render() {
    const status = this._state("plan_status") || "unknown";
    const si = STATUS_MAP[status] || STATUS_MAP.unknown;
    const target = this._state("target_kw");
    const batterySoc = this._state("battery_soc");
    const evSoc = this._state("ev_soc");
    const gridImport = this._state("grid_import");
    const savings = this._state("savings_month");
    const peakSavings = this._attr("savings_month", "peak_reduction_kr") || 0;
    const dischargeSavings = this._attr("savings_month", "discharge_savings_kr") || 0;
    const gridChargeSavings = this._attr("savings_month", "grid_charge_savings_kr") || 0;
    const planData = this._attr("plan_status", "plan") || [];
    const planHours = this._attr("plan_status", "plan_hours") || 0;
    const showEv = this._config.show_ev && evSoc != null && evSoc !== "None" && evSoc !== "-1";
    const showSavings = this._config.show_savings;
    const showPlan = this._config.show_plan;
    const showSocCurve = this._config.show_soc_curve;
    const nowHour = new Date().getHours();

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          --cb-charge: #4caf50;
          --cb-discharge: #2196f3;
          --cb-grid-charge: #ff9800;
          --cb-idle: #9e9e9e;
        }
        ha-card { overflow: visible; }
        .cb { padding: 16px; font-family: var(--ha-card-header-font-family, inherit); }

        /* Header */
        .cb-header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
        .cb-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
        .cb-title { font-size: 1.1em; font-weight: 500; flex: 1; color: var(--primary-text-color); }
        .cb-status { font-size: 0.85em; color: var(--secondary-text-color); display: flex; align-items: center; gap: 4px; }

        /* Metrics grid */
        .cb-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 10px; margin-bottom: 14px; }
        .cb-m { background: var(--card-background-color, var(--ha-card-background)); border: 1px solid var(--divider-color); border-radius: 10px; padding: 10px 8px; text-align: center; transition: transform 0.15s; }
        .cb-m:hover { transform: scale(1.03); }
        .cb-mv { font-size: 1.4em; font-weight: 600; color: var(--primary-text-color); }
        .cb-ml { font-size: 0.7em; color: var(--secondary-text-color); margin-top: 2px; }
        .cb-mi { color: var(--secondary-text-color); margin-bottom: 4px; }

        /* Savings */
        .cb-savings { background: var(--card-background-color, var(--ha-card-background)); border: 1px solid var(--divider-color); border-radius: 10px; padding: 12px; margin-bottom: 14px; }
        .cb-sav-hero { text-align: center; margin-bottom: 10px; }
        .cb-sav-total { font-size: 1.6em; font-weight: 700; color: var(--cb-charge); margin-top: 4px; }
        .cb-sav-label { font-size: 0.75em; color: var(--secondary-text-color); }
        .cb-sav-breakdown { display: flex; justify-content: space-around; margin-bottom: 10px; padding: 8px 0; border-top: 1px solid var(--divider-color); border-bottom: 1px solid var(--divider-color); }
        .cb-sav-cat { text-align: center; }
        .cb-sav-cat-val { font-size: 1.1em; font-weight: 600; color: var(--primary-text-color); }
        .cb-sav-cat-lbl { font-size: 0.65em; color: var(--secondary-text-color); }

        /* What-if */
        .cb-whatif { margin: 10px 0; padding: 8px; background: rgba(76,175,80,0.06); border-radius: 8px; }
        .cb-whatif-title { font-size: 0.7em; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
        .cb-whatif-row { display: flex; align-items: center; justify-content: space-around; }
        .cb-whatif-item { text-align: center; flex: 1; }
        .cb-whatif-arrow { flex: 0; padding: 0 8px; }
        .cb-whatif-val { font-size: 1.15em; font-weight: 600; }
        .cb-whatif-without .cb-whatif-val { color: var(--error-color, #f44336); }
        .cb-whatif-with .cb-whatif-val { color: var(--cb-charge); }
        .cb-whatif-lbl { font-size: 0.65em; color: var(--secondary-text-color); }

        /* Peak comparison */
        .cb-peaks { margin: 10px 0; }
        .cb-peaks-title { font-size: 0.7em; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
        .cb-peaks-legend { display: flex; gap: 12px; font-size: 0.65em; color: var(--secondary-text-color); margin-bottom: 6px; }
        .cb-peak-row { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
        .cb-peak-rank { font-size: 0.7em; color: var(--secondary-text-color); min-width: 20px; }
        .cb-peak-bars { flex: 1; display: flex; flex-direction: column; gap: 2px; }
        .cb-peak-bar { height: 14px; border-radius: 3px; font-size: 0.6em; line-height: 14px; padding-left: 4px; color: #fff; min-width: 32px; white-space: nowrap; }
        .cb-peak-actual { background: var(--cb-charge); }
        .cb-peak-baseline { background: var(--error-color, #f44336); opacity: 0.7; }

        /* Trend graph */
        .cb-trend { margin-top: 10px; }
        .cb-trend-title { font-size: 0.7em; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
        .cb-trend-svg { width: 100%; height: 50px; }
        .cb-trend-line { fill: none; stroke: var(--cb-charge); stroke-width: 1.5; }
        .cb-trend-area { fill: var(--cb-charge); opacity: 0.1; }
        .cb-trend-labels { display: flex; justify-content: space-between; font-size: 0.6em; color: var(--secondary-text-color); }

        /* Plan section */
        .cb-plan { margin-bottom: 8px; }
        .cb-plan-title { font-size: 0.75em; color: var(--secondary-text-color); margin-bottom: 6px; display: flex; justify-content: space-between; }
        .cb-plan-legend { display: flex; gap: 10px; font-size: 0.65em; }
        .cb-legend-dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; vertical-align: middle; margin-right: 3px; }

        /* Plan bar container */
        .cb-plan-wrap { position: relative; }
        .cb-bars { display: flex; height: 32px; border-radius: 6px; overflow: hidden; cursor: pointer; }
        .cb-bar { flex: 1; min-width: 0; position: relative; transition: opacity 0.15s; border-right: 1px solid rgba(0,0,0,0.08); }
        .cb-bar:last-child { border-right: none; }
        .cb-bar:hover { opacity: 0.75; }
        .cb-bar-now { position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--primary-color, #03a9f4); z-index: 5; pointer-events: none; }
        .cb-bar-hour { position: absolute; bottom: -14px; font-size: 0.55em; color: var(--secondary-text-color); left: 50%; transform: translateX(-50%); pointer-events: none; white-space: nowrap; }

        /* Hour labels below bars */
        .cb-hours { display: flex; height: 16px; margin-top: 2px; }
        .cb-h { flex: 1; min-width: 0; text-align: center; font-size: 0.55em; color: var(--secondary-text-color); }

        /* SoC SVG curve */
        .cb-soc-svg { width: 100%; height: 40px; margin-top: 4px; }
        .cb-soc-line { fill: none; stroke: var(--cb-charge); stroke-width: 1.5; }
        .cb-soc-area { fill: var(--cb-charge); opacity: 0.1; }
        .cb-soc-labels { display: flex; justify-content: space-between; font-size: 0.6em; color: var(--secondary-text-color); }

        /* Tooltip */
        .cb-tooltip { position: absolute; z-index: 100; background: var(--ha-card-background, #fff); color: var(--primary-text-color); border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px 10px; font-size: 0.8em; box-shadow: 0 2px 8px rgba(0,0,0,0.15); pointer-events: none; white-space: nowrap; opacity: 0; transition: opacity 0.15s; }
        .cb-tooltip.visible { opacity: 1; }
        .cb-tt-action { font-weight: 600; margin-bottom: 2px; }
        .cb-tt-row { display: flex; justify-content: space-between; gap: 12px; }

        /* EV buttons */
        .cb-ev { display: flex; gap: 8px; margin-top: 10px; }
        .cb-ev-btn { flex: 1; padding: 8px 0; border: none; border-radius: 8px; font-size: 0.85em; font-weight: 500; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; transition: opacity 0.15s; }
        .cb-ev-btn:hover { opacity: 0.85; }
        .cb-ev-start { background: var(--cb-charge); color: #fff; }
        .cb-ev-stop { background: var(--error-color, #f44336); color: #fff; }

        /* Plan empty */
        .cb-plan-empty { text-align: center; color: var(--secondary-text-color); font-size: 0.85em; padding: 16px 0; }

        /* Responsive: stack on mobile */
        @media (max-width: 400px) {
          .cb-metrics { grid-template-columns: 1fr 1fr; gap: 6px; }
          .cb-m { padding: 8px 4px; }
          .cb-mv { font-size: 1.2em; }
          .cb-sav-detail { flex-direction: column; align-items: center; gap: 2px; }
          .cb-bars { height: 28px; }
          .cb-soc-svg { height: 30px; }
          .cb-plan-legend { gap: 6px; font-size: 0.6em; }
        }
      </style>
      <ha-card>
        <div class="cb">
          ${this._renderHeader(si, status)}
          ${this._renderMetrics(target, gridImport, batterySoc, evSoc, planHours, showEv)}
          ${showSavings ? this._renderSavings(savings, peakSavings, dischargeSavings, gridChargeSavings) : ""}
          ${showPlan ? this._renderPlan(planData, planHours, nowHour, showSocCurve) : ""}
          ${showEv ? this._renderEvButtons() : ""}
        </div>
      </ha-card>
    `;

    // Attach tooltip listeners
    if (showPlan && planData.length > 0) {
      this._attachTooltipListeners(planData);
    }
  }

  _renderHeader(si, status) {
    return `
      <div class="cb-header">
        <div class="cb-dot" style="background:${si.color}"></div>
        <div class="cb-title">CARMA Box</div>
        <div class="cb-status">
          <ha-icon icon="${si.icon}" style="--mdc-icon-size:18px;color:${si.color}"></ha-icon>
          ${si.label}
        </div>
      </div>`;
  }

  _renderMetrics(target, gridImport, batterySoc, evSoc, planHours, showEv) {
    let cards = `
      <div class="cb-m">
        <div class="cb-mi"><ha-icon icon="mdi:target" style="--mdc-icon-size:20px"></ha-icon></div>
        <div class="cb-mv">${this._fmt(target, "")}</div>
        <div class="cb-ml">Effektmal kW</div>
      </div>
      <div class="cb-m">
        <div class="cb-mi"><ha-icon icon="mdi:transmission-tower-import" style="--mdc-icon-size:20px"></ha-icon></div>
        <div class="cb-mv">${this._fmt(gridImport, "")}</div>
        <div class="cb-ml">Grid Import kW</div>
      </div>
      <div class="cb-m">
        <div class="cb-mi"><ha-icon icon="mdi:battery" style="--mdc-icon-size:20px"></ha-icon></div>
        <div class="cb-mv">${this._fmt(batterySoc, "%")}</div>
        <div class="cb-ml">Batteri</div>
      </div>`;
    if (showEv) {
      cards += `
      <div class="cb-m">
        <div class="cb-mi"><ha-icon icon="mdi:car-electric" style="--mdc-icon-size:20px"></ha-icon></div>
        <div class="cb-mv">${this._fmt(evSoc, "%")}</div>
        <div class="cb-ml">Elbil</div>
      </div>`;
    } else {
      cards += `
      <div class="cb-m">
        <div class="cb-mi"><ha-icon icon="mdi:calendar-clock" style="--mdc-icon-size:20px"></ha-icon></div>
        <div class="cb-mv">${planHours}h</div>
        <div class="cb-ml">Planerat</div>
      </div>`;
    }
    return `<div class="cb-metrics">${cards}</div>`;
  }

  _renderSavings(savings, peak, discharge, gridCharge) {
    const whatif = this._attr("savings_month", "whatif") || {};
    const trend = this._attr("savings_month", "trend") || [];
    const peaks = this._attr("savings_month", "peaks") || {};

    return `
      <div class="cb-savings">
        <div class="cb-sav-hero">
          <ha-icon icon="mdi:piggy-bank" style="--mdc-icon-size:28px;color:var(--cb-charge)"></ha-icon>
          <div class="cb-sav-total">Sparat ${this._fmt(savings, " kr")}</div>
          <div class="cb-sav-label">denna manad</div>
        </div>

        <div class="cb-sav-breakdown">
          <div class="cb-sav-cat">
            <div class="cb-sav-cat-val">${peak} kr</div>
            <div class="cb-sav-cat-lbl">Effektavgift</div>
          </div>
          <div class="cb-sav-cat">
            <div class="cb-sav-cat-val">${discharge} kr</div>
            <div class="cb-sav-cat-lbl">Prisoptimering</div>
          </div>
          <div class="cb-sav-cat">
            <div class="cb-sav-cat-val">${gridCharge} kr</div>
            <div class="cb-sav-cat-lbl">Natladdning</div>
          </div>
        </div>

        ${this._renderWhatIf(whatif)}
        ${this._renderPeakComparison(peaks)}
        ${this._renderTrend(trend)}
      </div>`;
  }

  _renderWhatIf(whatif) {
    if (!whatif || !whatif.without_carma_kr) return "";
    return `
      <div class="cb-whatif">
        <div class="cb-whatif-title">What-if</div>
        <div class="cb-whatif-row">
          <div class="cb-whatif-item cb-whatif-without">
            <div class="cb-whatif-val">${Math.round(whatif.without_carma_kr)} kr</div>
            <div class="cb-whatif-lbl">Utan CARMA Box</div>
          </div>
          <div class="cb-whatif-arrow">
            <ha-icon icon="mdi:arrow-right" style="--mdc-icon-size:20px;color:var(--cb-charge)"></ha-icon>
          </div>
          <div class="cb-whatif-item cb-whatif-with">
            <div class="cb-whatif-val">${Math.round(whatif.with_carma_kr)} kr</div>
            <div class="cb-whatif-lbl">Med CARMA Box</div>
          </div>
        </div>
      </div>`;
  }

  _renderPeakComparison(peaks) {
    if (!peaks || !peaks.baseline || !peaks.baseline.length) return "";
    const actual = peaks.actual || [];
    const baseline = peaks.baseline || [];
    const maxPeak = Math.max(...baseline, ...actual, 1);

    let rows = "";
    for (let i = 0; i < Math.max(actual.length, baseline.length); i++) {
      const a = actual[i] != null ? actual[i] : 0;
      const b = baseline[i] != null ? baseline[i] : 0;
      const aPct = Math.round((a / maxPeak) * 100);
      const bPct = Math.round((b / maxPeak) * 100);
      rows += `
        <div class="cb-peak-row">
          <span class="cb-peak-rank">#${i + 1}</span>
          <div class="cb-peak-bars">
            <div class="cb-peak-bar cb-peak-actual" style="width:${aPct}%">${a} kW</div>
            <div class="cb-peak-bar cb-peak-baseline" style="width:${bPct}%">${b} kW</div>
          </div>
        </div>`;
    }
    return `
      <div class="cb-peaks">
        <div class="cb-peaks-title">Effekttoppar: Dina vs Utan CARMA</div>
        <div class="cb-peaks-legend">
          <span><span class="cb-legend-dot" style="background:var(--cb-charge)"></span>Med</span>
          <span><span class="cb-legend-dot" style="background:var(--error-color,#f44336)"></span>Utan</span>
        </div>
        ${rows}
      </div>`;
  }

  _renderTrend(trend) {
    if (!trend || trend.length < 2) return "";
    const maxVal = Math.max(...trend.map(d => d.total_kr), 1);
    const w = 100;
    const h = 50;
    const n = trend.length;

    // Build SVG polyline points
    const points = trend.map((d, i) => {
      const x = (i / (n - 1)) * w;
      const y = h - (d.total_kr / maxVal) * (h - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const linePoints = points.join(" ");
    const areaPoints = `0,${h} ${linePoints} ${w},${h}`;

    const first = trend[0];
    const last = trend[trend.length - 1];

    return `
      <div class="cb-trend">
        <div class="cb-trend-title">Sparande senaste ${n} dagar</div>
        <svg class="cb-trend-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
          <polygon class="cb-trend-area" points="${areaPoints}" />
          <polyline class="cb-trend-line" points="${linePoints}" />
        </svg>
        <div class="cb-trend-labels">
          <span>${first.date.slice(5)}</span>
          <span>${last.total_kr} kr</span>
          <span>${last.date.slice(5)}</span>
        </div>
      </div>`;
  }

  _renderPlan(planData, planHours, nowHour, showSocCurve) {
    if (!planData || planData.length === 0) {
      return `<div class="cb-plan"><div class="cb-plan-empty">Ingen plan tillganglig</div></div>`;
    }

    // Legend
    const legend = `
      <div class="cb-plan-legend">
        <span><span class="cb-legend-dot" style="background:var(--cb-charge)"></span>Ladda</span>
        <span><span class="cb-legend-dot" style="background:var(--cb-discharge)"></span>Urladda</span>
        <span><span class="cb-legend-dot" style="background:var(--cb-grid-charge)"></span>Natladdning</span>
        <span><span class="cb-legend-dot" style="background:var(--cb-idle)"></span>Vila</span>
      </div>`;

    // Bars
    const maxPrice = Math.max(...planData.map(h => h.p), 1);
    let barsHtml = "";
    let nowMarker = "";

    for (let idx = 0; idx < planData.length; idx++) {
      const hp = planData[idx];
      const ac = ACTION_COLORS[hp.a] || ACTION_COLORS.i;
      // Height based on price relative to max (min 20%)
      const heightPct = Math.max(20, (hp.p / maxPrice) * 100);
      const isNow = hp.h === nowHour;
      barsHtml += `<div class="cb-bar" data-idx="${idx}" style="background:${ac.bg};opacity:${isNow ? 1 : 0.7};">${isNow ? '<div class="cb-bar-now"></div>' : ""}</div>`;
    }

    // Hour labels (show every 3rd or 6th depending on count)
    const step = planData.length > 12 ? 3 : 1;
    let hoursHtml = "";
    for (let idx = 0; idx < planData.length; idx++) {
      const label = idx % step === 0 ? `${String(planData[idx].h).padStart(2, "0")}` : "";
      hoursHtml += `<div class="cb-h">${label}</div>`;
    }

    // SoC curve SVG
    let socSvg = "";
    if (showSocCurve && planData.length > 1) {
      const w = 100; // viewBox width percentage
      const h = 40;
      const n = planData.length;
      const points = planData.map((hp, i) => {
        const x = (i / (n - 1)) * w;
        const y = h - (hp.soc / 100) * h;
        return `${x},${y}`;
      });
      const linePoints = points.join(" ");
      const areaPoints = `0,${h} ${linePoints} ${w},${h}`;
      const minSoc = Math.min(...planData.map(h => h.soc));
      const maxSoc = Math.max(...planData.map(h => h.soc));

      socSvg = `
        <svg class="cb-soc-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
          <polygon class="cb-soc-area" points="${areaPoints}" />
          <polyline class="cb-soc-line" points="${linePoints}" />
        </svg>
        <div class="cb-soc-labels">
          <span>SoC: ${minSoc}%</span>
          <span>${maxSoc}%</span>
        </div>`;
    }

    return `
      <div class="cb-plan">
        <div class="cb-plan-title">
          <span>Plan (${planHours} timmar)</span>
          ${legend}
        </div>
        <div class="cb-plan-wrap">
          <div class="cb-bars">${barsHtml}</div>
          <div class="cb-hours">${hoursHtml}</div>
          ${socSvg}
          <div class="cb-tooltip" id="cb-tip"></div>
        </div>
      </div>`;
  }

  _renderEvButtons() {
    return `
      <div class="cb-ev">
        <button class="cb-ev-btn cb-ev-start" id="cb-ev-start">
          <ha-icon icon="mdi:ev-station" style="--mdc-icon-size:18px"></ha-icon>
          Start 6A
        </button>
        <button class="cb-ev-btn cb-ev-stop" id="cb-ev-stop">
          <ha-icon icon="mdi:stop-circle" style="--mdc-icon-size:18px"></ha-icon>
          Stop
        </button>
      </div>`;
  }

  _attachTooltipListeners(planData) {
    const root = this.shadowRoot;
    const bars = root.querySelectorAll(".cb-bar");
    const tooltip = root.getElementById("cb-tip");
    if (!tooltip) return;

    const showTip = (e, idx) => {
      const hp = planData[idx];
      if (!hp) return;
      const ac = ACTION_COLORS[hp.a] || ACTION_COLORS.i;
      tooltip.innerHTML = `
        <div class="cb-tt-action" style="color:${ac.bg}">${ac.label}</div>
        <div class="cb-tt-row"><span>Timme:</span><span>${String(hp.h).padStart(2, "0")}:00</span></div>
        <div class="cb-tt-row"><span>Pris:</span><span>${hp.p} ore/kWh</span></div>
        <div class="cb-tt-row"><span>SoC:</span><span>${hp.soc}%</span></div>
        <div class="cb-tt-row"><span>Grid:</span><span>${hp.grid} kW</span></div>
        <div class="cb-tt-row"><span>Batteri:</span><span>${hp.bat} kW</span></div>
      `;
      // Position tooltip
      const bar = bars[idx];
      const rect = bar.getBoundingClientRect();
      const wrapRect = root.querySelector(".cb-plan-wrap").getBoundingClientRect();
      let left = rect.left - wrapRect.left + rect.width / 2;
      // Clamp to container
      const tipWidth = 160;
      if (left + tipWidth / 2 > wrapRect.width) left = wrapRect.width - tipWidth / 2;
      if (left - tipWidth / 2 < 0) left = tipWidth / 2;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `-70px`;
      tooltip.style.transform = "translateX(-50%)";
      tooltip.classList.add("visible");
    };

    const hideTip = () => {
      tooltip.classList.remove("visible");
    };

    bars.forEach((bar) => {
      const idx = parseInt(bar.dataset.idx, 10);
      bar.addEventListener("mouseenter", (e) => showTip(e, idx));
      bar.addEventListener("mouseleave", hideTip);
      bar.addEventListener("touchstart", (e) => {
        e.preventDefault();
        showTip(e, idx);
        setTimeout(hideTip, 2500);
      }, { passive: false });
    });

    // EV buttons
    const startBtn = root.getElementById("cb-ev-start");
    const stopBtn = root.getElementById("cb-ev-stop");
    if (startBtn) {
      startBtn.addEventListener("click", () => this._evStart());
    }
    if (stopBtn) {
      stopBtn.addEventListener("click", () => this._evStop());
    }
  }

  _evStart() {
    if (!this._hass) return;
    const chargerId = this._config.ev_charger_id;
    if (chargerId) {
      this._hass.callService("easee", "set_charger_dynamic_limit", {
        charger_id: chargerId,
        current: 6,
      });
    }
  }

  _evStop() {
    if (!this._hass) return;
    const chargerId = this._config.ev_charger_id;
    if (chargerId) {
      this._hass.callService("easee", "set_charger_dynamic_limit", {
        charger_id: chargerId,
        current: 0,
      });
    }
  }
}

/* ─── Config Editor ─────────────────────────────────────── */

class CarmaboxCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  _render() {
    const c = this._config;
    this.shadowRoot.innerHTML = `
      <style>
        .editor { padding: 16px; }
        .editor label { display: block; margin-bottom: 12px; font-size: 0.9em; color: var(--primary-text-color); }
        .editor input[type=text] { width: 100%; padding: 6px 8px; border: 1px solid var(--divider-color); border-radius: 4px; background: var(--card-background-color); color: var(--primary-text-color); font-size: 0.9em; box-sizing: border-box; }
        .editor .toggle-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .editor .section-title { font-size: 0.75em; color: var(--secondary-text-color); margin-top: 12px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
      </style>
      <div class="editor">
        <label>
          Entity-prefix
          <input type="text" id="prefix" value="${c.entity_prefix || "sensor.carmabox"}" />
        </label>
        <label>
          EV ström-entity (for start/stop)
          <input type="text" id="ev_entity" value="${c.ev_current_entity || ""}" placeholder="number.easee_home_12840_dynamic_charger_limit" />
        </label>

        <div class="section-title">Visa sektioner</div>
        <div class="toggle-row"><span>Besparingar</span><input type="checkbox" id="show_savings" ${c.show_savings !== false ? "checked" : ""} /></div>
        <div class="toggle-row"><span>24h plan</span><input type="checkbox" id="show_plan" ${c.show_plan !== false ? "checked" : ""} /></div>
        <div class="toggle-row"><span>SoC-kurva</span><input type="checkbox" id="show_soc_curve" ${c.show_soc_curve !== false ? "checked" : ""} /></div>
        <div class="toggle-row"><span>Elbil</span><input type="checkbox" id="show_ev" ${c.show_ev !== false ? "checked" : ""} /></div>
      </div>
    `;

    // Bind events
    const fire = () => {
      const event = new CustomEvent("config-changed", {
        detail: { config: this._config },
        bubbles: true,
        composed: true,
      });
      this.dispatchEvent(event);
    };

    this.shadowRoot.getElementById("prefix").addEventListener("input", (e) => {
      this._config = { ...this._config, entity_prefix: e.target.value };
      fire();
    });
    this.shadowRoot.getElementById("ev_entity").addEventListener("input", (e) => {
      this._config = { ...this._config, ev_current_entity: e.target.value };
      fire();
    });
    ["show_savings", "show_plan", "show_soc_curve", "show_ev"].forEach((key) => {
      this.shadowRoot.getElementById(key).addEventListener("change", (e) => {
        this._config = { ...this._config, [key]: e.target.checked };
        fire();
      });
    });
  }
}

customElements.define("carmabox-card", CarmaboxCard);
customElements.define("carmabox-card-editor", CarmaboxCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "carmabox-card",
  name: "CARMA Box",
  description: "Energy optimizer dashboard card with 24h plan visualization",
  preview: true,
  documentationURL: "https://git.malmgrens.me/bormal/carmabox",
});
