"""Adversarial tests against the hardening work itself.

Every fix is new code, and new code is new attack surface. These tests attack the
remediations rather than the original defects: they ask what happens when the new
machinery fails, and insist that failing does not quietly grant more than it
should.
"""

import json
from pathlib import Path

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.config import U2Config
from u2_mcp.credentials import CredentialError, MappedCredentialResolver
from u2_mcp.identity import (
    CallerIdentity,
    IdentityError,
    current_caller,
    set_identity_resolver,
)
from u2_mcp.registry import ConnectionRegistry


@pytest.fixture(autouse=True)
def clear_resolver() -> None:
    """Make sure no test leaves an identity resolver installed."""
    yield
    set_identity_resolver(None)


class TestIdentityFailsClosed:
    """When the server cannot say who is calling, it must not guess."""

    def test_a_failing_resolver_does_not_become_a_local_operator(self) -> None:
        """A token lookup that throws must not silently downgrade to unauthenticated.

        Falling back to the local operator would let a caller launder their
        identity out of the audit trail by making resolution fail.
        """

        def broken_resolver() -> CallerIdentity | None:
            raise RuntimeError("token store unavailable")

        set_identity_resolver(broken_resolver)

        with pytest.raises(IdentityError):
            current_caller()

    def test_an_unresolvable_token_is_refused(self) -> None:
        """A resolver that recognises nobody must refuse, not fall back."""
        set_identity_resolver(lambda: None)

        with pytest.raises(IdentityError):
            current_caller()

    def test_no_resolver_still_means_local_stdio_use(self) -> None:
        """With no resolver installed at all, stdio mode still works unauthenticated."""
        set_identity_resolver(None)

        assert current_caller().subject == "local"


class TestUnmappedCallersGetNothing:
    """Mapped mode must not leak the shared account to anyone unmapped."""

    @pytest.fixture
    def mapped_registry(
        self, mock_config: U2Config, mock_uopy: MockSession, tmp_path: Path
    ) -> ConnectionRegistry:
        """Build a registry that maps exactly one person."""
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"alice@example.com": {"user": "ALICE", "password": "pw"}}),
            encoding="utf-8",
        )
        return ConnectionRegistry(mock_config, MappedCredentialResolver(mock_config, path))

    def test_the_local_caller_cannot_reach_the_database_in_mapped_mode(
        self, mapped_registry: ConnectionRegistry
    ) -> None:
        """The unauthenticated fallback identity has no mapped login, so it is refused."""
        with pytest.raises(CredentialError):
            mapped_registry.for_caller(CallerIdentity(subject="local", is_authenticated=False))

    def test_a_refused_caller_opens_no_connection(
        self, mapped_registry: ConnectionRegistry
    ) -> None:
        """A refusal must not leave a usable connection slot behind."""
        with pytest.raises(CredentialError):
            mapped_registry.for_caller(CallerIdentity(subject="stranger@example.com"))

        assert mapped_registry.active_logins() == []


class TestConnectionRegistryLimits:
    """One connection per caller must not become unbounded connections."""

    @pytest.fixture
    def crowded_registry(
        self, mock_config: U2Config, mock_uopy: MockSession, tmp_path: Path
    ) -> ConnectionRegistry:
        """Build a registry mapping more people than the connection limit allows."""
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps(
                {f"user{n}@example.com": {"user": f"U{n}", "password": "pw"} for n in range(20)}
            ),
            encoding="utf-8",
        )
        mock_config.max_connections = 5
        return ConnectionRegistry(mock_config, MappedCredentialResolver(mock_config, path))

    def test_connections_do_not_grow_without_limit(
        self, crowded_registry: ConnectionRegistry
    ) -> None:
        """Twenty callers must not hold twenty database sessions open at once."""
        for n in range(20):
            crowded_registry.for_caller(CallerIdentity(subject=f"user{n}@example.com"))

        assert crowded_registry.get_stats()["connection_count"] <= 5

    def test_the_most_recent_caller_keeps_their_connection(
        self, crowded_registry: ConnectionRegistry
    ) -> None:
        """Eviction takes the least recently used slot, not the one in use."""
        for n in range(20):
            crowded_registry.for_caller(CallerIdentity(subject=f"user{n}@example.com"))

        assert "U19@TEST" in crowded_registry.active_logins()


class TestCredentialMapSafety:
    """The credential map holds plaintext passwords, so it must be handled as a secret."""

    def test_a_password_can_come_from_the_environment_instead_of_the_file(
        self, mock_config: U2Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator can keep passwords in a secret store rather than on disk."""
        monkeypatch.setenv("ALICE_U2_PASSWORD", "from-the-vault")
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps(
                {"alice@example.com": {"user": "ALICE", "password_env": "ALICE_U2_PASSWORD"}}
            ),
            encoding="utf-8",
        )

        resolver = MappedCredentialResolver(mock_config, path)

        assert resolver.resolve(CallerIdentity(subject="alice@example.com")).password == (
            "from-the-vault"
        )

    def test_a_named_environment_variable_that_is_unset_is_refused(
        self, mock_config: U2Config, tmp_path: Path
    ) -> None:
        """A password that cannot be found fails loudly rather than connecting as nobody."""
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps(
                {"alice@example.com": {"user": "ALICE", "password_env": "NOT_SET_ANYWHERE"}}
            ),
            encoding="utf-8",
        )

        resolver = MappedCredentialResolver(mock_config, path)

        with pytest.raises(CredentialError):
            resolver.resolve(CallerIdentity(subject="alice@example.com"))

    def test_the_credential_map_is_never_echoed_in_an_error(
        self, mock_config: U2Config, tmp_path: Path
    ) -> None:
        """An error about a missing caller must not print other people's credentials."""
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"alice@example.com": {"user": "ALICE", "password": "s3cret-pw"}}),
            encoding="utf-8",
        )
        resolver = MappedCredentialResolver(mock_config, path)

        with pytest.raises(CredentialError) as exc_info:
            resolver.resolve(CallerIdentity(subject="stranger@example.com"))

        assert "s3cret-pw" not in str(exc_info.value)
        assert "ALICE" not in str(exc_info.value)
