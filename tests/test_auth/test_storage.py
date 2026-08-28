"""Contract tests every auth storage backend must satisfy.

Both backends are held to the same behaviour, so swapping one for the other
cannot change how authentication behaves. The durable backend has extra tests
of its own for the two things only it can promise: surviving a restart, and not
writing usable bearer tokens to disk.
"""

import time
from pathlib import Path
from typing import Any

import pytest

from u2_mcp.auth.sqlite_storage import SQLiteAuthStorage
from u2_mcp.auth.storage import (
    InMemoryAuthStorage,
    PendingAuthorization,
    StoredAuthCode,
    StoredClient,
    StoredToken,
)


def make_client(client_id: str = "client-1") -> StoredClient:
    """Build a registered client for testing."""
    return StoredClient(
        client_id=client_id,
        client_secret="s3cret",
        client_name="Claude",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="u2:read",
    )


def make_token(token: str = "tok-1", token_type: str = "access", **overrides: Any) -> StoredToken:
    """Build an issued token for testing."""
    values: dict[str, Any] = {
        "token": token,
        "token_type": token_type,
        "client_id": "client-1",
        "user_subject": "mike@example.com",
        "scope": "u2:read",
        "expires_at": time.time() + 3600,
        "user_claims": {"email": "mike@example.com", "name": "Mike"},
    }
    values.update(overrides)
    return StoredToken(**values)


def make_auth_code(code: str = "code-1") -> StoredAuthCode:
    """Build a pending authorization code for testing."""
    return StoredAuthCode(
        code=code,
        client_id="client-1",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        scope="u2:read",
        state="abc",
        code_challenge="challenge",
        code_challenge_method="S256",
        user_subject="mike@example.com",
        user_claims={"email": "mike@example.com"},
    )


def make_pending(state: str = "state-1") -> PendingAuthorization:
    """Build in-flight authorization state for testing."""
    return PendingAuthorization(
        state=state,
        client_id="client-1",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        scope="u2:read",
        code_challenge=None,
        code_challenge_method=None,
        claude_redirect_uri="https://claude.ai/api/mcp/auth_callback",
        claude_state="claude-state",
    )


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """Provide each storage backend in turn, so both meet the same contract."""
    if request.param == "memory":
        return InMemoryAuthStorage()
    return SQLiteAuthStorage(tmp_path / "auth.db")


class TestClientRegistration:
    """Storing and retrieving DCR clients."""

    def test_stored_client_can_be_read_back(self, storage: Any) -> None:
        """A registered client is returned intact."""
        storage.store_client(make_client())

        found = storage.get_client("client-1")

        assert found is not None
        assert found.client_name == "Claude"
        assert found.redirect_uris == ["https://claude.ai/api/mcp/auth_callback"]

    def test_unknown_client_is_none(self, storage: Any) -> None:
        """An unregistered client id returns nothing."""
        assert storage.get_client("nope") is None

    def test_client_is_findable_by_redirect_uris(self, storage: Any) -> None:
        """Re-registration finds the existing client by its redirect URIs."""
        storage.store_client(make_client())

        found = storage.find_client_by_redirect_uris(["https://claude.ai/api/mcp/auth_callback"])

        assert found is not None
        assert found.client_id == "client-1"

    def test_deleting_a_client_removes_its_tokens(self, storage: Any) -> None:
        """Deleting a client revokes what was issued to it."""
        storage.store_client(make_client())
        storage.store_access_token(make_token("tok-1"))

        assert storage.delete_client("client-1") is True
        assert storage.get_client("client-1") is None
        assert storage.get_access_token("tok-1") is None

    def test_correct_secret_validates(self, storage: Any) -> None:
        """A matching client secret authenticates the client."""
        storage.store_client(make_client())

        assert storage.validate_client_credentials("client-1", "s3cret") is not None

    def test_wrong_secret_is_rejected(self, storage: Any) -> None:
        """A mismatched client secret does not authenticate."""
        storage.store_client(make_client())

        assert storage.validate_client_credentials("client-1", "wrong") is None


class TestTokens:
    """Access and refresh token lifecycle."""

    def test_access_token_round_trips_with_its_claims(self, storage: Any) -> None:
        """A stored token returns with the identity it was issued for."""
        storage.store_access_token(make_token("tok-1"))

        found = storage.get_access_token("tok-1")

        assert found is not None
        assert found.user_subject == "mike@example.com"
        assert found.user_claims["name"] == "Mike"

    def test_expired_access_token_is_not_returned(self, storage: Any) -> None:
        """A token past its expiry is treated as absent."""
        storage.store_access_token(make_token("old", expires_at=time.time() - 1))

        assert storage.get_access_token("old") is None

    def test_revoked_access_token_is_gone(self, storage: Any) -> None:
        """Revoking a token removes it."""
        storage.store_access_token(make_token("tok-1"))

        assert storage.revoke_access_token("tok-1") is True
        assert storage.get_access_token("tok-1") is None

    def test_refresh_token_round_trips(self, storage: Any) -> None:
        """A refresh token is stored and retrieved independently of access tokens."""
        storage.store_refresh_token(make_token("ref-1", token_type="refresh"))

        found = storage.get_refresh_token("ref-1")

        assert found is not None
        assert found.token_type == "refresh"

    def test_revoked_refresh_token_is_gone(self, storage: Any) -> None:
        """Revoking a refresh token removes it."""
        storage.store_refresh_token(make_token("ref-1", token_type="refresh"))

        assert storage.revoke_refresh_token("ref-1") is True
        assert storage.get_refresh_token("ref-1") is None


class TestOneTimeUse:
    """Authorization codes and pending state may each be consumed once."""

    def test_auth_code_can_be_consumed_once(self, storage: Any) -> None:
        """The second exchange of the same code gets nothing."""
        storage.store_auth_code(make_auth_code())

        assert storage.get_auth_code("code-1") is not None
        assert storage.get_auth_code("code-1") is None

    def test_expired_auth_code_is_refused(self, storage: Any) -> None:
        """A code past its expiry cannot be exchanged."""
        code = make_auth_code("stale")
        code.expires_at = time.time() - 1
        storage.store_auth_code(code)

        assert storage.get_auth_code("stale") is None

    def test_pending_auth_can_be_consumed_once(self, storage: Any) -> None:
        """Returning from the identity provider twice with one state fails the second time."""
        storage.store_pending_auth(make_pending())

        assert storage.get_pending_auth("state-1") is not None
        assert storage.get_pending_auth("state-1") is None

    def test_pending_auth_preserves_the_callers_state(self, storage: Any) -> None:
        """The client's own state parameter survives the round trip."""
        storage.store_pending_auth(make_pending())

        pending = storage.get_pending_auth("state-1")

        assert pending is not None
        assert pending.claude_state == "claude-state"


class TestStats:
    """Operational visibility into what the store holds."""

    def test_stats_count_what_is_stored(self, storage: Any) -> None:
        """Stats report the number of live entries of each kind."""
        storage.store_client(make_client())
        storage.store_access_token(make_token("tok-1"))

        stats = storage.get_stats()

        assert stats["clients"] == 1
        assert stats["access_tokens"] == 1


class TestDurability:
    """What only the durable backend can promise."""

    def test_tokens_survive_a_restart(self, tmp_path: Path) -> None:
        """A new instance on the same file still recognises an issued token."""
        db_path = tmp_path / "auth.db"
        first = SQLiteAuthStorage(db_path)
        first.store_client(make_client())
        first.store_access_token(make_token("tok-1"))
        first.close()

        second = SQLiteAuthStorage(db_path)

        found = second.get_access_token("tok-1")
        assert found is not None
        assert found.user_subject == "mike@example.com"

    def test_raw_tokens_are_never_written_to_disk(self, tmp_path: Path) -> None:
        """The database holds token hashes, so a stolen file yields no usable tokens."""
        db_path = tmp_path / "auth.db"
        storage = SQLiteAuthStorage(db_path)
        storage.store_access_token(make_token("super-secret-token"))
        storage.store_refresh_token(make_token("super-secret-refresh", token_type="refresh"))
        storage.close()

        contents = db_path.read_bytes()

        assert b"super-secret-token" not in contents
        assert b"super-secret-refresh" not in contents

    def test_expired_entries_are_purged_from_disk(self, tmp_path: Path) -> None:
        """Cleanup removes expired rows rather than letting the file grow forever."""
        storage = SQLiteAuthStorage(tmp_path / "auth.db")
        storage.store_access_token(make_token("old", expires_at=time.time() - 1))
        storage.store_access_token(make_token("current"))

        storage.cleanup()

        assert storage.get_stats()["access_tokens"] == 1
