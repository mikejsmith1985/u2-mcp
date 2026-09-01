"""A file is not a header because its name contains the same word.

`list_files` decided which lines of a listing were file names by looking for
header words anywhere in the line: RECORDS, LISTED, SELECTED, FILE NAME. Any file
whose name contained one of them was dropped.

`RECORDS` is a plausible MultiValue file name and so is `SELECTED.ITEMS`. Neither
is exotic, and a file that vanishes from `list_files` is a file the person
exploring the account never learns exists -- with no error and no gap to notice.

The same defect as the one in `list_dictionary`, in the same shape, found the
same way: by exercising the path rather than reading it.
"""

from __future__ import annotations

import pytest

from u2_mcp.tools.files import _parse_file_list


class TestNamesThatContainHeaderWords:
    @pytest.mark.parametrize(
        "name",
        ["RECORDS", "SELECTED.ITEMS", "LISTED.PARTS", "FILE.NAME.MAP", "RECORDS.ARCHIVE"],
    )
    def test_a_file_named_like_a_header_survives(self, name: str) -> None:
        listing = "\n".join(["BRANCH", name, "PRODUCT", "3 records listed."])

        assert name in _parse_file_list(listing)

    def test_the_ordinary_files_still_come_back(self) -> None:
        listing = "\n".join(["BRANCH", "PRODUCT", "2 records listed."])

        assert _parse_file_list(listing) == ["BRANCH", "PRODUCT"]


class TestTheHeaderAndSummaryAreStillRemoved:
    """The looseness was doing a real job; the fix has to keep doing it."""

    def test_the_closing_summary_is_not_a_file(self) -> None:
        parsed = _parse_file_list("BRANCH\n1 records listed.")

        assert parsed == ["BRANCH"]

    def test_a_separator_line_is_not_a_file(self) -> None:
        parsed = _parse_file_list("----------\nBRANCH\n***\n1 records listed.")

        assert parsed == ["BRANCH"]

    def test_a_column_header_is_not_a_file(self) -> None:
        # Universe prints this above the listing on some releases.
        parsed = _parse_file_list("FILE NAME\nBRANCH\n1 records listed.")

        assert parsed == ["BRANCH"]
