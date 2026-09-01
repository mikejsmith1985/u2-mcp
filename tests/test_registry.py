"""Tests for per-caller database connections."""

import json
from pathlib import Path

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.config import U2Config
from u2_mcp.credentials import CredentialError, MappedCredentialResolver
from u2_mcp.identity import CallerIdentity, use_caller
from u2_mcp.registry import ConnectionRegistry


@pytest.fixture
def two_user_map(tmp_path: Path) -> Path:
    """Write a credential map giving two people their own Universe logins."""
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "alice@example.com": {"user": "ALICE", "password": "pw1", "account": "SALES"},
                "bob@example.com": {"user": "BOB", "password": "pw2", "account": "PURCH"},
            }
        ),
        encoding="utf-8",
    )
    return path


ALICE = CallerIdentity(subject="alice@example.com", name="Alice")
BOB = CallerIdentity(subject="bob@example.com", name="Bob")


class TestSharedMode:
    """The default: one login for everyone, as upstream behaved."""

    def test_all_callers_share_one_connection(
        self, mock_config: U2Config, mock_uopy: MockSession
    ) -> None:
        """Two callers resolve to the same connection slot."""
        registry = ConnectionRegistry(mock_config)

        assert registry.for_caller(ALICE) is registry.for_caller(BOB)
        assert registry.get_stats()["connection_count"] == 1

    def test_the_shared_login_is_marked_shared(
        self, mock_config: U2Config, mock_uopy: MockSession
    ) -> None:
        """The manager reports that its login cannot distinguish callers."""
        registry = ConnectionRegistry(mock_config)

        assert registry.for_caller(ALICE).credentials.is_shared is True


class TestMappedMode:
    """Each person reaching the database as themselves."""

    def test_each_caller_gets_their_own_connection(
        self, mock_config: U2Config, mock_uopy: MockSession, two_user_map: Path
    ) -> None:
        """Two people hold two separate sessions under two separate logins."""
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, two_user_map)
        )

        assert registry.for_caller(ALICE) is not registry.for_caller(BOB)
        assert registry.active_logins() == ["ALICE@SALES", "BOB@PURCH"]

    def test_the_database_receives_the_callers_own_login(
        self, mock_config: U2Config, mock_uopy: MockSession, two_user_map: Path
    ) -> None:
        """The credentials sent to Universe are the caller's, not the server's."""
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, two_user_map)
        )

        registry.for_caller(ALICE).connect()

        assert mock_uopy.connect_kwargs["user"] == "ALICE"
        assert mock_uopy.connect_kwargs["account"] == "SALES"

    def test_the_same_caller_reuses_their_connection(
        self, mock_config: U2Config, mock_uopy: MockSession, two_user_map: Path
    ) -> None:
        """Asking twice for one person does not open a second session."""
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, two_user_map)
        )

        registry.for_caller(ALICE)
        registry.for_caller(ALICE)

        assert registry.get_stats()["connection_count"] == 1

    def test_an_unmapped_caller_is_refused(
        self, mock_config: U2Config, mock_uopy: MockSession, two_user_map: Path
    ) -> None:
        """Someone with no mapped login gets no connection at all."""
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, two_user_map)
        )

        with pytest.raises(CredentialError):
            registry.for_caller(CallerIdentity(subject="stranger@example.com"))

        assert registry.active_logins() == []


class TestCurrentCaller:
    """Resolving the connection from the caller of the current request."""

    def test_current_follows_the_caller_in_scope(
        self, mock_config: U2Config, mock_uopy: MockSession, two_user_map: Path
    ) -> None:
        """The connection returned matches whoever the request belongs to."""
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, two_user_map)
        )

        with use_caller(ALICE):
            assert registry.current().credentials.user == "ALICE"

        with use_caller(BOB):
            assert registry.current().credentials.user == "BOB"


class TestRegistryOperations:
    """Operating on every held connection at once."""

    def test_disconnect_all_clears_every_slot(
        self, mock_config: U2Config, mock_uopy: MockSession, two_user_map: Path
    ) -> None:
        """Shutting down closes each caller's connection."""
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, two_user_map)
        )
        registry.for_caller(ALICE).connect()
        registry.for_caller(BOB).connect()

        assert registry.disconnect_all() == 2
        assert registry.active_logins() == []
