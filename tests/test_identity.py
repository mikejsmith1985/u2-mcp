"""Tests for caller identity and per-user database credentials.

Authenticating someone to the server is not the same as the database knowing who
they are. These tests hold the line on both halves: the audit trail must name a
person, and the credentials used to reach Universe must be resolvable per person.
"""

import json
from pathlib import Path

import pytest

from u2_mcp.config import U2Config
from u2_mcp.credentials import (
    CredentialError,
    MappedCredentialResolver,
    SharedCredentialResolver,
    create_credential_resolver,
)
from u2_mcp.identity import (
    LOCAL_CALLER,
    CallerIdentity,
    current_caller,
    set_identity_resolver,
    use_caller,
)


class TestCallerIdentity:
    """Who the server thinks is calling."""

    def test_without_authentication_the_caller_is_local(self) -> None:
        """In stdio mode there is no OAuth, so the caller is the local operator."""
        assert current_caller() == LOCAL_CALLER
        assert LOCAL_CALLER.is_authenticated is False

    def test_a_resolved_caller_is_used(self) -> None:
        """When a resolver reports an authenticated user, that user is the caller."""
        alice = CallerIdentity(subject="alice@example.com", email="alice@example.com", name="Alice")
        set_identity_resolver(lambda: alice)
        try:
            assert current_caller().subject == "alice@example.com"
        finally:
            set_identity_resolver(None)

    def test_caller_can_be_scoped_to_a_block(self) -> None:
        """A caller set for a block does not leak out of it."""
        bob = CallerIdentity(subject="bob@example.com", email="bob@example.com", name="Bob")

        with use_caller(bob):
            assert current_caller().subject == "bob@example.com"

        assert current_caller() == LOCAL_CALLER

    def test_identity_key_distinguishes_users(self) -> None:
        """Two different people produce two different keys."""
        alice = CallerIdentity(subject="alice@example.com")
        bob = CallerIdentity(subject="bob@example.com")

        assert alice.identity_key != bob.identity_key

    def test_display_name_prefers_something_a_human_recognises(self) -> None:
        """The audit trail should read as a person, not an opaque subject id."""
        caller = CallerIdentity(subject="auth0|653f", email="alice@example.com", name="Alice")

        assert caller.display_name == "Alice"


class TestSharedCredentials:
    """The upstream behaviour, kept but made explicit."""

    def test_every_caller_gets_the_configured_account(self, mock_config: U2Config) -> None:
        """A shared resolver hands the same credentials to everyone."""
        resolver = SharedCredentialResolver(mock_config)

        alice = resolver.resolve(CallerIdentity(subject="alice@example.com"))
        bob = resolver.resolve(CallerIdentity(subject="bob@example.com"))

        assert alice.user == bob.user == "test-user"

    def test_shared_credentials_declare_that_they_are_shared(self, mock_config: U2Config) -> None:
        """The resolver states plainly that the database cannot tell callers apart."""
        resolver = SharedCredentialResolver(mock_config)

        assert resolver.resolve(CallerIdentity(subject="alice@example.com")).is_shared is True


class TestMappedCredentials:
    """Each person reaching the database as themselves."""

    @pytest.fixture
    def credential_map(self, tmp_path: Path) -> Path:
        """Write a credential map naming one user."""
        path = tmp_path / "u2-credentials.json"
        path.write_text(
            json.dumps(
                {
                    "alice@example.com": {
                        "user": "ALICE",
                        "password": "alice-pw",
                        "account": "SALES",
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_mapped_caller_gets_their_own_login(
        self, mock_config: U2Config, credential_map: Path
    ) -> None:
        """A mapped user reaches Universe under their own account."""
        resolver = MappedCredentialResolver(mock_config, credential_map)

        credentials = resolver.resolve(CallerIdentity(subject="alice@example.com"))

        assert credentials.user == "ALICE"
        assert credentials.account == "SALES"
        assert credentials.is_shared is False

    def test_unmapped_caller_is_refused(self, mock_config: U2Config, credential_map: Path) -> None:
        """An unmapped user is refused rather than quietly falling back to a shared login."""
        resolver = MappedCredentialResolver(mock_config, credential_map)

        with pytest.raises(CredentialError) as exc_info:
            resolver.resolve(CallerIdentity(subject="stranger@example.com"))

        assert "stranger@example.com" in str(exc_info.value)

    def test_missing_map_is_refused_at_startup(self, mock_config: U2Config, tmp_path: Path) -> None:
        """A missing credential map fails immediately, not on the first query."""
        with pytest.raises(CredentialError):
            MappedCredentialResolver(mock_config, tmp_path / "absent.json")


class TestResolverSelection:
    """Choosing a resolver from configuration."""

    def test_shared_is_the_default(self, mock_config: U2Config) -> None:
        """Without configuration the behaviour matches upstream."""
        assert isinstance(create_credential_resolver(mock_config), SharedCredentialResolver)

    def test_unknown_mode_is_refused(self, mock_config: U2Config) -> None:
        """A misspelled identity mode fails loudly rather than defaulting to shared."""
        mock_config.identity_mode = "kerberos"

        with pytest.raises(CredentialError):
            create_credential_resolver(mock_config)
