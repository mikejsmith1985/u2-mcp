"""Adversarial tests for the durable auth store.

Making OAuth state durable replaced dictionary operations that were atomic under
Python's own lock with a read followed by a separate delete. That is exactly the
shape a one-time-use guarantee breaks on, so it is tested directly: an
authorization code must be exchangeable once even when two requests race for it.
"""

import os
import threading
import time
from pathlib import Path

import pytest

from tests.test_auth.test_storage import make_auth_code, make_pending
from u2_mcp.auth.sqlite_storage import SQLiteAuthStorage
from u2_mcp.auth.storage import InMemoryAuthStorage

ATTEMPTS = 40


def race_for_code(storage: object, attempts: int) -> int:
    """Have several threads try to exchange one authorization code at once.

    Args:
        storage: The auth store under test
        attempts: How many concurrent exchanges to attempt

    Returns:
        How many threads successfully received the code
    """
    barrier = threading.Barrier(attempts)
    successes: list[object] = []
    lock = threading.Lock()

    def exchange() -> None:
        barrier.wait()
        result = storage.get_auth_code("code-1")  # type: ignore[attr-defined]
        if result is not None:
            with lock:
                successes.append(result)

    threads = [threading.Thread(target=exchange) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return len(successes)


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    """Provide each backend, so both are held to the same guarantee."""
    if request.param == "memory":
        return InMemoryAuthStorage()
    return SQLiteAuthStorage(tmp_path / "auth.db")


class TestOneTimeUseUnderRace:
    """A one-time-use secret must be usable once, not once per lucky thread."""

    def test_an_authorization_code_is_exchanged_exactly_once(self, storage: object) -> None:
        """Concurrent exchanges of one code yield exactly one success."""
        storage.store_auth_code(make_auth_code("code-1"))  # type: ignore[attr-defined]

        assert race_for_code(storage, ATTEMPTS) == 1

    def test_a_code_cannot_be_replayed_while_another_thread_is_mid_exchange(
        self, storage: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window between reading a code and consuming it must not be exploitable.

        Timing alone does not prove this: the window is small enough that a race
        usually loses. Widening it deliberately makes the guarantee testable, so a
        pass here means the code is consumed atomically rather than luckily.
        """
        storage.store_auth_code(make_auth_code("code-1"))  # type: ignore[attr-defined]

        original_read = storage._read_one if hasattr(storage, "_read_one") else None
        if original_read is not None:

            def slow_read(sql: str, parameters: tuple = ()) -> object:
                result = original_read(sql, parameters)
                time.sleep(0.05)  # hold the window open for the racing threads
                return result

            monkeypatch.setattr(storage, "_read_one", slow_read)

        assert race_for_code(storage, ATTEMPTS) == 1

    def test_pending_authorization_is_consumed_exactly_once(self, storage: object) -> None:
        """Concurrent callbacks carrying one state value yield exactly one success."""
        storage.store_pending_auth(make_pending("state-1"))  # type: ignore[attr-defined]

        barrier = threading.Barrier(ATTEMPTS)
        successes: list[object] = []
        lock = threading.Lock()

        def callback() -> None:
            barrier.wait()
            result = storage.get_pending_auth("state-1")  # type: ignore[attr-defined]
            if result is not None:
                with lock:
                    successes.append(result)

        threads = [threading.Thread(target=callback) for _ in range(ATTEMPTS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(successes) == 1


class TestSecretsAtRest:
    """The database file holds credentials, so it must not be readable by others."""

    @pytest.mark.skipif(
        os.name == "nt", reason="Windows ignores POSIX permission bits; ACLs govern access there"
    )
    def test_the_database_file_is_not_group_or_world_readable(self, tmp_path: Path) -> None:
        """Only the owning account may read the OAuth state file."""
        db_path = tmp_path / "auth.db"
        SQLiteAuthStorage(db_path)

        mode = db_path.stat().st_mode & 0o077

        assert mode == 0, f"auth.db is accessible to other users (mode bits {oct(mode)})"
