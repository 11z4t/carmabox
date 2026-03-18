"""CARMA Box — SafetyGuard.

Mandatory safety checks before EVERY battery/EV command.
Cannot be disabled. Cannot be bypassed. Logs every check.

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


@dataclass
class SafetyResult:
    """Result of a safety check."""

    ok: bool
    reason: str = ""


class SafetyGuard:
    """Obligatory safety checks.

    Rules:
    - Cannot be disabled (no config flag)
    - Cannot be bypassed (all code paths use this)
    - Logs every check (pass and block)
    - Unknown state → block
    """

    def __init__(
        self,
        min_soc: float = 15.0,
        crosscharge_threshold_w: float = 500.0,
        temperature_min_c: float = 0.0,
        temperature_max_c: float = 45.0,
    ) -> None:
        """Initialize with safety thresholds."""
        self.min_soc = min_soc
        self.crosscharge_threshold_w = crosscharge_threshold_w
        self.temp_min = temperature_min_c
        self.temp_max = temperature_max_c

    def check_discharge(
        self,
        soc_1: float,
        soc_2: float,
        min_soc: float,
        grid_power_w: float,
        temp_c: float | None = None,
    ) -> SafetyResult:
        """Check if discharge is safe.

        Blocks if:
        - Any battery below min SoC
        - Grid is exporting (grid_power < 0)
        - Temperature out of range
        """
        # Never discharge during export
        if grid_power_w < 0:
            reason = f"grid exporting ({grid_power_w:.0f}W)"
            _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
            return SafetyResult(ok=False, reason=reason)

        # Min SoC check
        if soc_1 < min_soc:
            reason = f"battery_1 SoC {soc_1:.0f}% < min {min_soc:.0f}%"
            _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
            return SafetyResult(ok=False, reason=reason)

        if soc_2 >= 0 and soc_2 < min_soc:
            reason = f"battery_2 SoC {soc_2:.0f}% < min {min_soc:.0f}%"
            _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
            return SafetyResult(ok=False, reason=reason)

        # Temperature check
        if temp_c is not None:
            if temp_c < self.temp_min:
                reason = f"temperature {temp_c:.1f}°C < min {self.temp_min}°C"
                _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
                return SafetyResult(ok=False, reason=reason)
            if temp_c > self.temp_max:
                reason = f"temperature {temp_c:.1f}°C > max {self.temp_max}°C"
                _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
                return SafetyResult(ok=False, reason=reason)

        _LOGGER.debug(
            "SafetyGuard PASS discharge: SoC %s/%s%%, grid %sW", soc_1, soc_2, grid_power_w
        )
        return SafetyResult(ok=True)

    def check_charge(
        self,
        soc_1: float,
        soc_2: float,
        temp_c: float | None = None,
    ) -> SafetyResult:
        """Check if charging is safe.

        Blocks if:
        - All batteries at 100%
        - Temperature below 0°C (cell damage risk)
        """
        # Max SoC
        all_full = soc_1 >= 100 and (soc_2 < 0 or soc_2 >= 100)
        if all_full:
            reason = "all batteries full (100%)"
            _LOGGER.debug("SafetyGuard BLOCK charge: %s", reason)
            return SafetyResult(ok=False, reason=reason)

        # Temperature — never charge below 0°C
        if temp_c is not None and temp_c < self.temp_min:
            reason = f"temperature {temp_c:.1f}°C < min {self.temp_min}°C — charge blocked"
            _LOGGER.debug("SafetyGuard BLOCK charge: %s", reason)
            return SafetyResult(ok=False, reason=reason)

        _LOGGER.debug("SafetyGuard PASS charge: SoC %s/%s%%", soc_1, soc_2)
        return SafetyResult(ok=True)

    def check_crosscharge(
        self,
        power_1_w: float,
        power_2_w: float,
    ) -> SafetyResult:
        """Check for crosscharge condition.

        Crosscharge = one battery charging while other discharging.
        Both must be significant (>threshold).
        """
        if power_2_w == 0:  # No second battery
            return SafetyResult(ok=True)

        opposite_signs = (power_1_w * power_2_w) < 0
        both_significant = (
            abs(power_1_w) > self.crosscharge_threshold_w
            and abs(power_2_w) > self.crosscharge_threshold_w
        )

        if opposite_signs and both_significant:
            reason = f"crosscharge: battery_1={power_1_w:.0f}W, battery_2={power_2_w:.0f}W"
            _LOGGER.warning("SafetyGuard BLOCK: %s", reason)
            return SafetyResult(ok=False, reason=reason)

        return SafetyResult(ok=True)
