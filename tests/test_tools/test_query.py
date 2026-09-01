"""Tests for the query execution tools.

These exercise the tool functions the AI client actually calls, through the real
connection manager and against the mock uopy layer.
"""

from tests.mocks.mock_uopy import MockSession
from u2_mcp.connection import ConnectionManager
from u2_mcp.tools.query import execute_query, execute_tcl, get_select_list, validate_query


class TestQuerySafety:
    """Only read-only query verbs may reach the database."""

    def test_allowed_verb_runs(self, connection_manager: ConnectionManager) -> None:
        """A COUNT query is permitted and reaches the server."""
        result = execute_query('COUNT CUSTOMERS WITH STATE = "CA"')

        assert result["status"] == "success"
        assert "error" not in result

    def test_write_verb_is_refused(self, connection_manager: ConnectionManager) -> None:
        """A DELETE statement is refused before it reaches the database."""
        result = execute_query("DELETE CUSTOMERS AR1042")

        assert "error" in result

    def test_refused_query_never_reaches_the_server(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A refused query executes nothing at all."""
        execute_query("DELETE CUSTOMERS AR1042")

        assert mock_uopy.executed_commands == []

    def test_blocked_tcl_command_is_refused(self, connection_manager: ConnectionManager) -> None:
        """A destructive TCL command is blocked by the validator."""
        result = execute_tcl("DELETE.FILE CUSTOMERS")

        assert "error" in result

    def test_blocked_tcl_command_never_reaches_the_server(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A blocked TCL command executes nothing at all."""
        execute_tcl("CLEAR.FILE CUSTOMERS")

        assert mock_uopy.executed_commands == []


class TestResultCompleteness:
    """A partial answer must never be presented as a complete one."""

    def test_sampled_query_is_marked_incomplete(
        self, connection_manager: ConnectionManager
    ) -> None:
        """A LIST that had a row limit injected reports that it may be partial."""
        result = execute_query("LIST CUSTOMERS", max_rows=10)

        assert result["is_complete"] is False

    def test_sampled_query_explains_the_limit_in_words(
        self, connection_manager: ConnectionManager
    ) -> None:
        """The response carries a warning an operator can read."""
        result = execute_query("LIST CUSTOMERS", max_rows=10)

        assert "10" in result["warning"]

    def test_count_query_is_not_marked_incomplete(
        self, connection_manager: ConnectionManager
    ) -> None:
        """A COUNT returns a single figure, so nothing was truncated."""
        result = execute_query('COUNT CUSTOMERS WITH STATE = "CA"')

        assert result["is_complete"] is True

    def test_caller_supplied_sample_is_respected(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """An explicit SAMPLE clause is not overridden with a second one."""
        execute_query("LIST CUSTOMERS SAMPLE 5")

        assert mock_uopy.executed_commands[0].upper().count("SAMPLE") == 1

    def test_select_list_reports_truncation(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A select list cut short at the cap says so, and says it in words."""
        mock_uopy.set_select_list([f"ID{n:03d}" for n in range(50)])

        result = get_select_list("SELECT CUSTOMERS", max_ids=10)

        assert result["truncated"] is True
        assert result["is_complete"] is False
        assert len(result["record_ids"]) == 10

    def test_complete_select_list_is_marked_complete(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A select list that fit under the cap is reported as complete."""
        mock_uopy.set_select_list(["ID001", "ID002"])

        result = get_select_list("SELECT CUSTOMERS", max_ids=10)

        assert result["truncated"] is False
        assert result["is_complete"] is True


class TestQueryValidation:
    """Dry-running a query before it executes."""

    def test_valid_query_passes(self, connection_manager: ConnectionManager) -> None:
        """A well-formed LIST validates cleanly."""
        result = validate_query('LIST CUSTOMERS NAME WITH STATE = "CA"')

        assert result["valid"] is True

    def test_validation_executes_nothing(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Validating a query does not run it."""
        validate_query('LIST CUSTOMERS WITH STATE = "CA"')

        assert mock_uopy.executed_commands == []

    def test_disallowed_verb_fails_validation(self, connection_manager: ConnectionManager) -> None:
        """A write verb is rejected by validation with an explanation."""
        result = validate_query("DELETE CUSTOMERS")

        assert result["valid"] is False
        assert result["error"]
