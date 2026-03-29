"""CARMA Box — SafetyGuard.

Mandatory safety checks before EVERY battery/EV command.
Cannot be disabled. Cannot be bypassed. Logs every check.

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


MAX_SAFETY_LOG_ENTRIES = 50


@dataclass
class SafetyLogEntry:
    """Single safety check log entry."""

    timestamp: float
    check: str
    ok: bool
    reason: str = ""


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
        max_mode_changes_per_hour: int = 60,
        temperature_min_charge_c: float = 2.0,
        temperature_min_discharge_c: float = 0.0,
    ) -> None:
        """Initialize with safety thresholds."""
        self.min_soc = min_soc
        self.crosscharge_threshold_w = crosscharge_threshold_w
        self.temp_min = temperature_min_c  # Legacy fallback
        self.temp_min_charge = temperature_min_charge_c  # PLAT-1019
        self.temp_min_discharge = temperature_min_discharge_c  # PLAT-1019
        self.temp_max = temperature_max_c
        self.max_mode_changes = max_mode_changes_per_hour

        # #7 Rate guard — track mode changes (S3: bounded deque)
        self._mode_change_timestamps: deque[float] = deque(maxlen=max_mode_changes_per_hour * 2)

        # Rate limit cooldown — when triggered, pause ALL commands for 5 min
        self._rate_limit_cooldown_until: float | None = None

        # #8 Heartbeat — track last successful update
        self._last_heartbeat: float = time.monotonic()

        # Safety log — ring buffer of recent checks (blocks + passes)
        self._safety_log: deque[SafetyLogEntry] = deque(maxlen=MAX_SAFETY_LOG_ENTRIES)

    def _log(self, check: str, result: SafetyResult) -> None:
        """Record a safety check result."""
        self._safety_log.append(
            SafetyLogEntry(
                timestamp=time.time(),
                check=check,
                ok=result.ok,
                reason=result.reason,
            )
        )

    def get_safety_log(self) -> list[dict[str, object]]:
        """Return recent safety log entries as dicts (for diagnostics)."""
        return [
            {
                "timestamp": e.timestamp,
                "check": e.check,
                "ok": e.ok,
                "reason": e.reason,
            }
            for e in self._safety_log
        ]

    def recent_block_count(self, seconds: float = 3600.0) -> int:
        """Count blocks in the last N seconds."""
        cutoff = time.time() - seconds
        return sum(1 for e in self._safety_log if not e.ok and e.timestamp > cutoff)

    def check_discharge(
        self,
        soc_1: float,
        soc_2: float,
        min_soc: float,
        grid_power_w: float,
        temp_c: float | None = None,
        reserve_kwh: float = 0.0,
        available_kwh: float = 999.0,
    ) -> SafetyResult:
        """Check if discharge is safe.

        Blocks if:
        - Any battery below min SoC
        - Grid is exporting (grid_power < 0)
        - Temperature out of range
        - IT-2075: Available energy below reserve
        """
        # IT-2075: Reserve-aware gating
        if reserve_kwh > 0 and available_kwh < reserve_kwh + 1.0:
            reason = f"available {available_kwh:.1f} kWh < reserve {reserve_kwh:.1f} + 1.0"
            _LOGGER.info("SafetyGuard BLOCK discharge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("discharge", r)
            return r
        # Never discharge during export
        if grid_power_w < 0:
            reason = f"grid exporting ({grid_power_w:.0f}W)"
            _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("discharge", r)
            return r

        # Min SoC check
        if soc_1 < min_soc:
            reason = f"battery_1 SoC {soc_1:.0f}% < min {min_soc:.0f}%"
            _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("discharge", r)
            return r

        if soc_2 > 0 and soc_2 < min_soc:
            # soc_2==0 likely means unavailable — don't block on that
            reason = f"battery_2 SoC {soc_2:.0f}% < min {min_soc:.0f}%"
            _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("discharge", r)
            return r

        # Temperature check (PLAT-1019: use discharge-specific threshold)
        if temp_c is not None:
            if temp_c < self.temp_min_discharge:
                reason = f"temperature {temp_c:.1f}°C < min {self.temp_min_discharge}°C"
                _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
                r = SafetyResult(ok=False, reason=reason)
                self._log("discharge", r)
                return r
            if temp_c > self.temp_max:
                reason = f"temperature {temp_c:.1f}°C > max {self.temp_max}°C"
                _LOGGER.debug("SafetyGuard BLOCK discharge: %s", reason)
                r = SafetyResult(ok=False, reason=reason)
                self._log("discharge", r)
                return r

        _LOGGER.debug(
            "SafetyGuard PASS discharge: SoC %s/%s%%, grid %sW", soc_1, soc_2, grid_power_w
        )
        r = SafetyResult(ok=True)
        self._log("discharge", r)
        return r

    def check_charge(
        self,
        soc_1: float,
        soc_2: float,
        temp_c: float | None = None,
    ) -> SafetyResult:
        """Check if charging is safe.

        Blocks if:
        - All batteries at 100%
        - Temperature below 2°C (PLAT-1019: LFP cell damage risk)
        """
        # Max SoC
        all_full = soc_1 >= 100 and (soc_2 < 0 or soc_2 >= 100)
        if all_full:
            reason = "all batteries full (100%)"
            _LOGGER.debug("SafetyGuard BLOCK charge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("charge", r)
            return r

        # Temperature — PLAT-1019: never charge LFP below 2°C
        if temp_c is not None and temp_c < self.temp_min_charge:
            reason = f"temperature {temp_c:.1f}°C < min {self.temp_min_charge}°C — charge blocked"
            _LOGGER.debug("SafetyGuard BLOCK charge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("charge", r)
            return r

        _LOGGER.debug("SafetyGuard PASS charge: SoC %s/%s%%", soc_1, soc_2)
        r = SafetyResult(ok=True)
        self._log("charge", r)
        return r

    def check_crosscharge(
        self,
        power_1_w: float,
        power_2_w: float,
        power_1_valid: bool = True,
        power_2_valid: bool = True,
    ) -> SafetyResult:
        """Check for crosscharge condition.

        Crosscharge = one battery charging while other discharging.
        Both must be significant (>threshold).

        power_X_valid=False means the sensor was unknown/unavailable (e.g. HA start).
        When readings are unreliable, we block to be safe (PLAT-946).
        """
        # No second battery (soc_2 == -1 → caller passes power_2=0, valid=True)
        # Single-battery setups never crosscharge
        if power_2_w == 0 and power_2_valid:
            r = SafetyResult(ok=True)
            self._log("crosscharge", r)
            return r

        # PLAT-946: If battery_2 unavailable but battery_1 is fine → single-battery mode (OK)
        if not power_2_valid and power_1_valid:
            r = SafetyResult(ok=True)
            self._log("crosscharge", r)
            return r

        # Block only if battery_1 is unreliable (we can't make safe decisions)
        if not power_1_valid:
            reason = (
                f"unreliable power readings: "
                f"battery_1={power_1_w}W (valid={power_1_valid}), "
                f"battery_2={power_2_w}W (valid={power_2_valid}) "
                f"— blocking until sensors available"
            )
            _LOGGER.warning("SafetyGuard BLOCK crosscharge: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("crosscharge", r)
            return r

        opposite_signs = (power_1_w * power_2_w) < 0
        both_significant = (
            abs(power_1_w) > self.crosscharge_threshold_w
            and abs(power_2_w) > self.crosscharge_threshold_w
        )

        if opposite_signs and both_significant:
            reason = f"crosscharge: battery_1={power_1_w:.0f}W, battery_2={power_2_w:.0f}W"
            _LOGGER.warning("SafetyGuard BLOCK: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("crosscharge", r)
            return r

        r = SafetyResult(ok=True)
        self._log("crosscharge", r)
        return r

    def check_rate_limit(self) -> SafetyResult:
        """#7 Rate guard — block if too many mode changes per hour.

        Prevents oscillation and Modbus flooding.
        When triggered, enters 5-minute cooldown to break oscillation cycle.
        """
        now = time.monotonic()

        # Check if in cooldown period
        if self._rate_limit_cooldown_until is not None:
            if now < self._rate_limit_cooldown_until:
                remaining = int(self._rate_limit_cooldown_until - now)
                reason = f"rate limit cooldown: {remaining}s remaining"
                _LOGGER.debug("SafetyGuard BLOCK: %s", reason)
                r = SafetyResult(ok=False, reason=reason)
                # Don't log during cooldown to avoid spam
                return r
            # Cooldown expired — clear it
            _LOGGER.info("SafetyGuard: rate limit cooldown expired, resuming normal operation")
            self._rate_limit_cooldown_until = None

        cutoff = now - 3600  # 1 hour window

        # Count recent timestamps (deque is already bounded)
        recent_count = sum(1 for t in self._mode_change_timestamps if t > cutoff)

        if recent_count >= self.max_mode_changes:
            # Trigger 5-minute cooldown
            self._rate_limit_cooldown_until = now + 300  # 5 minutes
            reason = (
                f"rate limit: {recent_count} changes in 1h (max {self.max_mode_changes}) "
                f"— entering 5min cooldown"
            )
            _LOGGER.warning("SafetyGuard BLOCK: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("rate_limit", r)
            return r

        r = SafetyResult(ok=True)
        self._log("rate_limit", r)
        return r

    def record_mode_change(self) -> None:
        """Record a mode change for rate limiting."""
        self._mode_change_timestamps.append(time.monotonic())

    def check_heartbeat(self, max_stale_seconds: float = 120.0) -> SafetyResult:
        """#8 Heartbeat — block if coordinator hasn't updated recently.

        If the coordinator stops updating (crash, freeze, Modbus lockup),
        all commands should be blocked to prevent stale-state actions.
        """
        elapsed = time.monotonic() - self._last_heartbeat
        if elapsed > max_stale_seconds:
            reason = f"heartbeat stale: {elapsed:.0f}s since last update (max {max_stale_seconds}s)"
            _LOGGER.warning("SafetyGuard BLOCK: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("heartbeat", r)
            return r

        r = SafetyResult(ok=True)
        self._log("heartbeat", r)
        return r

    def update_heartbeat(self) -> None:
        """Update heartbeat timestamp. Called every successful update cycle."""
        self._last_heartbeat = time.monotonic()

    def check_write_verify(
        self,
        expected_mode: str,
        actual_mode: str,
    ) -> SafetyResult:
        """#9 Write-verify — confirm command was applied.

        After sending a mode change, verify the inverter actually
        changed. If not, Modbus lockup is likely.
        """
        if expected_mode and actual_mode and expected_mode != actual_mode:
            reason = (
                f"write-verify failed: expected '{expected_mode}', "
                f"actual '{actual_mode}' — possible Modbus lockup"
            )
            _LOGGER.warning("SafetyGuard BLOCK: %s", reason)
            r = SafetyResult(ok=False, reason=reason)
            self._log("write_verify", r)
            return r

        r = SafetyResult(ok=True)
        self._log("write_verify", r)
        return r
