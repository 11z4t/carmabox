"""PLAT-1198: Tests for AuditEntry and AuditLog."""

from datetime import datetime

import pytest

from custom_components.carmabox.core.audit import AuditEntry, AuditLog


def _entry(
    command: str = "set_ems_mode",
    target: str = "kontor",
    value: str = "discharge_pv",
    reason: str = "peak shaving",
    safety_result: bool | None = True,
    plan_hour: int | None = 14,
    source: str = "executor",
) -> AuditEntry:
    return AuditEntry(
        timestamp=datetime(2026, 4, 2, 14, 0, 0),
        command=command,
        target=target,
        value=value,
        reason=reason,
        safety_result=safety_result,
        plan_hour=plan_hour,
        source=source,
    )


class TestAuditEntry:
    def test_frozen_immutable(self) -> None:
        """AuditEntry must be immutable (frozen=True)."""
        e = _entry()
        with pytest.raises((AttributeError, TypeError)):
            e.command = "other"  # type: ignore[misc]

    def test_all_eight_fields_present(self) -> None:
        """All 8 required fields exist."""
        e = _entry()
        assert e.timestamp == datetime(2026, 4, 2, 14, 0, 0)
        assert e.command == "set_ems_mode"
        assert e.target == "kontor"
        assert e.value == "discharge_pv"
        assert e.reason == "peak shaving"
        assert e.safety_result is True
        assert e.plan_hour == 14
        assert e.source == "executor"

    def test_to_dict_keys(self) -> None:
        """to_dict() returns all 8 keys."""
        d = _entry().to_dict()
        expected = {"timestamp", "command", "target", "value", "reason", "safety_result", "plan_hour", "source"}
        assert set(d.keys()) == expected

    def test_to_dict_timestamp_iso(self) -> None:
        """timestamp in to_dict() is ISO-formatted string."""
        d = _entry().to_dict()
        assert isinstance(d["timestamp"], str)
        assert "2026-04-02" in d["timestamp"]

    def test_safety_result_none_allowed(self) -> None:
        """safety_result=None is valid (skipped check)."""
        e = _entry(safety_result=None)
        assert e.safety_result is None
        assert e.to_dict()["safety_result"] is None

    def test_plan_hour_none_allowed(self) -> None:
        """plan_hour=None is valid for ad-hoc commands."""
        e = _entry(plan_hour=None)
        assert e.plan_hour is None

    def test_value_stored_as_string(self) -> None:
        """value field stores as string — works for numeric values too."""
        e = _entry(value="1500")
        assert e.value == "1500"


class TestAuditLog:
    def test_empty_on_init(self) -> None:
        log = AuditLog()
        assert len(log) == 0
        assert log.recent() == []

    def test_add_and_len(self) -> None:
        log = AuditLog()
        log.add(_entry())
        assert len(log) == 1

    def test_recent_newest_first(self) -> None:
        """recent() returns newest entries first."""
        log = AuditLog()
        e1 = _entry(value="charge_pv")
        e2 = _entry(value="discharge_pv")
        log.add(e1)
        log.add(e2)
        result = log.recent()
        assert result[0].value == "discharge_pv"
        assert result[1].value == "charge_pv"

    def test_ring_buffer_max_len(self) -> None:
        """Buffer never exceeds maxlen."""
        log = AuditLog(maxlen=5)
        for i in range(10):
            log.add(_entry(value=str(i)))
        assert len(log) == 5
        # Most recent 5 should be 5..9
        values = [e.value for e in log.recent()]
        assert "9" in values
        assert "0" not in values

    def test_to_dicts_returns_list_of_dicts(self) -> None:
        log = AuditLog()
        log.add(_entry())
        result = log.to_dicts()
        assert isinstance(result, list)
        assert isinstance(result[0], dict)
        assert "command" in result[0]

    def test_to_dicts_respects_n(self) -> None:
        """to_dicts(n) limits result count."""
        log = AuditLog()
        for _ in range(20):
            log.add(_entry())
        assert len(log.to_dicts(5)) == 5

    def test_default_maxlen_200(self) -> None:
        """Default AuditLog holds 200 entries."""
        log = AuditLog()
        for _ in range(250):
            log.add(_entry())
        assert len(log) == 200
