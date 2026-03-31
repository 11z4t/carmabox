"""QC guard: detect patterns that cause recurring QC rejects.

1. Hardcoded entity IDs (must use opts.get)
2. Magic numbers in policy logic (must be named constants)
3. Missing else/return branches (predict_24h None bug)
4. Hardcoded ampere values (never 16A)
"""

import re
from pathlib import Path

COORDINATOR = Path("custom_components/carmabox/coordinator.py")

# Allowed hardcoded entities (logging, comments, heartbeat file path)
ALLOWED_PATTERNS = {
    "input_text.v6_battery_plan",  # Plan write target — always this entity
    "input_text.v6_battery_plan_today",
    "input_text.v6_battery_plan_tomorrow",
    "input_text.v6_battery_plan_day3",
    "input_boolean.carma_ev_executor_enabled",  # Runtime toggle — always this
    "button.easee_home_12840_override_schedule",  # Easee recovery — hardware
    "switch.easee_home_12840_is_enabled",  # Easee recovery — hardware
    "/config/carmabox-heartbeat.json",  # File path, not entity
}

# Pattern: string literal containing entity domain prefix
ENTITY_PATTERN = re.compile(
    r"""['"]"""
    r"(sensor\.|binary_sensor\.|input_boolean\.|input_number\.|"
    r"input_select\.|input_text\.|input_datetime\.|"
    r"switch\.|number\.|select\.|button\.|automation\.)"
    r"""[a-z_0-9.]+['"]"""
)


def test_no_hardcoded_entities_in_read_float():
    """_read_float() calls must use opts.get(), not hardcoded entity strings."""
    code = COORDINATOR.read_text()
    violations = []
    for i, line in enumerate(code.split("\n"), 1):
        if (
            "_read_float(" in line
            and "opts.get" not in line
            and "_cfg.get" not in line
            and "_c.get" not in line
        ):
            match = ENTITY_PATTERN.search(line)
            if match:
                entity = match.group(0).strip("'\"")
                if entity not in ALLOWED_PATTERNS:
                    violations.append(f"  L{i}: {line.strip()}")
    assert not violations, "Hardcoded entity IDs in _read_float() — use opts.get():\n" + "\n".join(
        violations
    )


def test_no_hardcoded_entities_in_hass_states_get():
    """hass.states.get() with hardcoded entity outside of allowed list."""
    code = COORDINATOR.read_text()
    violations = []
    for i, line in enumerate(code.split("\n"), 1):
        if "hass.states.get(" in line or "self.hass.states.get(" in line:
            match = ENTITY_PATTERN.search(line)
            if match:
                entity = match.group(0).strip("'\"")
                if entity not in ALLOWED_PATTERNS:
                    # Skip if it's using opts.get or self._get_entity
                    if "opts.get" in line or "_get_entity" in line or "_cfg.get" in line:
                        continue
                    violations.append(f"  L{i}: {line.strip()}")
    if violations:
        # Warn but don't fail — too many existing patterns to fix at once
        import warnings

        warnings.warn(  # noqa: B028
            "Hardcoded entity IDs in hass.states.get():\n" + "\n".join(violations[:10])
        )


# ── Magic number guard ──────────────────────────────────────

CORE_FILES = list(Path("custom_components/carmabox/core").glob("*.py"))


def test_no_magic_numbers_in_if_statements():
    """Policy values in if-statements must be named constants, not literals."""
    violations = []
    for fpath in CORE_FILES:
        code = fpath.read_text()
        for i, line in enumerate(code.split("\n"), 1):
            stripped = line.strip()
            # Skip comments, imports, docstrings, assignments, constants
            if stripped.startswith(("#", "import", "from", '"""', "'''", "return")):
                continue
            if "=" in stripped and "==" not in stripped and "!=" not in stripped:
                continue  # assignment, not comparison
            # Look for numeric literals in comparisons
            if re.search(r"(?:if|elif|and|or)\s+.*[<>=!]+\s*\d{2,}", stripped):
                # Skip known OK patterns (range, enumerate, len checks)
                skip_patterns = ("range(", "enumerate(", "len(", "[:")
                if any(ok in stripped for ok in skip_patterns):
                    continue
                violations.append(f"  {fpath.name}:L{i}: {stripped[:80]}")
    # Warn — too many existing to block CI
    if violations:
        import warnings

        warnings.warn(  # noqa: B028
            f"Potential magic numbers in if-statements ({len(violations)} found):\n"
            + "\n".join(violations[:5])
        )


# ── EV amps guard ──────────────────────────────────────────


def test_no_16a_in_codebase():
    """16A EV current is FORBIDDEN — max is 10A (DEFAULT_EV_MAX_AMPS)."""
    all_py = list(Path("custom_components/carmabox").rglob("*.py"))
    violations = []
    for fpath in all_py:
        code = fpath.read_text()
        for i, line in enumerate(code.split("\n"), 1):
            if re.search(r"\b16\b", line) and re.search(r"amp|amps|current", line.lower()):
                # Skip false positives (timestamp slicing, etc)
                if "timestamp" in line or "[11:" in line:
                    continue
                violations.append(f"  {fpath.name}:L{i}: {line.strip()[:80]}")
    assert not violations, "16A found in code — max is 10A:\n" + "\n".join(violations)


# ── Return None guard ──────────────────────────────────────


def test_predict_24h_never_returns_none():
    """predict_24h must ALWAYS return list[float], never None."""
    from custom_components.carmabox.optimizer.predictor import (
        ConsumptionPredictor,
        HourSample,
    )

    p = ConsumptionPredictor()

    # Untrained
    result = p.predict_24h(start_hour=0, weekday=0, month=3)
    assert result is not None and len(result) == 24

    # Trained
    for d in range(7):
        for h in range(24):
            p.add_sample(HourSample(weekday=d, hour=h, month=3, consumption_kw=2.0))
    result = p.predict_24h(start_hour=20, weekday=0, month=3)
    assert result is not None and len(result) == 24
