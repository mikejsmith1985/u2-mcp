"""Tests for the audit trail.

An audit record that cannot name who acted is a debugging aid, not an audit
trail. These tests hold it to naming the person, the database login their request
used, and whether that login was shared with anyone else.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from u2_mcp.credentials import U2Credentials
from u2_mcp.identity import CallerIdentity, use_caller
from u2_mcp.utils.audit import AuditLogger

ALICE = CallerIdentity(
    subject="auth0|653f",
    email="alice@example.com",
    name="Alice Nguyen",
    client_id="claude-web",
)

ALICE_LOGIN = U2Credentials(user="ALICE", password="pw", account="SALES", is_shared=False)
SHARED_LOGIN = U2Credentials(user="u2svc", password="pw", account="PROD", is_shared=True)


@pytest.fixture
def audit_logger(tmp_path: Path) -> AuditLogger:
    """Provide an audit logger writing to a temporary directory."""
    logger = AuditLogger(audit_path=str(tmp_path), include_results=True)
    logger.start_session("test-session")
    return logger


def read_entries(path: Path) -> list[dict[str, Any]]:
    """Read every audit entry written to the directory."""
    entries: list[dict[str, Any]] = []
    for log_file in sorted(path.glob("*.jsonl")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def tool_calls(path: Path) -> list[dict[str, Any]]:
    """Return only the tool-call entries."""
    return [entry for entry in read_entries(path) if entry.get("event") == "tool_call"]


class TestAttribution:
    """Every record must be traceable to a person."""

    def test_record_names_the_authenticated_user(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """The subject from the identity provider appears on the record."""
        with use_caller(ALICE):
            audit_logger.log_tool_call("read_record", {"file_name": "CUSTOMERS"})

        assert tool_calls(tmp_path)[0]["user"] == "auth0|653f"

    def test_record_carries_a_readable_name_and_email(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """A reviewer sees a person, not only an opaque identifier."""
        with use_caller(ALICE):
            audit_logger.log_tool_call("read_record", {"file_name": "CUSTOMERS"})

        entry = tool_calls(tmp_path)[0]
        assert entry["user_name"] == "Alice Nguyen"
        assert entry["user_email"] == "alice@example.com"

    def test_record_names_the_client_that_presented_the_token(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """Which application acted on the person's behalf is recorded."""
        with use_caller(ALICE):
            audit_logger.log_tool_call("read_record", {"file_name": "CUSTOMERS"})

        assert tool_calls(tmp_path)[0]["client_id"] == "claude-web"

    def test_unauthenticated_use_is_recorded_as_such(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """Local stdio use is marked unauthenticated rather than left ambiguous."""
        audit_logger.log_tool_call("read_record", {"file_name": "CUSTOMERS"})

        entry = tool_calls(tmp_path)[0]
        assert entry["user"] == "local"
        assert entry["authenticated"] is False


class TestDatabaseLoginRecording:
    """The record must also say which database login carried out the work."""

    def test_the_login_used_is_recorded(self, audit_logger: AuditLogger, tmp_path: Path) -> None:
        """A reviewer can tie the MCP request to the Universe session that ran it."""
        with use_caller(ALICE):
            audit_logger.log_tool_call(
                "execute_query", {"query": "COUNT CUSTOMERS"}, credentials=ALICE_LOGIN
            )

        entry = tool_calls(tmp_path)[0]
        assert entry["db_login"] == "ALICE@SALES"
        assert entry["db_login_is_shared"] is False

    def test_a_shared_login_is_flagged_on_every_record(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """When the database could not tell callers apart, the record says so."""
        with use_caller(ALICE):
            audit_logger.log_tool_call(
                "execute_query", {"query": "COUNT CUSTOMERS"}, credentials=SHARED_LOGIN
            )

        assert tool_calls(tmp_path)[0]["db_login_is_shared"] is True


class TestSecrets:
    """Credentials must never reach the audit file."""

    def test_passwords_in_parameters_are_redacted(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """A parameter that looks like a secret is masked."""
        audit_logger.log_tool_call("connect", {"user": "ALICE", "password": "hunter2"})

        written = "".join(f.read_text(encoding="utf-8") for f in tmp_path.glob("*.jsonl"))
        assert "hunter2" not in written

    def test_the_database_password_is_never_recorded(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """Recording which login ran a query must not record its password."""
        with use_caller(ALICE):
            audit_logger.log_tool_call("execute_query", {"query": "X"}, credentials=ALICE_LOGIN)

        written = "".join(f.read_text(encoding="utf-8") for f in tmp_path.glob("*.jsonl"))
        assert "pw" not in json.loads(written.splitlines()[-1]).get("db_login", "")
        assert '"password"' not in written
