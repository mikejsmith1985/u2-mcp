"""Edge cases in the token lifecycle.

Tokens expire mid-session, get revoked while in use, arrive from clients that no
longer exist, and are refreshed by two tabs at once. Each of those must fail in
the direction that denies access rather than grants it.
"""

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_auth.test_storage import make_client, make_token
from u2_mcp.auth.sqlite_storage import SQLiteAuthStorage, hash_secret
from u2_mcp.auth.storage import InMemoryAuthStorage


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """Provide each backend, so both are held to the same guarantees."""
    if request.param == "memory":
        return InMemoryAuthStorage()
    return SQLiteAuthStorage(tmp_path / "auth.db")


class TestExpiry:
    """An expired token is no token at all."""

    def test_a_token_expiring_now_is_refused(self, storage: Any) -> None:
        """A token whose expiry has just passed is not accepted."""
        storage.store_access_token(make_token("edge", expires_at=time.time() - 0.001))

        assert storage.get_access_token("edge") is None

    def test_a_token_from_a_clock_ahead_of_ours_still_works(self, storage: Any) -> None:
        """Skew that makes a token look longer-lived does not break acceptance."""
        storage.store_access_token(make_token("future", expires_at=time.time() + 86400))

        assert storage.get_access_token("future") is not None

    def test_a_token_issued_by_a_clock_behind_ours_is_refused(self, storage: Any) -> None:
        """A token that already expired by our clock is refused, not honoured."""
        storage.store_access_token(make_token("stale", expires_at=time.time() - 3600))

        assert storage.get_access_token("stale") is None

    def test_a_token_with_no_expiry_is_accepted(self, storage: Any) -> None:
        """A token without an expiry is treated as long-lived rather than invalid."""
        storage.store_access_token(make_token("forever", expires_at=None))

        assert storage.get_access_token("forever") is not None

    def test_an_expired_token_is_purged_rather_than_kept(self, storage: Any) -> None:
        """Reading an expired token removes it instead of leaving it to accumulate."""
        storage.store_access_token(make_token("stale", expires_at=time.time() - 1))
        storage.get_access_token("stale")

        assert storage.get_stats()["access_tokens"] == 0


class TestRevocation:
    """Revoking access must take effect immediately."""

    def test_a_revoked_token_stops_working(self, storage: Any) -> None:
        """The token is refused the moment it is revoked."""
        storage.store_access_token(make_token("live"))
        storage.revoke_access_token("live")

        assert storage.get_access_token("live") is None

    def test_revoking_one_token_leaves_the_others(self, storage: Any) -> None:
        """Revocation is precise, not a blanket sign-out."""
        storage.store_access_token(make_token("first"))
        storage.store_access_token(make_token("second"))

        storage.revoke_access_token("first")

        assert storage.get_access_token("second") is not None

    def test_revoking_an_unknown_token_is_not_an_error(self, storage: Any) -> None:
        """Revoking something already gone reports False rather than raising."""
        assert storage.revoke_access_token("never-existed") is False

    def test_deleting_a_client_revokes_its_refresh_tokens_too(self, storage: Any) -> None:
        """Removing a client must not leave a way back in."""
        storage.store_client(make_client())
        storage.store_refresh_token(make_token("ref", token_type="refresh"))

        storage.delete_client("client-1")

        assert storage.get_refresh_token("ref") is None


class TestTokenTypeConfusion:
    """A refresh token must not be usable as an access token, or vice versa."""

    def test_a_refresh_token_is_not_an_access_token(self, storage: Any) -> None:
        """Presenting a refresh token as a bearer token gets nothing."""
        storage.store_refresh_token(make_token("ref-1", token_type="refresh"))

        assert storage.get_access_token("ref-1") is None

    def test_an_access_token_is_not_a_refresh_token(self, storage: Any) -> None:
        """An access token cannot be exchanged for a new one."""
        storage.store_access_token(make_token("acc-1"))

        assert storage.get_refresh_token("acc-1") is None


class TestConcurrentRefresh:
    """Two tabs refreshing at once must not both consume the same token."""

    def test_a_revoked_refresh_token_is_claimed_once(self, storage: Any) -> None:
        """Only one of many concurrent revocations reports success.

        Refresh rotation revokes the old token as it issues a new one, so if two
        callers can both revoke successfully, both believe they rotated it and one
        rotation is lost.
        """
        storage.store_refresh_token(make_token("ref-1", token_type="refresh"))

        barrier = threading.Barrier(20)
        successes: list[bool] = []
        lock = threading.Lock()

        def revoke() -> None:
            barrier.wait()
            if storage.revoke_refresh_token("ref-1"):
                with lock:
                    successes.append(True)

        threads = [threading.Thread(target=revoke) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(successes) == 1


class TestUnknownAndMalformedTokens:
    """Anything unrecognised must be refused without leaking why."""

    @pytest.mark.parametrize(
        "token", ["", " ", "not-a-token", "../../etc/passwd", "\x00", "a" * 10000]
    )
    def test_an_unrecognised_token_is_refused(self, storage: Any, token: str) -> None:
        """No malformed token is ever accepted."""
        assert storage.get_access_token(token) is None

    def test_a_hash_of_a_token_is_not_itself_a_token(self, tmp_path: Path) -> None:
        """Knowing the stored hash must not be enough to authenticate.

        The database stores hashes, so someone who reads the file must not be able
        to present a hash as though it were the token it stands for.
        """
        storage = SQLiteAuthStorage(tmp_path / "auth.db")
        storage.store_access_token(make_token("the-real-token"))

        assert storage.get_access_token(hash_secret("the-real-token")) is None
