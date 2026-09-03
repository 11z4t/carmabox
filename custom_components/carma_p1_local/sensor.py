"""HomeWizard P1 sensor via the open, unauthenticated /api/v1/data endpoint.

Fallback for firmware where the official homewizard integration's pairing
endpoint (/api/user) does not exist (404) even with Local API enabled and the
device button pressed - see agent-comms/knowledge/904 2026-08-24 for the
investigation. Not a replacement for the official integration where pairing
works; only use this where it does not.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import async_timeout
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_SCAN_INTERVAL, PERCENTAGE
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DEFAULT_SCAN_INTERVAL_S, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default="P1 Meter"): cv.string,
        vol.Optional(
            CONF_SCAN_INTERVAL, default=timedelta(seconds=DEFAULT_SCAN_INTERVAL_S)
        ): cv.time_period,
    }
)

# (key in /api/v1/data, suffix for entity name, unit, device_class, state_class)
SENSOR_DESCRIPTIONS = [
    ("active_power_w", "Effekt", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
    (
        "active_power_l1_w",
        "Effekt Fas 1",
        "W",
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_power_l2_w",
        "Effekt Fas 2",
        "W",
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_power_l3_w",
        "Effekt Fas 3",
        "W",
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "total_power_import_kwh",
        "Total Elimport",
        "kWh",
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
    ),
    (
        "total_power_export_kwh",
        "Total Elexport",
        "kWh",
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
    ),
    (
        "active_voltage_l1_v",
        "Spänning Fas 1",
        "V",
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_voltage_l2_v",
        "Spänning Fas 2",
        "V",
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_voltage_l3_v",
        "Spänning Fas 3",
        "V",
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_current_l1_a",
        "Ström Fas 1",
        "A",
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_current_l2_a",
        "Ström Fas 2",
        "A",
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
    ),
    (
        "active_current_l3_a",
        "Ström Fas 3",
        "A",
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
    ),
    ("wifi_strength", "WiFi Signal", PERCENTAGE, None, SensorStateClass.MEASUREMENT),
]


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the HomeWizard local P1 sensors."""
    host = config[CONF_HOST]
    name = config[CONF_NAME]
    scan_interval = config[CONF_SCAN_INTERVAL]
    session = async_get_clientsession(hass)

    # One-time device info fetch (product_type/serial/firmware) for the device registry.
    device_info_raw: dict = {}
    try:
        async with async_timeout.timeout(10):
            resp = await session.get(f"http://{host}/api")
            device_info_raw = await resp.json()
    except Exception as err:
        _LOGGER.warning("Could not fetch device info from %s: %s", host, err)

    # unique_id is host-based (known at config time, deterministic across every
    # boot) - never serial, which comes from a one-shot /api fetch that can fail
    # on a given boot and would otherwise churn entity_ids (PLAT-1975 QC, 901).
    # serial is still used in DeviceInfo for display/grouping only.
    serial = device_info_raw.get("serial", host)
    device_info = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer="HomeWizard",
        model=device_info_raw.get("product_type", "HWE-P1"),
        name=name,
        sw_version=device_info_raw.get("firmware_version"),
        configuration_url=f"http://{host}",
    )

    async def _async_update_data() -> dict:
        try:
            async with async_timeout.timeout(5):
                resp = await session.get(f"http://{host}/api/v1/data")
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status} from {host}")
                return await resp.json()
        except Exception as err:
            raise UpdateFailed(f"Error polling {host}: {err}") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{serial}",
        update_method=_async_update_data,
        update_interval=scan_interval,
    )
    # async_config_entry_first_refresh() requires a real config entry and raises
    # ConfigEntryError otherwise - this is a legacy YAML platform, not a config
    # entry, so use the plain refresh + PlatformNotReady instead.
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise PlatformNotReady(f"Could not reach P1 meter at {host}")

    entities = [
        HomeWizardLocalSensor(
            coordinator, host, device_info, key, suffix, unit, device_class, state_class
        )
        for key, suffix, unit, device_class, state_class in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class HomeWizardLocalSensor(CoordinatorEntity, SensorEntity):
    """A single value from the HomeWizard local /api/v1/data response."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        unique_id_base: str,
        device_info: DeviceInfo,
        key: str,
        suffix: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = f"P1 Meter {suffix}"
        self._attr_unique_id = f"{DOMAIN}_{unique_id_base}_{key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)
