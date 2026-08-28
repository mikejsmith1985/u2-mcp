"""Adversarial input against the safety controls.

The command validator is the only thing standing between a model-generated
string and a production database. These tests treat it as a security boundary
and try to get past it: shell escapes, account switching, compiling code,
operating-system file access, and the SQL verbs that would drop a table.
"""

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.connection import ConnectionManager
from u2_mcp.tools.knowledge import get_knowledge_topic, list_knowledge, save_knowledge
from u2_mcp.tools.query import execute_query, execute_tcl
from u2_mcp.utils.safety import CommandValidator

# Verbs that reach the operating system, another account, or the compiler.
# Any one of these turns database access into control of the host.
ESCAPE_COMMANDS = [
    "SH",
    "SH -c 'id'",
    "!ls -la",
    "!",
    "DOS dir",
    "OSDELETE /etc/passwd",
    "OSWRITE X ON '/tmp/owned'",
    "OSREAD X FROM '/etc/shadow'",
    "OSCOPY FROM '/etc/passwd' TO 'HERE'",
    "RUN BP EVIL.PROGRAM",
    "BASIC BP EVIL.PROGRAM",
    "CATALOG BP EVIL.PROGRAM",
    "LOGTO PAYROLL",
    "LOGIN OTHERACCOUNT",
    "PHANTOM RUN BP EVIL",
    "SETPTR 0,132,60,1,3",
    "CLEAR.LOCKS",
    "SUPERCLEAR",
]

# SQL verbs that Universe also accepts, and that destroy or alter data.
SQL_COMMANDS = [
    "DROP TABLE CUSTOMERS",
    "ALTER TABLE CUSTOMERS ADD COLUMN X",
    "GRANT DBA TO PUBLIC",
    "REVOKE SELECT ON CUSTOMERS FROM ALICE",
    "INSERT INTO CUSTOMERS VALUES ('X')",
    "UPDATE CUSTOMERS SET NAME = 'X'",
    "CREATE TABLE EVIL (X INT)",
]


class TestEscapeCommandsAreBlocked:
    """No TCL command may reach the operating system or another account."""

    @pytest.mark.parametrize("command", ESCAPE_COMMANDS)
    def test_the_validator_refuses_it(self, mock_config: object, command: str) -> None:
        """Each escape verb is rejected by the validator itself."""
        validator = CommandValidator([], read_only=False)

        is_valid, message = validator.validate(command)

        assert is_valid is False, f"{command!r} was allowed"
        assert message

    @pytest.mark.parametrize("command", ESCAPE_COMMANDS)
    def test_it_never_reaches_the_database(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession, command: str
    ) -> None:
        """The tool refuses before anything is sent to the server."""
        result = execute_tcl(command)

        assert "error" in result, f"{command!r} was executed"
        assert mock_uopy.executed_commands == []


class TestSqlVerbsAreBlocked:
    """Universe accepts SQL too, and those verbs destroy data."""

    @pytest.mark.parametrize("command", SQL_COMMANDS)
    def test_destructive_sql_is_refused(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession, command: str
    ) -> None:
        """A SQL verb that writes or alters schema is blocked."""
        result = execute_tcl(command)

        assert "error" in result, f"{command!r} was executed"
        assert mock_uopy.executed_commands == []


class TestCaseAndWhitespaceEvasion:
    """A blocklist that can be evaded by formatting is not a blocklist."""

    @pytest.mark.parametrize(
        "command",
        [
            "sh -c id",
            "  SH -c id",
            "\tSH -c id",
            "Sh -C id",
            "  !ls",
            "\t!ls",
        ],
    )
    def test_formatting_does_not_get_past_the_validator(
        self, mock_config: object, command: str
    ) -> None:
        """Case and leading whitespace do not change the verdict."""
        validator = CommandValidator([], read_only=False)

        is_valid, _ = validator.validate(command)

        assert is_valid is False, f"{command!r} was allowed"


class TestQueryAllowlistHolds:
    """execute_query allows only read verbs, whatever follows them."""

    @pytest.mark.parametrize(
        "query",
        ["SH -c id", "DELETE CUSTOMERS AR1042", "DROP TABLE CUSTOMERS", "RUN BP EVIL"],
    )
    def test_a_non_read_verb_is_refused(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession, query: str
    ) -> None:
        """Only the allowlisted read verbs are accepted."""
        result = execute_query(query)

        assert "error" in result
        assert mock_uopy.executed_commands == []


class TestRowLimitCannotBeEvaded:
    """The row cap must not be defeated by a file or field that merely mentions it."""

    def test_a_file_whose_name_contains_sample_is_still_capped(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A file called SAMPLES must not suppress the row limit."""
        execute_query("LIST SAMPLES")

        assert "SAMPLE 1000" in mock_uopy.executed_commands[0].upper()

    def test_a_field_whose_name_contains_sample_is_still_capped(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A field called SAMPLE.DATE must not suppress the row limit."""
        execute_query("LIST LABTESTS SAMPLE.DATE")

        assert mock_uopy.executed_commands[0].upper().rstrip().endswith("SAMPLE 1000")

    def test_a_query_that_evaded_the_cap_is_not_reported_as_complete(
        self, connection_manager: ConnectionManager
    ) -> None:
        """If a limit was applied, the answer is still marked possibly partial."""
        result = execute_query("LIST SAMPLES")

        assert result["is_complete"] is False


class TestKnowledgeFileInjection:
    """A saved topic must not be able to rewrite the knowledge file's structure."""

    @pytest.fixture(autouse=True)
    def knowledge_file(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point the knowledge store at a temporary file."""
        import u2_mcp.utils.knowledge as knowledge_module

        monkeypatch.setenv("U2_KNOWLEDGE_PATH", str(tmp_path / "knowledge.md"))
        knowledge_module._knowledge_store = None
        yield
        knowledge_module._knowledge_store = None

    def test_content_cannot_forge_another_topic(self) -> None:
        """Markdown headers inside content must not become separate topics."""
        save_knowledge("Real topic", "Some notes.\n\n## Forged topic\n\nInjected content.")

        topics = [entry["topic"] for entry in list_knowledge()["topics"]]

        assert "Forged topic" not in topics

    def test_a_topic_name_cannot_forge_another_topic(self) -> None:
        """Newlines in a topic name must not create extra sections."""
        save_knowledge("Innocent\n\n## Forged topic", "Notes.")

        topics = [entry["topic"] for entry in list_knowledge()["topics"]]

        assert "Forged topic" not in topics

    def test_injected_content_cannot_overwrite_a_real_topic(self) -> None:
        """Writing one topic must not damage another."""
        save_knowledge("AR-CUST file", "Customer master.")
        save_knowledge("Notes", "Text.\n\n## AR-CUST file\n\nWrong information.")

        assert "Customer master." in get_knowledge_topic("AR-CUST file")["content"]
