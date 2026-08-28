"""The server must not change directories it did not create.

Tightening permissions on a file the server owns is right. Tightening them on a
directory that already existed is not: an operator who points the OAuth database
at a shared location would find that location locked down under them.
"""

import os
import stat
from pathlib import Path

import pytest

from u2_mcp.auth.sqlite_storage import SQLiteAuthStorage


class TestWhatGetsRestricted:
    """Which paths the server tightens, checked without relying on the OS.

    The permission bits themselves are only meaningful on POSIX, but the decision
    about *which* paths to touch is platform-independent and worth testing
    everywhere -- including on the machine this fork is developed on.
    """

    @pytest.fixture
    def restricted(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Record every path the storage asks to restrict."""
        recorded: list[Path] = []
        monkeypatch.setattr(
            "u2_mcp.auth.sqlite_storage._restrict_to_owner", lambda path: recorded.append(path)
        )
        return recorded

    def test_an_existing_parent_is_never_touched(
        self, tmp_path: Path, restricted: list[Path]
    ) -> None:
        """A directory the server did not create is left exactly as it was."""
        shared = tmp_path / "shared"
        shared.mkdir()

        SQLiteAuthStorage(shared / "auth.db")

        assert shared not in restricted

    def test_the_database_file_is_always_restricted(
        self, tmp_path: Path, restricted: list[Path]
    ) -> None:
        """The file the server creates is always protected."""
        shared = tmp_path / "shared"
        shared.mkdir()

        SQLiteAuthStorage(shared / "auth.db")

        assert shared / "auth.db" in restricted

    def test_a_parent_the_server_creates_is_restricted(
        self, tmp_path: Path, restricted: list[Path]
    ) -> None:
        """Creating a directory means creating it safely."""
        created = tmp_path / "brand" / "new"

        SQLiteAuthStorage(created / "auth.db")

        assert created in restricted


@pytest.mark.skipif(
    os.name == "nt", reason="Windows ignores POSIX permission bits; ACLs govern access there"
)
class TestExistingDirectoriesAreLeftAlone:
    """A directory that was already there keeps the permissions it had."""

    def test_a_shared_parent_directory_is_not_restricted(self, tmp_path: Path) -> None:
        """Pointing the database at an existing shared directory does not lock it."""
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755)
        before = stat.S_IMODE(shared.stat().st_mode)

        SQLiteAuthStorage(shared / "auth.db")

        assert stat.S_IMODE(shared.stat().st_mode) == before

    def test_the_database_file_is_still_restricted(self, tmp_path: Path) -> None:
        """The file the server creates is still its own to protect."""
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755)

        SQLiteAuthStorage(shared / "auth.db")

        assert stat.S_IMODE((shared / "auth.db").stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(
    os.name == "nt", reason="Windows ignores POSIX permission bits; ACLs govern access there"
)
class TestCreatedDirectoriesAreRestricted:
    """A directory the server creates is one it should protect."""

    def test_a_new_parent_directory_is_owner_only(self, tmp_path: Path) -> None:
        """Creating the directory means creating it safely."""
        created = tmp_path / "brand" / "new"

        SQLiteAuthStorage(created / "auth.db")

        assert stat.S_IMODE(created.stat().st_mode) & 0o077 == 0
