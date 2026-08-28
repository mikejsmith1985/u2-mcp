"""Tests for the knowledge persistence tools.

What the assistant learns about a database is written to a file it will read
again in a later session, so a mistake here is durable. These tests cover the
round trip, the de-duplication that stops one file accruing five entries under
five spellings of its name, and deletion.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import u2_mcp.utils.knowledge as knowledge_module
from u2_mcp.tools.knowledge import (
    delete_knowledge,
    get_all_knowledge,
    get_knowledge_topic,
    list_knowledge,
    save_knowledge,
    search_knowledge,
)


@pytest.fixture
def knowledge_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the knowledge store at a temporary file, and reset it afterwards."""
    path = tmp_path / "knowledge.md"
    monkeypatch.setenv("U2_KNOWLEDGE_PATH", str(path))
    knowledge_module._knowledge_store = None
    try:
        yield path
    finally:
        knowledge_module._knowledge_store = None


class TestSavingAndRecall:
    """The round trip that makes the feature worth having."""

    def test_saved_knowledge_is_written_to_the_file(self, knowledge_file: Path) -> None:
        """Saving a topic puts it on disk, where a later session will find it."""
        save_knowledge("AR-CUST file", "Customer master file. Key is customer number.")

        assert "customer number" in knowledge_file.read_text(encoding="utf-8")

    def test_a_saved_topic_can_be_read_back(self, knowledge_file: Path) -> None:
        """A topic is retrievable by name."""
        save_knowledge("AR-CUST file", "Customer master file.")

        result = get_knowledge_topic("AR-CUST file")

        assert "Customer master file." in result["content"]

    def test_topics_are_listed(self, knowledge_file: Path) -> None:
        """Saved topics appear in the index."""
        save_knowledge("AR-CUST file", "Customer master.")
        save_knowledge("Date formats", "Internal format; use D2/ conversion.")

        topics = [entry["topic"] for entry in list_knowledge()["topics"]]

        assert len(topics) == 2

    def test_everything_can_be_retrieved_at_once(self, knowledge_file: Path) -> None:
        """The whole knowledge base is available to seed a new conversation."""
        save_knowledge("AR-CUST file", "Customer master.")

        assert "Customer master." in get_all_knowledge()["knowledge"]

    def test_an_unknown_topic_reports_an_error(self, knowledge_file: Path) -> None:
        """Asking for something never saved reports an error rather than empty content."""
        assert "error" in get_knowledge_topic("Never written")


class TestReplacingAndAppending:
    """Updating what was learned earlier."""

    def test_saving_again_replaces_the_content(self, knowledge_file: Path) -> None:
        """By default a second save corrects the first rather than duplicating it."""
        save_knowledge("AR-CUST file", "First guess.")
        save_knowledge("AR-CUST file", "Corrected understanding.")

        content = get_knowledge_topic("AR-CUST file")["content"]
        assert "Corrected understanding." in content
        assert "First guess." not in content

    def test_appending_keeps_both(self, knowledge_file: Path) -> None:
        """Appending adds to what was already known."""
        save_knowledge("AR-CUST file", "Key is customer number.")
        save_knowledge("AR-CUST file", "Field 1 is name.", append=True)

        content = get_knowledge_topic("AR-CUST file")["content"]
        assert "Key is customer number." in content
        assert "Field 1 is name." in content

    def test_near_duplicate_topic_names_are_merged(self, knowledge_file: Path) -> None:
        """'AR-CUST' and 'AR-CUST file' are the same subject, not two."""
        save_knowledge("AR-CUST - Customer Master", "Key is customer number.")
        save_knowledge("AR-CUST file", "Field 1 is name.", append=True)

        assert len(list_knowledge()["topics"]) == 1


class TestSearchAndDeletion:
    """Finding and removing what was learned."""

    def test_search_finds_a_matching_topic(self, knowledge_file: Path) -> None:
        """Searching returns the topics whose content matches."""
        save_knowledge("AR-CUST file", "Customer master file. Key is customer number.")
        save_knowledge("Date formats", "Internal format; use D2/ conversion.")

        result = search_knowledge("customer number")

        assert result["match_count"] == 1

    def test_search_without_matches_returns_nothing(self, knowledge_file: Path) -> None:
        """A search that matches nothing reports zero results, not an error."""
        save_knowledge("AR-CUST file", "Customer master file.")

        assert search_knowledge("nonexistent term")["match_count"] == 0

    def test_deleting_requires_confirmation(self, knowledge_file: Path) -> None:
        """An unconfirmed delete asks for confirmation rather than removing anything."""
        save_knowledge("AR-CUST file", "Customer master file.")

        result = delete_knowledge("AR-CUST file")

        assert result["status"] == "confirmation_required"
        assert list_knowledge()["count"] == 1

    def test_a_confirmed_delete_removes_the_topic(self, knowledge_file: Path) -> None:
        """With confirmation, the topic is gone."""
        save_knowledge("AR-CUST file", "Customer master file.")

        delete_knowledge("AR-CUST file", confirm=True)

        assert list_knowledge()["count"] == 0

    def test_deleting_something_absent_reports_an_error(self, knowledge_file: Path) -> None:
        """Deleting a topic that was never saved is reported, not silently ignored."""
        result = delete_knowledge("Never written", confirm=True)

        assert result["status"] == "error"
