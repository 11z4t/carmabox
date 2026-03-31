"""PLAT-1177: Tests for coordinator_commands.py — CommandsMixin + BatteryCommand import.

Verifies that:
1. BatteryCommand is importable from coordinator_commands (was NameError before fix)
2. CommandsMixin is a valid class that can be instantiated
3. BatteryCommand enum has all expected members
4. Module-level logger is configured
"""

from __future__ import annotations

from enum import Enum

from custom_components.carmabox.coordinator_commands import (
    _LOGGER,
    BatteryCommand,
    CommandsMixin,
)


class TestBatteryCommandImport:
    """PLAT-1177: BatteryCommand must be importable from coordinator_commands."""

    def test_battery_command_is_enum(self) -> None:
        assert issubclass(BatteryCommand, Enum)

    def test_battery_command_members(self) -> None:
        expected = {"IDLE", "CHARGE_PV", "CHARGE_PV_TAPER", "BMS_COLD_LOCK", "STANDBY", "DISCHARGE"}
        assert set(BatteryCommand.__members__.keys()) == expected

    def test_battery_command_values(self) -> None:
        assert BatteryCommand.IDLE.value == "idle"
        assert BatteryCommand.CHARGE_PV.value == "charge_pv"
        assert BatteryCommand.CHARGE_PV_TAPER.value == "charge_pv_taper"
        assert BatteryCommand.BMS_COLD_LOCK.value == "bms_cold_lock"
        assert BatteryCommand.STANDBY.value == "standby"
        assert BatteryCommand.DISCHARGE.value == "discharge"

    def test_battery_command_lookup_by_value(self) -> None:
        assert BatteryCommand("idle") is BatteryCommand.IDLE
        assert BatteryCommand("discharge") is BatteryCommand.DISCHARGE

    def test_battery_command_lookup_by_name(self) -> None:
        assert BatteryCommand["CHARGE_PV"] is BatteryCommand.CHARGE_PV


class TestCommandsMixin:
    """CommandsMixin class validation."""

    def test_mixin_is_class(self) -> None:
        assert isinstance(CommandsMixin, type)

    def test_mixin_instantiable(self) -> None:
        obj = CommandsMixin()
        assert obj is not None

    def test_mixin_docstring(self) -> None:
        assert CommandsMixin.__doc__ is not None
        assert "Battery" in CommandsMixin.__doc__


class TestModuleStructure:
    """Module-level attributes."""

    def test_logger_configured(self) -> None:
        assert _LOGGER.name == "custom_components.carmabox.coordinator_commands"

    def test_no_circular_import(self) -> None:
        """Importing coordinator_commands must not fail due to circular imports."""
        import importlib

        mod = importlib.import_module("custom_components.carmabox.coordinator_commands")
        assert hasattr(mod, "BatteryCommand")
        assert hasattr(mod, "CommandsMixin")
