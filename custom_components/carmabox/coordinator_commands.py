"""CARMA Box Coordinator — Battery & EV command methods.

Mixin class: methods use self (coordinator) attributes directly.
Extracted from coordinator.py to reduce file size.

PLAT-1140: Step 1 of coordinator refactor.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


class CommandsMixin:
    """Battery and EV command methods — mixed into CarmaboxCoordinator."""

    # Methods will be moved here one at a time in subsequent commits.
    # Each commit: move method from coordinator.py → here, verify tests pass.
    pass
