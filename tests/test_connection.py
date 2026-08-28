"""Tests for the Universe/UniData connection manager."""

import pytest

from tests.mocks.mock_uopy import MockSession, UOError
from u2_mcp.connection import ConnectionManager


class TestConnectionLifecycle:
    """Connecting, reusing, and tearing down sessions."""

    def test_connect_passes_configured_credentials(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Connecting hands uopy exactly what the configuration specified."""
        info = connection_manager.connect()

        assert info.host == "test-host.example.com"
        assert info.account == "TEST"
        assert info.is_active is True

    def test_connect_reuses_an_active_connection(
        self, connection_manager: ConnectionManager
    ) -> None:
        """A second connect under the same name reuses the first session."""
        first = connection_manager.connect("default")
        second = connection_manager.connect("default")

        assert first is second
        assert len(connection_manager.list_connections()) == 1

    def test_disconnect_clears_state(self, connection_manager: ConnectionManager) -> None:
        """Disconnecting drops the session, the file cache, and the connection record."""
        connection_manager.connect()
        connection_manager.open_file("CUSTOMERS")

        assert connection_manager.disconnect() is True
        assert connection_manager.list_connections() == {}

    def test_get_session_reconnects_when_session_went_inactive(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A dead session is replaced rather than handed back to the caller."""
        connection_manager.connect()
        mock_uopy.is_active = False

        session = connection_manager.get_session()

        assert session.is_active is True


class TestFileHandles:
    """Opening and caching Universe file handles."""

    def test_open_file_caches_the_handle(self, connection_manager: ConnectionManager) -> None:
        """Opening the same file twice returns the same cached handle."""
        first = connection_manager.open_file("CUSTOMERS")
        second = connection_manager.open_file("CUSTOMERS")

        assert first is second

    def test_open_missing_file_raises(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A file the server does not have surfaces as a FileNotFoundError."""
        mock_uopy.set_missing_file("NOPE")

        with pytest.raises(Exception) as exc_info:
            connection_manager.open_file("NOPE")

        assert "NOPE" in str(exc_info.value)


class TestCommandExecution:
    """Running TCL commands through the manager."""

    def test_execute_command_returns_server_output(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """The command's response comes back to the caller."""
        mock_uopy.set_command_responses({"WHO": "1 test-user TEST"})

        assert "test-user" in connection_manager.execute_command("WHO")

    def test_execute_command_propagates_server_errors(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A server-side failure is raised, not swallowed into an empty string."""
        mock_uopy.command_error = UOError("Command failed")

        with pytest.raises(UOError):
            connection_manager.execute_command("LIST CUSTOMERS")


class TestOutputSanitization:
    """How raw Universe output is cleaned for display.

    The MultiValue delimiters must become readable, but real business data --
    accented names, currency symbols -- has to survive the trip intact.
    """

    def test_multivalue_delimiters_become_readable(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Attribute, value, and subvalue marks are replaced with visible separators."""
        raw = "ONE" + chr(254) + "TWO" + chr(253) + "THREE" + chr(252) + "FOUR"

        cleaned = connection_manager._sanitize_output(raw)

        assert chr(254) not in cleaned
        assert chr(253) not in cleaned
        assert chr(252) not in cleaned
        assert "ONE" in cleaned and "FOUR" in cleaned

    def test_carriage_returns_and_form_feeds_normalize_to_newlines(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Terminal line endings and page breaks become plain newlines."""
        cleaned = connection_manager._sanitize_output("A\r\nB\rC\fD")

        assert "\r" not in cleaned
        assert "\f" not in cleaned
        assert cleaned.count("\n") == 3

    def test_accented_customer_names_survive(self, connection_manager: ConnectionManager) -> None:
        """Non-ASCII letters in real customer data are preserved, not stripped."""
        cleaned = connection_manager._sanitize_output("MÜLLER GmbH" + chr(254) + "José Peña")

        assert "MÜLLER GmbH" in cleaned
        assert "José Peña" in cleaned

    def test_currency_symbols_survive(self, connection_manager: ConnectionManager) -> None:
        """Currency symbols in amounts are preserved, not silently deleted."""
        cleaned = connection_manager._sanitize_output("Total: £1,250.00 / €1,430.55 / ¥98,000")

        assert "£1,250.00" in cleaned
        assert "€1,430.55" in cleaned
        assert "¥98,000" in cleaned

    def test_control_characters_are_still_removed(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Terminal control codes are dropped so they cannot corrupt the display."""
        cleaned = connection_manager._sanitize_output("A\x00B\x07C\x1b[31mD")

        assert "\x00" not in cleaned
        assert "\x07" not in cleaned
        assert "\x1b" not in cleaned
        assert "ABC" in cleaned.replace("[31m", "")


class TestTransactions:
    """Transaction state tracking."""

    def test_commit_cycle_updates_state(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Begin then commit leaves no transaction open and logs both steps."""
        connection_manager.begin_transaction()
        assert connection_manager.in_transaction is True

        connection_manager.commit_transaction()
        assert connection_manager.in_transaction is False
        assert mock_uopy.transaction_log == ["start", "commit"]

    def test_rollback_cycle_updates_state(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Begin then rollback leaves no transaction open and logs both steps."""
        connection_manager.begin_transaction()
        connection_manager.rollback_transaction()

        assert connection_manager.in_transaction is False
        assert mock_uopy.transaction_log == ["start", "rollback"]

    def test_nested_begin_is_rejected(self, connection_manager: ConnectionManager) -> None:
        """Starting a second transaction raises rather than silently nesting."""
        connection_manager.begin_transaction()

        with pytest.raises(RuntimeError):
            connection_manager.begin_transaction()

    def test_commit_without_transaction_is_rejected(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Committing with nothing open raises rather than pretending to succeed."""
        with pytest.raises(RuntimeError):
            connection_manager.commit_transaction()
