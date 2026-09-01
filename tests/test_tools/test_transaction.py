"""Tests for transaction management.

A transaction left open holds locks on a live production file, so the tools that
open one have to be exact about state: what is open, what closed it, and what
happens when a caller asks for something that makes no sense.
"""

from collections.abc import Iterator

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.config import U2Config
from u2_mcp.connection import ConnectionManager
from u2_mcp.tools.transaction import (
    begin_transaction,
    commit_transaction,
    get_transaction_status,
    rollback_transaction,
)


class TestTransactionLifecycle:
    """Opening and closing a unit of work."""

    def test_beginning_a_transaction_opens_one(self, connection_manager: ConnectionManager) -> None:
        """Starting a transaction reports it as open."""
        result = begin_transaction()

        assert result["status"] == "success"
        assert result["in_transaction"] is True

    def test_committing_closes_it(self, connection_manager: ConnectionManager) -> None:
        """Committing leaves no transaction open."""
        begin_transaction()

        result = commit_transaction()

        assert result["in_transaction"] is False

    def test_committing_reaches_the_database(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """The commit is issued to the server, not merely tracked locally."""
        begin_transaction()
        commit_transaction()

        assert mock_uopy.transaction_log == ["start", "commit"]

    def test_rolling_back_closes_it(self, connection_manager: ConnectionManager) -> None:
        """Rolling back leaves no transaction open."""
        begin_transaction()

        result = rollback_transaction()

        assert result["in_transaction"] is False

    def test_rolling_back_reaches_the_database(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """The rollback is issued to the server."""
        begin_transaction()
        rollback_transaction()

        assert mock_uopy.transaction_log == ["start", "rollback"]


class TestInvalidRequests:
    """Asking for something that makes no sense must not corrupt the state."""

    def test_a_second_begin_is_refused(self, connection_manager: ConnectionManager) -> None:
        """Nested transactions are refused rather than silently ignored."""
        begin_transaction()

        result = begin_transaction()

        assert "error" in result

    def test_the_first_transaction_survives_a_refused_second(
        self, connection_manager: ConnectionManager
    ) -> None:
        """A refused nested begin leaves the open transaction intact."""
        begin_transaction()
        begin_transaction()

        assert get_transaction_status()["in_transaction"] is True

    def test_committing_nothing_is_refused(self, connection_manager: ConnectionManager) -> None:
        """A commit with no transaction open reports an error."""
        result = commit_transaction()

        assert "error" in result

    def test_rolling_back_nothing_is_refused(self, connection_manager: ConnectionManager) -> None:
        """A rollback with no transaction open reports an error."""
        result = rollback_transaction()

        assert "error" in result


class TestStatus:
    """Reporting what is currently open."""

    def test_status_reports_no_transaction_initially(
        self, connection_manager: ConnectionManager
    ) -> None:
        """A fresh connection has nothing open."""
        assert get_transaction_status()["in_transaction"] is False

    def test_status_reports_an_open_transaction(
        self, connection_manager: ConnectionManager
    ) -> None:
        """An open transaction is visible in the status."""
        begin_transaction()

        assert get_transaction_status()["in_transaction"] is True

    def test_status_reports_read_only_mode(self, connection_manager: ConnectionManager) -> None:
        """The status says whether the server can write at all."""
        assert get_transaction_status()["read_only_mode"] is False


class TestReadOnlyMode:
    """No transaction should open on a server that cannot write."""

    @pytest.fixture
    def read_only_manager(
        self, mock_config_read_only: U2Config, mock_uopy: MockSession
    ) -> Iterator[ConnectionManager]:
        """Install a read-only connection manager as the server's global."""
        import u2_mcp.server as server_module

        manager = ConnectionManager(mock_config_read_only)
        server_module._connection_manager = manager
        try:
            yield manager
        finally:
            server_module._connection_manager = None

    def test_beginning_a_transaction_is_refused(self, read_only_manager: ConnectionManager) -> None:
        """Read-only mode refuses to open a transaction."""
        result = begin_transaction()

        assert "error" in result

    def test_nothing_reaches_the_database(
        self, read_only_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """The refusal happens before the server is asked to do anything."""
        begin_transaction()

        assert mock_uopy.transaction_log == []
