/**
 * CARMA Box — Custom Lovelace Card
 *
 * Displays: status, target, battery SoC, EV SoC, savings, 24h plan bar.
 * No build step. Pure vanilla JS. Works with any HA >= 2024.4.
 */

class CarmaboxCard extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    this._render();
  }

  setConfig(config) {
    this._config = config;
    this._entityPrefix = config.entity_prefix || "sensor.carmabox";
  }

  getCardSize() {
    return 4;
  }

  static getConfigElement() {
    return document.createElement("carmabox-card-editor");
  }

  static getStubConfig() {
    return { entity_prefix: "sensor.carmabox" };
  }

  _getState(suffix) {
    const entityId = `${this._entityPrefix}_${suffix}`;
    const state = this._hass.states[entityId];
    return state ? state.state : "—";
  }

  _getAttr(suffix, attr) {
    const entityId = `${this._entityPrefix}_${suffix}`;
    const state = this._hass.states[entityId];
    return state && state.attributes ? state.attributes[attr] : null;
  }

  _statusIcon(status) {
    const icons = {
      idle: "mdi:pause-circle-outline",
      charging: "mdi:battery-charging",
      charging_pv: "mdi:solar-power",
      discharging: "mdi:battery-arrow-down",
      standby: "mdi:battery-check",
      unknown: "mdi:help-circle-outline",
    };
    return icons[status] || icons.unknown;
  }

  _statusColor(status) {
    const colors = {
      idle: "#9e9e9e",
      charging: "#4caf50",
      charging_pv: "#ff9800",
      discharging: "#2196f3",
      standby: "#607d8b",
      unknown: "#9e9e9e",
    };
    return colors[status] || colors.unknown;
  }

  _statusLabel(status) {
    const labels = {
      idle: "Vilar",
      charging: "Laddar",
      charging_pv: "Solladdar",
      discharging: "Urladdning",
      standby: "Standby",
      unknown: "Okänd",
    };
    return labels[status] || status;
  }

  _buildPlanBar() {
    const planHours = this._getAttr("plan_status", "plan_hours") || 0;
    if (planHours === 0) return "<div class='plan-empty'>Ingen plan</div>";

    // Get plan from status sensor attributes (simplified visualization)
    const target = this._getAttr("plan_status", "target_weighted_kw") || 2.0;
    const bars = [];
    for (let i = 0; i < Math.min(planHours, 24); i++) {
      bars.push(`<div class="plan-bar" style="flex:1;height:20px;background:var(--primary-color);opacity:0.3;border-radius:2px;margin:0 1px;"></div>`);
    }
    return `<div class="plan-row">${bars.join("")}</div>`;
  }

  _render() {
    const status = this._getState("plan_status");
    const target = this._getState("target_kw");
    const batterySoc = this._getState("battery_soc");
    const evSoc = this._getState("ev_soc");
    const gridImport = this._getState("grid_import");
    const savings = this._getState("savings_month");
    const planHours = this._getAttr("plan_status", "plan_hours") || 0;

    // Savings breakdown
    const peakSavings = this._getAttr("savings_month", "peak_reduction_kr") || 0;
    const dischargeSavings = this._getAttr("savings_month", "discharge_savings_kr") || 0;
    const gridChargeSavings = this._getAttr("savings_month", "grid_charge_savings_kr") || 0;

    const statusColor = this._statusColor(status);
    const statusIcon = this._statusIcon(status);
    const statusLabel = this._statusLabel(status);

    const showEv = evSoc !== "—" && evSoc !== null && evSoc !== "None";

    this.innerHTML = `
      <ha-card>
        <style>
          .cb-card { padding: 16px; }
          .cb-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
          .cb-status-dot { width: 12px; height: 12px; border-radius: 50%; }
          .cb-title { font-size: 1.1em; font-weight: 500; flex: 1; }
          .cb-status-label { font-size: 0.9em; color: var(--secondary-text-color); }
          .cb-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
          .cb-metric { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 8px; padding: 12px; text-align: center; }
          .cb-metric-value { font-size: 1.5em; font-weight: 600; }
          .cb-metric-label { font-size: 0.75em; color: var(--secondary-text-color); margin-top: 4px; }
          .cb-metric-icon { font-size: 1.2em; margin-bottom: 4px; }
          .cb-savings { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 8px; padding: 12px; margin-bottom: 16px; }
          .cb-savings-total { font-size: 1.3em; font-weight: 600; text-align: center; color: #4caf50; }
          .cb-savings-label { font-size: 0.75em; color: var(--secondary-text-color); text-align: center; }
          .cb-savings-detail { display: flex; justify-content: space-between; font-size: 0.75em; color: var(--secondary-text-color); margin-top: 8px; }
          .cb-plan { margin-top: 8px; }
          .cb-plan-label { font-size: 0.75em; color: var(--secondary-text-color); margin-bottom: 4px; }
          .plan-row { display: flex; align-items: flex-end; height: 24px; }
          .plan-empty { font-size: 0.8em; color: var(--secondary-text-color); text-align: center; padding: 8px; }
        </style>
        <div class="cb-card">
          <div class="cb-header">
            <div class="cb-status-dot" style="background:${statusColor}"></div>
            <div class="cb-title">CARMA Box</div>
            <ha-icon icon="${statusIcon}" style="color:${statusColor}"></ha-icon>
            <div class="cb-status-label">${statusLabel}</div>
          </div>

          <div class="cb-grid">
            <div class="cb-metric">
              <div class="cb-metric-icon"><ha-icon icon="mdi:target"></ha-icon></div>
              <div class="cb-metric-value">${target} kW</div>
              <div class="cb-metric-label">Effektmål</div>
            </div>
            <div class="cb-metric">
              <div class="cb-metric-icon"><ha-icon icon="mdi:transmission-tower-import"></ha-icon></div>
              <div class="cb-metric-value">${gridImport} kW</div>
              <div class="cb-metric-label">Grid Import</div>
            </div>
            <div class="cb-metric">
              <div class="cb-metric-icon"><ha-icon icon="mdi:battery"></ha-icon></div>
              <div class="cb-metric-value">${batterySoc}%</div>
              <div class="cb-metric-label">Batteri</div>
            </div>
            ${showEv ? `
            <div class="cb-metric">
              <div class="cb-metric-icon"><ha-icon icon="mdi:car-electric"></ha-icon></div>
              <div class="cb-metric-value">${evSoc}%</div>
              <div class="cb-metric-label">Elbil</div>
            </div>
            ` : `
            <div class="cb-metric">
              <div class="cb-metric-icon"><ha-icon icon="mdi:calendar-clock"></ha-icon></div>
              <div class="cb-metric-value">${planHours}h</div>
              <div class="cb-metric-label">Planerat</div>
            </div>
            `}
          </div>

          <div class="cb-savings">
            <div class="cb-savings-total">${savings} kr</div>
            <div class="cb-savings-label">Besparing denna månad</div>
            <div class="cb-savings-detail">
              <span>Effekt: ${peakSavings} kr</span>
              <span>Pris: ${dischargeSavings} kr</span>
              <span>Nät: ${gridChargeSavings} kr</span>
            </div>
          </div>

          <div class="cb-plan">
            <div class="cb-plan-label">Plan (${planHours} timmar)</div>
            ${this._buildPlanBar()}
          </div>
        </div>
      </ha-card>
    `;
  }
}

class CarmaboxCardEditor extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  _render() {
    this.innerHTML = `
      <div style="padding: 16px;">
        <p>CARMA Box-kortet konfigureras automatiskt.</p>
        <p style="font-size: 0.85em; color: var(--secondary-text-color);">
          Sensorerna hittas via prefixet <code>sensor.carmabox_</code>.
        </p>
      </div>
    `;
  }

  get _entityPrefix() {
    return this._config?.entity_prefix || "sensor.carmabox";
  }
}

customElements.define("carmabox-card", CarmaboxCard);
customElements.define("carmabox-card-editor", CarmaboxCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "carmabox-card",
  name: "CARMA Box",
  description: "Energy optimizer dashboard card",
  preview: true,
});
