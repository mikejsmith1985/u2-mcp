"""Connection failures at the worst possible moment.

Databases drop connections mid-transaction, watchdogs fire while a query is
running, and a burst of traffic arrives the instant a session dies. These tests
put the connection layer in those positions and insist that it fails loudly and
leaks nothing.
"""

import threading
import time

import pytest

from tests.mocks.mock_uopy import MockSession, UOError
from u2_mcp.config import U2Config
from u2_mcp.connection import ConnectionManager
from u2_mcp.credentials import U2Credentials


class TestFailureDuringTransaction:
    """A transaction interrupted by a failure must never look like a success."""

    def test_a_timeout_during_a_transaction_ends_it(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Abandoning a query closes the session, so the transaction is gone with it."""
        connection_manager.begin_transaction()
        mock_uopy.command_delay_seconds = 0.2

        with pytest.raises(TimeoutError):
            connection_manager.execute_command("LIST HUGE", timeout=0.05)

        assert connection_manager.in_transaction is False

    def test_committing_after_that_fails_loudly(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A caller must never believe uncommitted work was committed."""
        connection_manager.begin_transaction()
        mock_uopy.command_delay_seconds = 0.2
        with pytest.raises(TimeoutError):
            connection_manager.execute_command("LIST HUGE", timeout=0.05)

        mock_uopy.command_delay_seconds = 0.0
        with pytest.raises(RuntimeError):
            connection_manager.commit_transaction()

    def test_a_dropped_session_clears_transaction_state(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Reconnecting after a drop does not carry a phantom transaction across."""
        connection_manager.begin_transaction()
        mock_uopy.is_active = False

        connection_manager.get_session()

        assert connection_manager.in_transaction is False

    def test_a_failed_commit_is_raised_not_swallowed(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """If the server refuses a commit, the caller hears about it."""
        connection_manager.begin_transaction()

        def failing_commit() -> None:
            raise UOError("Commit rejected")

        mock_uopy.tx_commit = failing_commit  # type: ignore[method-assign]

        with pytest.raises(UOError):
            connection_manager.commit_transaction()


class TestWatchdogDuringActivity:
    """The watchdog fires on its own schedule, including mid-query."""

    def test_a_forced_disconnect_during_a_query_does_not_corrupt_state(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Tearing the connection down under a running command leaves it recoverable."""
        connection_manager.connect()
        mock_uopy.command_delay_seconds = 0.1

        def force_disconnect_soon() -> None:
            connection_manager.force_disconnect()

        watchdog = threading.Timer(0.02, force_disconnect_soon)
        watchdog.start()
        try:
            connection_manager.execute_command("LIST BIG", timeout=5)
        except Exception:  # noqa: BLE001 - either outcome is acceptable; state is the point
            pass
        finally:
            watchdog.join()

        mock_uopy.command_delay_seconds = 0.0
        assert "test-user" in connection_manager.execute_command("WHO")

    def test_repeated_forced_disconnects_are_harmless(
        self, connection_manager: ConnectionManager
    ) -> None:
        """A watchdog that fires several times must not raise or wedge the manager."""
        connection_manager.connect()

        for _ in range(5):
            connection_manager.force_disconnect()

        assert connection_manager.get_session() is not None


class TestReconnectStorm:
    """A burst of traffic arriving as the session dies must open one session."""

    def test_concurrent_callers_do_not_open_duplicate_sessions(
        self, mock_config: U2Config, mock_uopy: MockSession
    ) -> None:
        """Twenty threads finding no session must not each create one.

        Duplicate sessions leak database connections and are invisible until the
        server runs out of them, so the count is asserted rather than the outcome.
        """
        connect_count = 0
        counter_lock = threading.Lock()
        original_connect = ConnectionManager.connect

        def counting_connect(self: ConnectionManager, name: str = "default") -> object:
            nonlocal connect_count
            # Opening a real session takes time. Holding the window open makes the
            # race deterministic rather than leaving it to the scheduler, which
            # would let a broken implementation pass by luck.
            time.sleep(0.05)
            result = original_connect(self, name)
            with counter_lock:
                connect_count += 1
            return result

        manager = ConnectionManager(mock_config)
        manager.connect = counting_connect.__get__(manager)  # type: ignore[method-assign]

        barrier = threading.Barrier(20)

        def call() -> None:
            barrier.wait()
            manager.get_session()

        threads = [threading.Thread(target=call) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert connect_count == 1, f"opened {connect_count} sessions for one connection slot"

    def test_concurrent_file_opens_share_one_handle(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Opening the same file from many threads yields one cached handle."""
        barrier = threading.Barrier(20)
        handles: list[object] = []
        handles_lock = threading.Lock()

        def open_it() -> None:
            barrier.wait()
            handle = connection_manager.open_file("CUSTOMERS")
            with handles_lock:
                handles.append(handle)

        threads = [threading.Thread(target=open_it) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len({id(handle) for handle in handles}) == 1


class TestCredentialsAreNeverLogged:
    """A connection failure must not put a password in the log or the error."""

    def test_a_connection_error_does_not_contain_the_password(
        self, mock_config: U2Config, mock_uopy: MockSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exception raised on failure names the host, never the credentials."""
        import uopy

        def failing_connect(**kwargs: object) -> object:
            raise UOError("Connection refused")

        monkeypatch.setattr(uopy, "connect", failing_connect)
        credentials = U2Credentials(
            user="ALICE", password="super-secret-pw", account="SALES", is_shared=False
        )
        manager = ConnectionManager(mock_config, credentials)

        with pytest.raises(Exception) as exc_info:
            manager.connect()

        assert "super-secret-pw" not in str(exc_info.value)

    def test_the_connection_log_does_not_contain_the_password(
        self, mock_config: U2Config, mock_uopy: MockSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logging which account connected must not log how it authenticated."""
        credentials = U2Credentials(
            user="ALICE", password="super-secret-pw", account="SALES", is_shared=False
        )
        manager = ConnectionManager(mock_config, credentials)

        with caplog.at_level("DEBUG"):
            manager.connect()

        assert "super-secret-pw" not in caplog.text
