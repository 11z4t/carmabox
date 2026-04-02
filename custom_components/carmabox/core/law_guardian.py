"""Law Guardian — monitors all laws, records breaches, triggers RCA.

Pure Python. No HA imports. Fully testable.

Runs every cycle (30s). Checks all 7 laws + 5 invariants.
Records breaches with context for root cause analysis.
Triggers replanning when laws are violated.

Key principles:
  - NEVER stop — degrade gracefully
  - Every breach documented with full context
  - Automatic root cause classification
  - Learns which actions prevent breaches
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from custom_components.carmabox.const import (
    LAG1_CRITICAL_BREACH_THRESHOLD,
    LAG2_IDLE_HOURS_THRESHOLD,
    LAG2_SOC_HYSTERESIS_PCT,
    LAW_GUARDIAN_BATTERY_IDLE_W,
    LAW_GUARDIAN_MAX_BREACH_HISTORY,
    LAW_GUARDIAN_TAK_MARGIN_FACTOR,
)

# Noise-floor thresholds
_EXPORT_WARNING_W = 500  # W — LAG_4 export guard threshold
_CROSSCHARGE_NOISE_W = 50  # W — minimum |power| to detect crosscharge or idle


class LawId(Enum):
    LAG_1_GRID = "LAG_1"
    LAG_2_IDLE = "LAG_2"
    LAG_3_EV = "LAG_3"
    LAG_4_EXPORT = "LAG_4"
    LAG_5_CHARGE_PRICE = "LAG_5"
    LAG_6_DISCHARGE = "LAG_6"
    LAG_7_SOLAR = "LAG_7"
    INV_1_EMS_AUTO = "INV_1"
    INV_2_CROSSCHARGE = "INV_2"
    INV_3_FAST_CHARGE = "INV_3"
    INV_4_COLD_CHARGE = "INV_4"
    INV_5_MIN_SOC = "INV_5"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class BreachRecord:
    """Documented law violation."""

    timestamp: str
    law: LawId
    severity: Severity
    actual_value: float
    limit_value: float
    duration_s: int = 0
    root_cause: str = ""
    correction: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    _mono: float = field(default_factory=time.monotonic, repr=False)


@dataclass
class GuardianState:
    """Current state for law evaluation."""

    grid_import_w: float
    grid_viktat_timmedel_kw: float
    ellevio_tak_kw: float
    battery_soc_1: float
    battery_soc_2: float
    battery_power_1: float
    battery_power_2: float
    battery_idle_hours: float
    ev_soc: float
    ev_target_soc: float
    ev_departure_hour: int
    current_hour: int
    current_price: float
    pv_power_w: float
    export_w: float
    ems_mode_1: str
    ems_mode_2: str
    fast_charging_1: bool
    fast_charging_2: bool
    cell_temp_1: float
    cell_temp_2: float
    min_soc: float
    cold_lock_temp: float


@dataclass
class CheckResult:
    """Result of one law check."""

    law: LawId
    ok: bool
    severity: Severity = Severity.INFO
    actual: float = 0
    limit: float = 0
    message: str = ""


@dataclass
class GuardianReport:
    """Complete guardian evaluation."""

    checks: list[CheckResult]
    breaches: list[BreachRecord]
    replan_needed: bool
    notifications: list[dict[str, Any]]  # {channel, severity, message}


class LawGuardian:
    """Monitors all laws and invariants every cycle."""

    def __init__(self, max_breach_history: int = LAW_GUARDIAN_MAX_BREACH_HISTORY) -> None:
        self.breach_history: list[BreachRecord] = []
        self._max_history = max_breach_history
        self._breach_count_hour: dict[str, int] = {}
        self._last_hour: int = -1
        self._idle_start: float = 0
        self._consecutive_idle: int = 0

    def evaluate(self, state: GuardianState) -> GuardianReport:
        """Evaluate all laws. Returns report with breaches and notifications."""
        checks: list[CheckResult] = []
        breaches: list[BreachRecord] = []
        notifications: list[dict[str, Any]] = []
        replan = False
        now_str = datetime.now().isoformat()

        # Reset hourly counters
        if state.current_hour != self._last_hour:
            self._breach_count_hour = {}
            self._last_hour = state.current_hour

        # ── LAG 1: Ellevio timmedel ─────────────────────────────
        c1 = self._check_lag1(state)
        checks.append(c1)
        if not c1.ok:
            br = BreachRecord(
                timestamp=now_str,
                law=LawId.LAG_1_GRID,
                severity=c1.severity,
                actual_value=c1.actual,
                limit_value=c1.limit,
                root_cause=self._classify_lag1_cause(state),
                correction="Grid Guard agerar",
                context={"grid_w": state.grid_import_w, "price": state.current_price},
            )
            breaches.append(br)
            replan = True
            self._count_breach("LAG_1")
            if self._breach_count_hour.get("LAG_1", 0) >= LAG1_CRITICAL_BREACH_THRESHOLD:
                notifications.append(
                    {
                        "channel": "slack",
                        "severity": "critical",
                        "message": f"LAG 1 brott x{self._breach_count_hour['LAG_1']}/h: "
                        f"viktat {c1.actual:.1f} kW > tak {c1.limit:.1f} kW",
                    }
                )

        # ── LAG 2: Batterier idle ───────────────────────────────
        c2 = self._check_lag2(state)
        checks.append(c2)
        if not c2.ok:
            breaches.append(
                BreachRecord(
                    timestamp=now_str,
                    law=LawId.LAG_2_IDLE,
                    severity=Severity.WARNING,
                    actual_value=c2.actual,
                    limit_value=c2.limit,
                    root_cause="Batterier idle > 4h",
                    context={
                        "soc_1": state.battery_soc_1,
                        "soc_2": state.battery_soc_2,
                    },
                )
            )

        # ── LAG 3: EV target ───────────────────────────────────
        c3 = self._check_lag3(state)
        checks.append(c3)
        if not c3.ok:
            breaches.append(
                BreachRecord(
                    timestamp=now_str,
                    law=LawId.LAG_3_EV,
                    severity=Severity.CRITICAL,
                    actual_value=c3.actual,
                    limit_value=c3.limit,
                    root_cause="EV under target vid avresetid",
                )
            )
            notifications.append(
                {
                    "channel": "slack",
                    "severity": "critical",
                    "message": (
                        f"LAG 3: EV {c3.actual:.0f}% < target"
                        f" {c3.limit:.0f}% kl {state.ev_departure_hour}"
                    ),
                }
            )

        # ── LAG 4: Export ───────────────────────────────────────
        c4 = self._check_lag4(state)
        checks.append(c4)
        if not c4.ok:
            breaches.append(
                BreachRecord(
                    timestamp=now_str,
                    law=LawId.LAG_4_EXPORT,
                    severity=Severity.WARNING,
                    actual_value=c4.actual,
                    limit_value=0,
                    root_cause="Export > 500W med styrbara förbrukare tillgängliga",
                )
            )

        # ── INV-1 till INV-5 ───────────────────────────────────
        for inv_check in self._check_invariants(state):
            checks.append(inv_check)
            if not inv_check.ok:
                breaches.append(
                    BreachRecord(
                        timestamp=now_str,
                        law=inv_check.law,
                        severity=Severity.CRITICAL,
                        actual_value=inv_check.actual,
                        limit_value=inv_check.limit,
                        root_cause=inv_check.message,
                    )
                )
                replan = True

        # Store breaches
        self.breach_history.extend(breaches)
        if len(self.breach_history) > self._max_history:
            self.breach_history = self.breach_history[-self._max_history :]

        return GuardianReport(
            checks=checks,
            breaches=breaches,
            replan_needed=replan,
            notifications=notifications,
        )

    # ── Slack notification check ─────────────────────────────────

    def should_notify_slack(
        self,
        law: str = "LAG_1",
        threshold_count: int = LAG1_CRITICAL_BREACH_THRESHOLD,
        window_minutes: int = 60,
    ) -> tuple[bool, str]:
        """Return (True, message) if breach count in window exceeds threshold."""
        now = time.monotonic()
        cutoff = now - window_minutes * 60
        recent = [b for b in self.breach_history if b.law.value == law and b._mono >= cutoff]
        count = len(recent)
        if count < threshold_count:
            return False, ""

        worst = max(b.actual_value for b in recent)
        msg = f"{law} brott x{count} senaste {window_minutes}min (worst {worst:.1f} kW)"
        return True, msg

    # ── Hourly/daily reports ────────────────────────────────────

    def hourly_summary(self) -> dict[str, Any]:
        """Summary for the last hour."""
        recent = [b for b in self.breach_history if b.timestamp > datetime.now().isoformat()[:13]]
        by_law: dict[str, int] = {}
        for b in recent:
            key = b.law.value
            by_law[key] = by_law.get(key, 0) + 1
        return {
            "breach_count": len(recent),
            "by_law": by_law,
            "worst": max((b.actual_value for b in recent), default=0),
        }

    def daily_summary(self) -> dict[str, Any]:
        """Summary for today."""
        today = datetime.now().strftime("%Y-%m-%d")
        today_breaches = [b for b in self.breach_history if b.timestamp.startswith(today)]
        by_law: dict[str, int] = {}
        for b in today_breaches:
            key = b.law.value
            by_law[key] = by_law.get(key, 0) + 1

        lag1 = [b for b in today_breaches if b.law == LawId.LAG_1_GRID]
        return {
            "date": today,
            "total_breaches": len(today_breaches),
            "by_law": by_law,
            "lag1_max_kw": max((b.actual_value for b in lag1), default=0),
            "lag1_count": len(lag1),
        }

    # ── Law checks ──────────────────────────────────────────────

    def _check_lag1(self, state: GuardianState) -> CheckResult:
        viktat = state.grid_viktat_timmedel_kw
        tak = state.ellevio_tak_kw
        margin = tak * LAW_GUARDIAN_TAK_MARGIN_FACTOR
        if viktat > tak:
            return CheckResult(
                LawId.LAG_1_GRID,
                False,
                Severity.CRITICAL,
                viktat,
                tak,
                f"Viktat {viktat:.1f} > tak {tak:.1f}",
            )
        if viktat > margin:
            return CheckResult(
                LawId.LAG_1_GRID,
                True,
                Severity.WARNING,
                viktat,
                tak,
                f"Viktat {viktat:.1f} nära tak {tak:.1f}",
            )
        return CheckResult(LawId.LAG_1_GRID, True, Severity.INFO, viktat, tak)

    def _check_lag2(self, state: GuardianState) -> CheckResult:
        both_idle = (
            abs(state.battery_power_1) < LAW_GUARDIAN_BATTERY_IDLE_W
            and abs(state.battery_power_2) < LAW_GUARDIAN_BATTERY_IDLE_W
        )
        has_capacity = (
            state.battery_soc_1 > state.min_soc + LAG2_SOC_HYSTERESIS_PCT
            or state.battery_soc_2 > state.min_soc + LAG2_SOC_HYSTERESIS_PCT
        )

        if both_idle and has_capacity:
            self._consecutive_idle += 1
        else:
            self._consecutive_idle = 0

        idle_hours = self._consecutive_idle * 30 / 3600  # 30s cycles
        if idle_hours > LAG2_IDLE_HOURS_THRESHOLD:
            return CheckResult(
                LawId.LAG_2_IDLE,
                False,
                Severity.WARNING,
                idle_hours,
                LAG2_IDLE_HOURS_THRESHOLD,
                f"Idle {idle_hours:.1f}h > {LAG2_IDLE_HOURS_THRESHOLD}h",
            )
        return CheckResult(
            LawId.LAG_2_IDLE, True, Severity.INFO, idle_hours, LAG2_IDLE_HOURS_THRESHOLD
        )

    def _check_lag3(self, state: GuardianState) -> CheckResult:
        if state.ev_soc < 0:
            return CheckResult(LawId.LAG_3_EV, True, Severity.INFO, -1, state.ev_target_soc)
        if state.current_hour == state.ev_departure_hour and state.ev_soc < state.ev_target_soc:
            return CheckResult(
                LawId.LAG_3_EV,
                False,
                Severity.CRITICAL,
                state.ev_soc,
                state.ev_target_soc,
                f"EV {state.ev_soc:.0f}% < target {state.ev_target_soc:.0f}%",
            )
        return CheckResult(LawId.LAG_3_EV, True, Severity.INFO, state.ev_soc, state.ev_target_soc)

    def _check_lag4(self, state: GuardianState) -> CheckResult:
        if state.export_w > _EXPORT_WARNING_W:
            return CheckResult(
                LawId.LAG_4_EXPORT,
                False,
                Severity.WARNING,
                state.export_w,
                0,
                f"Export {state.export_w:.0f}W",
            )
        return CheckResult(LawId.LAG_4_EXPORT, True, Severity.INFO, state.export_w, 0)

    def _check_invariants(self, state: GuardianState) -> list[CheckResult]:
        results = []
        # INV-1: EMS auto
        for i, mode in enumerate([state.ems_mode_1, state.ems_mode_2], 1):
            if mode == "auto":
                results.append(
                    CheckResult(
                        LawId.INV_1_EMS_AUTO,
                        False,
                        Severity.CRITICAL,
                        0,
                        0,
                        f"Batteri {i} EMS=auto",
                    )
                )
            else:
                results.append(CheckResult(LawId.INV_1_EMS_AUTO, True))

        # INV-2: Crosscharge
        _n = _CROSSCHARGE_NOISE_W
        if state.battery_power_1 < -_n and state.battery_power_2 > _n:
            results.append(
                CheckResult(
                    LawId.INV_2_CROSSCHARGE,
                    False,
                    Severity.CRITICAL,
                    0,
                    0,
                    "Bat1 laddar, Bat2 urladdar",
                )
            )
        elif state.battery_power_1 > _n and state.battery_power_2 < -_n:
            results.append(
                CheckResult(
                    LawId.INV_2_CROSSCHARGE,
                    False,
                    Severity.CRITICAL,
                    0,
                    0,
                    "Bat1 urladdar, Bat2 laddar",
                )
            )
        else:
            results.append(CheckResult(LawId.INV_2_CROSSCHARGE, True))

        # INV-3: fast_charging
        for i, fc in enumerate([state.fast_charging_1, state.fast_charging_2], 1):
            if fc:
                results.append(
                    CheckResult(
                        LawId.INV_3_FAST_CHARGE,
                        False,
                        Severity.CRITICAL,
                        0,
                        0,
                        f"Batteri {i} fast_charging=ON",
                    )
                )
            else:
                results.append(CheckResult(LawId.INV_3_FAST_CHARGE, True))

        # INV-5: Min SoC
        for i, (soc, power, temp) in enumerate(
            [
                (state.battery_soc_1, state.battery_power_1, state.cell_temp_1),
                (state.battery_soc_2, state.battery_power_2, state.cell_temp_2),
            ],
            1,
        ):
            eff_min = 20.0 if temp < state.cold_lock_temp else state.min_soc
            if soc <= eff_min and power > LAW_GUARDIAN_BATTERY_IDLE_W:
                results.append(
                    CheckResult(
                        LawId.INV_5_MIN_SOC,
                        False,
                        Severity.CRITICAL,
                        soc,
                        eff_min,
                        f"Bat{i} SoC {soc:.0f}% ≤ min {eff_min:.0f}% och urladdar",
                    )
                )
            else:
                results.append(CheckResult(LawId.INV_5_MIN_SOC, True))

        return results

    # ── Root cause classification ───────────────────────────────

    def _classify_lag1_cause(self, state: GuardianState) -> str:
        if state.fast_charging_1 or state.fast_charging_2:
            return "fast_charging ON → nätimport"
        if state.ems_mode_1 == "auto" or state.ems_mode_2 == "auto":
            return "EMS auto → okontrollerad"
        _noise = _CROSSCHARGE_NOISE_W
        if abs(state.battery_power_1) < _noise and abs(state.battery_power_2) < _noise:
            return "Batterier idle → inget stöd"
        if state.battery_power_1 < -100 or state.battery_power_2 < -100:
            return "Batterier LADDAR → ökad import"
        return "Hög huslast utan tillräckligt batteristöd"

    def _count_breach(self, law_key: str) -> None:
        self._breach_count_hour[law_key] = self._breach_count_hour.get(law_key, 0) + 1
