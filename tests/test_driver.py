"""The driver seam must not change what the default path does.

A seam added for evaluation is worth nothing if it alters production behaviour,
so the first thing asserted is that the default is still uopy and still resolved
the same way. The rest holds a replacement driver to the surface this server
calls, checked at load rather than discovered at the first query.
"""

import sys
from types import ModuleType

import pytest

from u2_mcp.driver import (
    DEFAULT_DRIVER,
    REQUIRED_NAMES,
    DriverError,
    get_driver,
    reset_driver,
)


@pytest.fixture(autouse=True)
def clear_driver() -> None:
    """Ensure no test leaves a driver resolved for the next one."""
    reset_driver()
    yield
    reset_driver()


def make_driver(missing: str | None = None) -> ModuleType:
    """Build a module providing the driver surface, optionally missing one name."""
    module = ModuleType("fake_driver")
    for name in REQUIRED_NAMES:
        if name == missing:
            continue
        setattr(module, name, type(name, (), {}))
    return module


class TestTheDefaultIsUnchanged:
    """Adding a seam must not alter what production does."""

    def test_the_default_driver_is_uopy(self) -> None:
        """Nothing configured means the real database library."""
        assert DEFAULT_DRIVER == "uopy"

    def test_no_configuration_loads_uopy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset variable resolves to uopy, not to a demonstration driver."""
        monkeypatch.delenv("U2_DRIVER", raising=False)

        assert get_driver().__name__ == "uopy"

    def test_naming_uopy_explicitly_loads_uopy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default can be named without changing what it means."""
        monkeypatch.setenv("U2_DRIVER", "uopy")

        assert get_driver().__name__ == "uopy"

    def test_the_driver_is_resolved_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolution is cached, so configuration cannot change under a session."""
        monkeypatch.delenv("U2_DRIVER", raising=False)

        assert get_driver() is get_driver()


class TestReplacementDrivers:
    """A driver may be replaced, and must be complete when it is."""

    def test_a_module_path_can_be_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Any importable module providing the surface may be used."""
        monkeypatch.setitem(sys.modules, "fake_driver", make_driver())
        monkeypatch.setenv("U2_DRIVER", "fake_driver")

        assert get_driver().__name__ == "fake_driver"

    @pytest.mark.parametrize("missing", REQUIRED_NAMES)
    def test_an_incomplete_driver_is_refused_at_load(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        """Missing a name must fail at startup, not at the first query.

        A driver that loads and then fails mid-request is far harder to diagnose
        than one that refuses to load at all.
        """
        monkeypatch.setitem(sys.modules, "fake_driver", make_driver(missing=missing))
        monkeypatch.setenv("U2_DRIVER", "fake_driver")

        with pytest.raises(DriverError) as error:
            get_driver()

        assert missing in str(error.value)

    def test_an_unimportable_driver_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A name that resolves to nothing fails with the name in the message."""
        monkeypatch.setenv("U2_DRIVER", "no.such.module")

        with pytest.raises(DriverError) as error:
            get_driver()

        assert "no.such.module" in str(error.value)

    def test_a_replacement_is_announced(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Running against something other than a database must be visible.

        Someone reading the log needs to know the figures did not come from a
        production system.
        """
        monkeypatch.setitem(sys.modules, "fake_driver", make_driver())
        monkeypatch.setenv("U2_DRIVER", "fake_driver")

        with caplog.at_level("WARNING"):
            get_driver()

        assert "not connected to a Universe" in caplog.text

    def test_the_default_is_not_announced(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A production deployment should not carry a warning about itself."""
        monkeypatch.delenv("U2_DRIVER", raising=False)

        with caplog.at_level("WARNING"):
            get_driver()

        assert "not connected to a Universe" not in caplog.text
