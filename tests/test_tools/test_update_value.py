"""Changing one value without moving the ones beside it.

The tool exists because `write_record` cannot make this guarantee. Handing the
server a whole rebuilt record puts responsibility for the record's shape on
whoever assembled it, and in a MultiValue file the shape is load-bearing:
position n of a set of parallel fields describes one branch, so a field that
comes back a different length has moved a quantity onto a different branch.

Nothing catches that. The record stays well formed, no constraint is broken, and
no later read can tell. So these tests are mostly about what the tool refuses.
"""

from __future__ import annotations

from typing import Any

import pytest

from u2_mcp.tools.update import update_value

AM = chr(254)
VM = chr(253)


class _Handle:
    """A file that can update one value, and records what it was asked to do."""

    def __init__(self, record: str) -> None:
        self.record = record
        self.calls: list[tuple[int, int, str]] = []

    def read(self, record_id: str) -> str:
        return self.record

    def update_value(self, record_id: str, position: int, index: int, value: str) -> str:
        self.calls.append((position, index, value))

        fields = [field.split(VM) for field in self.record.split(AM)]
        fields[position - 1][index] = value
        self.record = AM.join(VM.join(field) for field in fields)

        return self.record


class _ReadOnlyHandle:
    """A file with no in-place update, like the read-only driver's."""

    def read(self, record_id: str) -> str:
        return "anything"


class _Manager:
    """Answers the one open the tool makes."""

    def __init__(self, handle: Any, read_only: bool = False) -> None:
        self._handle = handle
        self.config = type("Config", (), {"read_only": read_only})()

    def open_file(self, file_name: str) -> Any:
        return self._handle


@pytest.fixture
def inventory(monkeypatch):
    """Four branches, with bins recorded for only the first two."""
    record = AM.join(
        [
            VM.join(["AUR", "LKW", "BOU", "DEN"]),
            VM.join(["10", "20", "30", "40"]),
            VM.join(["1", "2", "3", "4"]),
            VM.join(["A-1", "B-2"]),
        ]
    )

    handle = _Handle(record)
    monkeypatch.setattr("u2_mcp.tools.update.get_connection_manager", lambda: _Manager(handle))

    return handle


class TestItRefusesBeforeItActs:
    def test_a_write_needs_confirmation(self, inventory):
        result = update_value("INVENTORY", "P-1", 2, 0, "99")

        assert result["status"] == "confirmation_required"
        assert inventory.calls == []

    def test_read_only_mode_refuses_even_with_confirmation(self, monkeypatch):
        handle = _Handle("a")
        monkeypatch.setattr(
            "u2_mcp.tools.update.get_connection_manager",
            lambda: _Manager(handle, read_only=True),
        )

        result = update_value("INVENTORY", "P-1", 2, 0, "99", confirm=True)

        assert "read-only" in result["error"]
        assert handle.calls == []

    def test_a_driver_that_cannot_update_says_so(self, monkeypatch):
        # The read-only driver has no update_value. Saying that plainly is better
        # than an AttributeError, which reads like a bug in the server.
        monkeypatch.setattr(
            "u2_mcp.tools.update.get_connection_manager",
            lambda: _Manager(_ReadOnlyHandle()),
        )

        result = update_value("INVENTORY", "P-1", 2, 0, "99", confirm=True)

        assert "no in-place update" in result["error"]


class TestItReportsWhetherTheShapeSurvived:
    def test_a_clean_change_reports_alignment_preserved(self, inventory):
        result = update_value("INVENTORY", "P-1", 2, 2, "999", confirm=True)

        assert result["alignment_preserved"] is True
        assert result["field_lengths_before"] == result["field_lengths_after"]

    def test_the_before_and_after_are_both_returned(self, inventory):
        # Both, because "it worked" is a claim and the two records are evidence.
        result = update_value("INVENTORY", "P-1", 2, 0, "555", confirm=True)

        assert result["before"] != result["after"]
        assert result["after"].split(AM)[1].split(VM)[0] == "555"

    def test_a_short_field_is_reported_as_short_rather_than_padded(self, inventory):
        # Bins are recorded for two of four branches, and that is legitimate.
        # The lengths say so rather than the tool quietly making them match.
        result = update_value("INVENTORY", "P-1", 4, 1, "B-9", confirm=True)

        assert result["field_lengths_before"] == [4, 4, 4, 2]
        assert result["field_lengths_after"] == [4, 4, 4, 2]
        assert result["alignment_preserved"] is True

    def test_a_change_that_moved_a_value_is_reported(self, monkeypatch):
        # The failure the tool exists to surface. A driver that padded to reach
        # an index would return a longer field, and this must not call that fine.
        class _PaddingHandle(_Handle):
            def update_value(self, record_id: str, position: int, index: int, value: str) -> str:
                fields = [field.split(VM) for field in self.record.split(AM)]
                target = fields[position - 1]
                while len(target) <= index:
                    target.append("")
                target[index] = value
                self.record = AM.join(VM.join(field) for field in fields)
                return self.record

        handle = _PaddingHandle(
            AM.join([VM.join(["AUR", "LKW", "BOU", "DEN"]), VM.join(["A-1", "B-2"])])
        )
        monkeypatch.setattr("u2_mcp.tools.update.get_connection_manager", lambda: _Manager(handle))

        result = update_value("INVENTORY", "P-1", 2, 3, "D-4", confirm=True)

        assert result["field_lengths_before"] == [4, 2]
        assert result["field_lengths_after"] == [4, 4]
        assert result["alignment_preserved"] is False
