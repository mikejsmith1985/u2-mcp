"""Which library this server talks to the database through.

By default it is Rocket's `uopy`, against a real Universe or UniData server.

An alternative driver can be named instead, which is what makes this server
evaluable by someone who has no Universe instance — and licensing means most
people who might assess it do not have one. The alternative presents the same
objects `uopy` does, so the server calls exactly the code it would in production
rather than a second path written for demonstrations.

The seam is deliberately narrow: one module, resolved once, holding the five
names this server uses. A driver that changed how the server was written would
change what the tests prove.
"""

from __future__ import annotations

import importlib
import logging
import os
from types import ModuleType

logger = logging.getLogger(__name__)

# The names this server uses from its driver. A replacement must provide all of
# them, checked at load rather than discovered at the first query.
REQUIRED_NAMES = ("connect", "File", "Command", "List", "Subroutine", "UOError")

# The default, and the only driver that reaches a real database.
DEFAULT_DRIVER = "uopy"

_driver: ModuleType | None = None


class DriverError(Exception):
    """Raised when the configured driver cannot be loaded or is incomplete."""


def get_driver() -> ModuleType:
    """Return the driver module, loading it on first use.

    Returns:
        The module providing connect, File, Command, List, Subroutine and UOError

    Raises:
        DriverError: If the configured driver cannot be imported or lacks a name
    """
    global _driver
    if _driver is None:
        _driver = _load(os.environ.get("U2_DRIVER", DEFAULT_DRIVER))
    return _driver


def reset_driver() -> None:
    """Forget the loaded driver, so a later call re-reads the configuration."""
    global _driver
    _driver = None


def _load(name: str) -> ModuleType:
    """Import a driver and confirm it provides everything this server calls.

    Args:
        name: `uopy`, `demo`, or an importable module path

    Returns:
        The imported module

    Raises:
        DriverError: If the module cannot be imported or is missing a name
    """
    module_path = _resolve(name)

    try:
        module = importlib.import_module(module_path)
    except ImportError as error:
        raise DriverError(
            f"Could not load the '{name}' driver from '{module_path}': {error}"
        ) from error

    missing = [required for required in REQUIRED_NAMES if not hasattr(module, required)]
    if missing:
        raise DriverError(
            f"The '{name}' driver is missing: {', '.join(missing)}. A driver must "
            f"provide {', '.join(REQUIRED_NAMES)}."
        )

    if module_path != DEFAULT_DRIVER:
        logger.warning(
            "Using the '%s' driver. This server is not connected to a Universe or "
            "UniData database, and any data it returns is not production data.",
            name,
        )

    return module


def _resolve(name: str) -> str:
    """Return the module path for a driver name."""
    known = {
        "uopy": "uopy",
        "demo": "mvstore.driver",
    }
    return known.get(name.strip().lower(), name)
