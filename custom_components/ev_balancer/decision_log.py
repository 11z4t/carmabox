"""DecisionLog — JSONL writer with rotation and rate-limiting (spec §9a)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from .const import (
    DECISION_LOG_FILENAME,
    DECISION_LOG_MAX_AGE_DAYS,
    DECISION_LOG_MAX_BYTES,
    DECISION_LOG_RATE_LIMIT_S,
    DECISION_LOG_SUBDIR,
)
from .models import EvBalancerState, EvDecision, SensorSnapshot

_LOGGER = logging.getLogger(__name__)


def _target_w_bucket(w: float) -> int:
    """Round brain_target_ev_w to nearest 500W bucket for rate-limit hash."""
    return int(round(w / 500) * 500)


class DecisionLog:
    """JSONL writer for ev_balancer decisions.

    Rate-limit: max 1 identical entry (same action+reason+target_bucket) per minute.
    Refusal reasons always logged (even HOLD).
    Rotation: 10 MB size limit + 7-day age limit.
    """

    def __init__(self, config_dir: str) -> None:
        log_dir = Path(config_dir) / DECISION_LOG_SUBDIR
        log_dir.mkdir(exist_ok=True)
        self._path = log_dir / DECISION_LOG_FILENAME
        # rate-limit table: hash → last_logged_ts
        self._rate_table: dict[str, float] = {}

    async def async_log(
        self,
        snap: SensorSnapshot,
        decision: EvDecision,
        state: EvBalancerState,
    ) -> None:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._log_sync, snap, decision, state
            )
        except Exception:
            _LOGGER.exception("decision_log write error")

    def _log_sync(
        self,
        snap: SensorSnapshot,
        decision: EvDecision,
        state: EvBalancerState,
    ) -> None:
        now = time.time()

        # Build rate-limit key
        bucket = _target_w_bucket(snap.brain_target_ev_w)
        reason_str = decision.reason.value if decision.reason else "none"
        key = hashlib.md5(f"{decision.action.value}:{reason_str}:{bucket}".encode()).hexdigest()

        # Always log refusals (reason != None) and HW writes (SET_DYNAMIC/PAUSE/RESUME/STOP)
        is_refusal = decision.reason is not None
        is_hw_write = decision.action.value in ("set_dynamic", "pause", "resume", "stop")
        skip_rate_limit = is_refusal or is_hw_write

        if not skip_rate_limit:
            last = self._rate_table.get(key, 0.0)
            if now - last < DECISION_LOG_RATE_LIMIT_S:
                return
        self._rate_table[key] = now

        # Purge stale rate-limit entries
        if len(self._rate_table) > 500:
            cutoff = now - DECISION_LOG_RATE_LIMIT_S * 10
            self._rate_table = {k: v for k, v in self._rate_table.items() if v > cutoff}

        entry = {
            "schema_version": 1,
            "ts": now,
            "action": decision.action.value,
            "dynamic_a": decision.dynamic_a,
            "reason": reason_str,
            "status": decision.status.value,
            "brain_target_w": snap.brain_target_ev_w,
            "ev_soc": snap.ev_soc,
            "last_dynamic_a": state.last_dynamic_a,
            "rejected_w": decision.rejected_w,
            "dwell_remaining_s": decision.dwell_remaining_s,
            "shadow": snap.balancer_disabled,
        }

        self._rotate_if_needed()
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _rotate_if_needed(self) -> None:
        if not self._path.exists():
            return
        stat = self._path.stat()
        age_s = time.time() - stat.st_mtime
        if stat.st_size >= DECISION_LOG_MAX_BYTES or age_s >= DECISION_LOG_MAX_AGE_DAYS * 86400:
            try:
                backup = self._path.with_suffix(".jsonl.bak")
                self._path.rename(backup)
                _LOGGER.info("decision_log rotated: %s → %s", self._path.name, backup.name)
            except OSError:
                _LOGGER.exception("decision_log rotation failed")
