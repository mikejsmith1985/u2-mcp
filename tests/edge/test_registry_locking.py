"""The connection registry must not stall every caller to serve one.

Two hazards live in the registry. Closing an evicted connection can block, since
the socket may be wedged -- which is why the connection was evicted. And writing
an audit record must not itself open a connection slot, or the act of recording
work would displace someone else's session.
"""

import json
import threading
from pathlib import Path

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.config import U2Config
from u2_mcp.credentials import MappedCredentialResolver
from u2_mcp.identity import CallerIdentity, use_caller
from u2_mcp.registry import ConnectionRegistry


@pytest.fixture
def credential_map(tmp_path: Path) -> Path:
    """Write a credential map for several callers."""
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({f"user{n}@x.com": {"user": f"U{n}", "password": "pw"} for n in range(10)}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def registry(
    mock_config: U2Config, mock_uopy: MockSession, credential_map: Path
) -> ConnectionRegistry:
    """A registry with room for two connections."""
    mock_config.max_connections = 2
    return ConnectionRegistry(mock_config, MappedCredentialResolver(mock_config, credential_map))


class TestEvictionDoesNotBlockOthers:
    """Closing a wedged connection must not hold up unrelated callers."""

    def test_a_slow_eviction_does_not_stall_the_registry(
        self, registry: ConnectionRegistry
    ) -> None:
        """While an evicted connection is closing, other callers still get answers.

        The connection being evicted may be the one that stopped responding, so
        closing it can block for as long as the network timeout. If that happens
        while the registry's lock is held, one wedged socket freezes every caller.
        """
        victim = registry.for_caller(CallerIdentity(subject="user0@x.com"))
        victim.connect()

        closing = threading.Event()
        release = threading.Event()
        original_disconnect = victim.disconnect_all

        def slow_disconnect() -> int:
            closing.set()
            release.wait(timeout=5)
            return original_disconnect()

        victim.disconnect_all = slow_disconnect  # type: ignore[method-assign]

        def fill_the_registry() -> None:
            for n in range(1, 5):
                registry.for_caller(CallerIdentity(subject=f"user{n}@x.com"))

        filler = threading.Thread(target=fill_the_registry)
        filler.start()

        assert closing.wait(timeout=5), "eviction never started"

        # The registry must answer another thread while the eviction is in flight.
        answered = threading.Event()

        def ask_registry() -> None:
            registry.active_logins()
            answered.set()

        asker = threading.Thread(target=ask_registry)
        asker.start()

        try:
            assert answered.wait(timeout=2), "the registry was blocked by a slow eviction"
        finally:
            release.set()
            asker.join(timeout=5)
            filler.join(timeout=5)


class TestAuditDoesNotConsumeConnections:
    """Recording what happened must not change what is connected."""

    def test_reporting_the_login_opens_no_connection_slot(
        self, registry: ConnectionRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Naming the login for an audit record must not create a session for it."""
        import u2_mcp.server as server_module

        monkeypatch.setattr(server_module, "_connection_registry", registry)
        monkeypatch.setattr(server_module, "_connection_manager", None)

        with use_caller(CallerIdentity(subject="user7@x.com")):
            login = server_module._current_login()

        assert login is not None
        assert login.user == "U7"
        assert registry.get_stats()["connection_count"] == 0

    def test_an_unmapped_caller_reports_no_login(
        self, registry: ConnectionRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller with no credentials produces no login, and no error."""
        import u2_mcp.server as server_module

        monkeypatch.setattr(server_module, "_connection_registry", registry)
        monkeypatch.setattr(server_module, "_connection_manager", None)

        with use_caller(CallerIdentity(subject="stranger@x.com")):
            assert server_module._current_login() is None
