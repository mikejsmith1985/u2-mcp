"""Writes are opt-in, because the person who forgets is the one who needs it.

The hardening notes already name this: describing what unauthenticated network
exposure cost, they say "read-only mode is off by default, so that includes
writing and deleting records." The fix that followed closed the bind address and
the CORS policy and left the default alone -- so the sentence stayed true.

It matters most for the case this server is actually offered for. Someone
evaluating it points it at a database they care about, having read that it is
read-only, and gets a server that will write if a caller asks twice. The
guarantee they are relying on is one environment variable they were never told
to set.

Every other safety setting in this fork fails closed. This one now does too:
`U2_READ_ONLY=false` is available and has to be chosen, which is exactly the
moment somebody is thinking about writes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from u2_mcp.config import U2Config

# The four settings a config needs before it will build. No database is reached
# by any test here.
BASE = {
    "U2_HOST": "test-host.example.com",
    "U2_USER": "tester",
    "U2_PASSWORD": "not-a-real-password",
    "U2_ACCOUNT": "TESTING",
}


@pytest.fixture
def environment(monkeypatch):
    """A clean environment holding only the required settings."""
    for name in (*BASE, "U2_READ_ONLY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


class TestTheDefault:
    def test_a_server_nobody_configured_is_read_only(self, environment) -> None:
        # The whole point. Someone who sets the four connection settings and
        # nothing else has a server that cannot write.
        assert U2Config().read_only is True

    def test_writes_can_still_be_chosen(self, environment) -> None:
        # Opt-in, not removed. A loader or an administrator has a real need, and
        # setting this is the moment they are thinking about it.
        environment.setenv("U2_READ_ONLY", "false")

        assert U2Config().read_only is False

    @pytest.mark.parametrize("written", ["true", "True", "TRUE", "1"])
    def test_asking_for_read_only_explicitly_also_works(
        self, environment, written: str
    ) -> None:
        environment.setenv("U2_READ_ONLY", written)

        assert U2Config().read_only is True


class TestTheSettingIsNotSilentlyIgnored:
    def test_an_unreadable_value_is_refused_rather_than_assumed(
        self, environment
    ) -> None:
        # A misspelling must not quietly become one setting or the other. Someone
        # who wrote U2_READ_ONLY=yes meant something, and guessing which is how a
        # safety setting comes to be off while its owner believes it is on.
        environment.setenv("U2_READ_ONLY", "banana")

        with pytest.raises(ValidationError):
            U2Config()
