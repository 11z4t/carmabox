"""Unit test configuration — isolate from HA integration test framework.

Two isolation problems solved here:

1. pytest-homeassistant-custom-component registers autouse fixtures
   (verify_cleanup, enable_event_loop_debug, socket_enabled) that assume a
   full HA event loop — override them to no-ops for unit tests.

2. IT-2466 module purge in integration/e2e teardown replaces class objects in
   sys.modules, breaking isinstance checks in unit tests that imported at
   module-load time.  Force-restore original modules before each unit test.
"""

from __future__ import annotations

import sys
from collections.abc import Generator

import pytest

_CARMABOX_PREFIX = "custom_components.carmabox"
# Will be populated by pytest_collection_modifyitems after all test imports
_module_snapshot: dict[str, object] = {}


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Snapshot carmabox modules after all test files have been collected."""
    global _module_snapshot
    if not _module_snapshot:
        _module_snapshot.update(
            {
                name: mod
                for name, mod in sys.modules.items()
                if name == _CARMABOX_PREFIX or name.startswith(f"{_CARMABOX_PREFIX}.")
            }
        )


@pytest.fixture(autouse=True)
def _restore_carmabox_modules() -> Generator[None]:
    """Force-restore carmabox modules purged by HA integration/e2e teardown."""
    if _module_snapshot:
        for name, mod in _module_snapshot.items():
            sys.modules[name] = mod  # type: ignore[assignment]
    yield


@pytest.fixture(autouse=True)
def verify_cleanup() -> Generator[None]:
    """No-op override — unit tests don't need HA lingering-task checks."""
    yield


@pytest.fixture(autouse=True)
def enable_event_loop_debug() -> None:
    """No-op override — unit tests don't need HA event loop debug mode."""


@pytest.fixture(autouse=True)
def socket_enabled() -> Generator[None]:
    """No-op override — unit tests don't need HA socket restrictions."""
    yield


@pytest.fixture(autouse=True)
def expected_lingering_tasks() -> bool:
    """Override HA default — not applicable to unit tests."""
    return False


@pytest.fixture(autouse=True)
def expected_lingering_timers() -> bool:
    """Override HA default — not applicable to unit tests."""
    return False
