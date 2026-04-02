"""CARMA Box — Audit trail for write commands.

PLAT-1198: Every hardware write (set_mode, set_charge, etc.) is logged
as an AuditEntry so incidents can be reconstructed forensically.

The AuditLog ring-buffer (max 200 entries) is exposed via diagnostics.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

_MAX_AUDIT_ENTRIES = 200


@dataclass(frozen=True)
class AuditEntry:
    """Immutable record of a single hardware write command.

    Fields
    ------
    timestamp:     When the command was issued (UTC-local datetime).
    command:       Name of the write operation (e.g. "set_ems_mode").
    target:        Device or entity that received the command (e.g. "kontor").
    value:         The value written (e.g. "discharge_pv", "1500").
    reason:        Why the command was issued (decision reason or watchdog ID).
    safety_result: True = safety check passed, False = blocked, None = skipped.
    plan_hour:     Plan hour this command belongs to, or None if ad-hoc.
    source:        Subsystem that issued the command (e.g. "executor", "watchdog").
    """

    timestamp: datetime
    command: str
    target: str
    value: str
    reason: str
    safety_result: bool | None
    plan_hour: int | None
    source: str

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict for diagnostics."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "command": self.command,
            "target": self.target,
            "value": self.value,
            "reason": self.reason,
            "safety_result": self.safety_result,
            "plan_hour": self.plan_hour,
            "source": self.source,
        }


class AuditLog:
    """Thread-safe ring-buffer for AuditEntry records.

    Keeps the last _MAX_AUDIT_ENTRIES entries. Older entries are
    automatically discarded when the buffer is full.
    """

    def __init__(self, maxlen: int = _MAX_AUDIT_ENTRIES) -> None:
        self._buf: deque[AuditEntry] = deque(maxlen=maxlen)

    def add(self, entry: AuditEntry) -> None:
        """Append an entry to the ring-buffer."""
        self._buf.append(entry)

    def recent(self, n: int = 50) -> list[AuditEntry]:
        """Return up to *n* most recent entries, newest first."""
        entries = list(self._buf)
        return list(reversed(entries[-n:]))

    def to_dicts(self, n: int = 50) -> list[dict[str, object]]:
        """Serialise the *n* most recent entries for diagnostics."""
        return [e.to_dict() for e in self.recent(n)]

    def __len__(self) -> int:
        return len(self._buf)
