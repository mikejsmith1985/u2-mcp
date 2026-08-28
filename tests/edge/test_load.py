"""Behaviour under concurrent load.

A shared server does several things at once, and the failures that only appear
under load are the ones nobody sees in testing: a corrupted audit line, a
duplicated connection, a slot evicted from under a running query.
"""

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.config import U2Config
from u2_mcp.credentials import MappedCredentialResolver
from u2_mcp.identity import CallerIdentity, use_caller
from u2_mcp.registry import ConnectionRegistry
from u2_mcp.utils.audit import AuditLogger

CALLERS = 50


@pytest.fixture
def many_users(tmp_path: Path) -> Path:
    """Write a credential map for a crowd of callers."""
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {f"user{n}@example.com": {"user": f"U{n}", "password": "pw"} for n in range(CALLERS)}
        ),
        encoding="utf-8",
    )
    return path


class TestAuditUnderLoad:
    """An audit trail that cannot be parsed is not an audit trail."""

    @pytest.fixture
    def audit_logger(self, tmp_path: Path) -> AuditLogger:
        """Provide an audit logger writing to a temporary directory."""
        logger = AuditLogger(audit_path=str(tmp_path), include_results=True)
        logger.start_session("load-test")
        return logger

    def test_every_line_is_valid_json_under_concurrent_writes(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """Concurrent writes must not interleave into an unparseable line."""
        barrier = threading.Barrier(CALLERS)

        def record(index: int) -> None:
            barrier.wait()
            with use_caller(CallerIdentity(subject=f"user{index}@example.com")):
                audit_logger.log_tool_call(
                    "execute_query",
                    {"query": "LIST CUSTOMERS " + "x" * 500},
                    result={"output": "y" * 2000},
                )

        threads = [threading.Thread(target=record, args=(n,)) for n in range(CALLERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for log_file in tmp_path.glob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)  # raises if the line was interleaved

    def test_no_record_is_lost_under_concurrent_writes(
        self, audit_logger: AuditLogger, tmp_path: Path
    ) -> None:
        """Every call that happened appears exactly once."""
        barrier = threading.Barrier(CALLERS)

        def record(index: int) -> None:
            barrier.wait()
            with use_caller(CallerIdentity(subject=f"user{index}@example.com")):
                audit_logger.log_tool_call("read_record", {"record_id": f"ID{index}"})

        threads = [threading.Thread(target=record, args=(n,)) for n in range(CALLERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        subjects = set()
        for log_file in tmp_path.glob("*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    if entry.get("event") == "tool_call":
                        subjects.add(entry["user"])

        assert len(subjects) == CALLERS


class TestRegistryUnderLoad:
    """Fifty callers arriving at once must be handled predictably."""

    @pytest.fixture
    def registry(
        self, mock_config: U2Config, mock_uopy: MockSession, many_users: Path
    ) -> ConnectionRegistry:
        """A registry with room for ten of fifty possible callers."""
        mock_config.max_connections = 10
        return ConnectionRegistry(mock_config, MappedCredentialResolver(mock_config, many_users))

    def test_concurrent_callers_stay_within_the_connection_limit(
        self, registry: ConnectionRegistry
    ) -> None:
        """The ceiling holds even when every caller arrives at the same moment."""
        barrier = threading.Barrier(CALLERS)
        errors: list[Exception] = []

        def request(index: int) -> None:
            barrier.wait()
            try:
                registry.for_caller(CallerIdentity(subject=f"user{index}@example.com"))
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=request, args=(n,)) for n in range(CALLERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert registry.get_stats()["connection_count"] <= 10

    def test_one_caller_arriving_many_times_gets_one_connection(
        self, registry: ConnectionRegistry
    ) -> None:
        """Concurrent requests from the same person share a single slot."""
        barrier = threading.Barrier(CALLERS)
        managers: list[Any] = []
        lock = threading.Lock()

        def request() -> None:
            barrier.wait()
            manager = registry.for_caller(CallerIdentity(subject="user1@example.com"))
            with lock:
                managers.append(manager)

        threads = [threading.Thread(target=request) for _ in range(CALLERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len({id(manager) for manager in managers}) == 1


class TestEvictionDuringActivity:
    """A slot may be evicted while its owner is mid-request."""

    def test_an_evicted_caller_can_still_work(
        self, mock_config: U2Config, mock_uopy: MockSession, many_users: Path
    ) -> None:
        """Losing a slot to eviction costs a reconnect, not an error."""
        mock_config.max_connections = 2
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, many_users)
        )
        alice = CallerIdentity(subject="user0@example.com")
        first = registry.for_caller(alice)
        first.connect()

        # Push Alice out of the cache entirely.
        for n in range(1, 6):
            registry.for_caller(CallerIdentity(subject=f"user{n}@example.com"))

        assert "test-user" in registry.for_caller(alice).execute_command("WHO")

    def test_eviction_closes_the_session_it_removes(
        self, mock_config: U2Config, mock_uopy: MockSession, many_users: Path
    ) -> None:
        """An evicted slot must not leave its database connection open."""
        mock_config.max_connections = 2
        registry = ConnectionRegistry(
            mock_config, MappedCredentialResolver(mock_config, many_users)
        )
        evicted = registry.for_caller(CallerIdentity(subject="user0@example.com"))
        evicted.connect()

        for n in range(1, 6):
            registry.for_caller(CallerIdentity(subject=f"user{n}@example.com"))

        assert evicted.list_connections() == {}
