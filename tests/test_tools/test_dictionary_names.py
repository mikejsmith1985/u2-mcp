"""A dictionary item is not a header just because it starts with the same letters.

`list_dictionary` reads the text a `LIST` command prints and works out which
lines are items and which are the command's own header and summary. It decided
by prefix: a line beginning "LIST" was a header, and a line containing "listed"
was the summary.

Both tests are too loose, and the data they get wrong is ordinary. `LIST.PRICE`
and `LIST.COST` are everyday MultiValue dictionary names -- a wholesale
distributor's product file has one of each -- and `LISTED.DATE` is not exotic
either. Every one of them was dropped.

Dropped, not reported. The tool returned a shorter dictionary with no error and
no gap where the item had been, so the only way to notice was to already know
what should have come back. Someone pointing this at their own database to find
out what is in it is, by definition, the person who cannot.

Found by running the discovery path end to end against the demonstration store
rather than by reading it, which is the only way this kind of defect shows up.
"""

from __future__ import annotations

import pytest

from u2_mcp.tools.dictionary import list_dictionary


class _StubFile:
    """A dictionary file holding items whose names collide with the parser."""

    def __init__(self, records: dict[str, str]) -> None:
        self._records = records

    def read(self, record_id: str) -> str:
        if record_id not in self._records:
            raise KeyError(record_id)
        return self._records[record_id]


class _StubManager:
    """Answers the one command and one open the tool makes."""

    def __init__(self, listing: str, records: dict[str, str]) -> None:
        self._listing = listing
        self._records = records

    def execute_command(self, command: str) -> str:
        return self._listing

    def open_file(self, file_name: str) -> _StubFile:
        return _StubFile(self._records)


# Attribute mark, which separates a dictionary item's fields.
AM = chr(254)


def _item(field_number: str, heading: str) -> str:
    """One D-type dictionary record."""
    return AM.join(["D", field_number, "", heading, "10R", "S"])


@pytest.fixture
def product_dictionary(monkeypatch):
    """A product dictionary whose names are ordinary and inconvenient."""
    records = {
        "DESCRIPTION": _item("1", "Description"),
        "LIST.PRICE": _item("6", "List Price"),
        "LIST.COST": _item("7", "List Cost"),
        "LISTED.DATE": _item("8", "Listed"),
        "STATUS": _item("9", "Status"),
    }

    # What Universe prints for `LIST DICT PRODUCT @ID`: its own echoed header,
    # the keys, and a closing summary.
    listing = "\n".join([
        "LIST DICT PRODUCT @ID 11:38:22am  29 AUG 2026  PAGE    1",
        *sorted(records),
        f"{len(records)} records listed.",
    ])

    manager = _StubManager(listing, records)
    monkeypatch.setattr(
        "u2_mcp.tools.dictionary.get_connection_manager", lambda: manager
    )
    return records


class TestNamesThatLookLikeTheCommand:
    def test_every_item_is_returned(self, product_dictionary) -> None:
        result = list_dictionary("PRODUCT")

        assert result["count"] == len(product_dictionary)

    @pytest.mark.parametrize(
        "name", ["LIST.PRICE", "LIST.COST", "LISTED.DATE"]
    )
    def test_a_name_beginning_like_the_verb_survives(
        self, product_dictionary, name: str
    ) -> None:
        # The one that matters commercially: a product file's price field.
        returned = {item["name"] for item in list_dictionary("PRODUCT")["dictionary_items"]}

        assert name in returned

    def test_the_price_field_keeps_its_definition(self, product_dictionary) -> None:
        # Present is not the same as correct. The conversion and field number are
        # what a reader needs in order to use it.
        found = next(
            item for item in list_dictionary("PRODUCT")["dictionary_items"]
            if item["name"] == "LIST.PRICE"
        )

        assert found["field_number"] == "6"
        assert found["heading"] == "List Price"


class TestTheHeaderAndSummaryAreStillRemoved:
    """The looseness was doing a real job, and the fix must keep doing it."""

    def test_the_echoed_command_is_not_an_item(self, product_dictionary) -> None:
        returned = {item["name"] for item in list_dictionary("PRODUCT")["dictionary_items"]}

        assert not any(name.startswith("LIST DICT") for name in returned)

    def test_the_closing_summary_is_not_an_item(self, product_dictionary) -> None:
        returned = {item["name"] for item in list_dictionary("PRODUCT")["dictionary_items"]}

        # "5 records listed." would otherwise arrive as an item called "5".
        assert "5" not in returned
