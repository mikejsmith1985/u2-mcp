"""Edge cases in MultiValue data itself.

Preserving MultiValue structure is the property this project exists for, so the
parser is tested against the shapes real business data takes: empty fields,
trailing marks, a value that is itself a delimiter, deeply repeated groups, and
records large enough to matter.
"""

import pytest

from u2_mcp.utils.dynarray import (
    AM,
    SM,
    VM,
    build_record,
    count_subvalues,
    count_values,
    extract_field,
    extract_subvalue,
    extract_value,
    parse_record,
)


class TestEmptyAndSparseRecords:
    """Records are routinely sparse, and empty is not the same as missing."""

    def test_an_empty_record_parses_to_nothing(self) -> None:
        """An empty string yields no fields rather than one empty field."""
        assert parse_record("") == {}

    def test_leading_empty_fields_do_not_shift_later_ones(self) -> None:
        """Field 3 stays field 3 even when fields 1 and 2 are empty."""
        parsed = parse_record(AM + AM + "THIRD")

        assert parsed["3"] == "THIRD"

    def test_a_trailing_mark_does_not_invent_a_field(self) -> None:
        """A record ending in an attribute mark has no extra field."""
        parsed = parse_record("ONE" + AM + "TWO" + AM)

        assert list(parsed) == ["1", "2"]

    def test_interior_empty_fields_are_omitted_not_guessed(self) -> None:
        """An empty field is absent from the parse rather than becoming a value."""
        parsed = parse_record("ONE" + AM + AM + "THREE")

        assert "2" not in parsed
        assert parsed["3"] == "THREE"


class TestDelimitersInsideData:
    """A record whose data contains the separators must still round trip."""

    def test_a_value_mark_inside_a_field_creates_multivalues(self) -> None:
        """Value marks are structure, so a field containing one becomes a list."""
        parsed = parse_record("A" + VM + "B")

        assert parsed["1"] == ["A", "B"]

    def test_an_empty_multivalue_is_preserved(self) -> None:
        """A missing middle value keeps its position in the list."""
        parsed = parse_record("A" + VM + VM + "C")

        assert parsed["1"] == ["A", "", "C"]

    def test_subvalues_nest_inside_multivalues(self) -> None:
        """Subvalue marks produce a list within the list."""
        parsed = parse_record("A" + SM + "B" + VM + "C")

        assert parsed["1"] == [["A", "B"], "C"]


class TestRoundTrips:
    """Whatever is parsed must be rebuildable, and vice versa."""

    @pytest.mark.parametrize(
        "fields",
        [
            {"1": "SIMPLE"},
            {"1": "NAME", "2": ["a", "b", "c"]},
            {"1": "NAME", "3": "SKIPPED SECOND"},
            {"1": ["x", "y"], "2": "plain"},
            {"1": "unicode MÜLLER", "2": ["€1,000", "£2,000"]},
        ],
    )
    def test_build_then_parse_returns_what_went_in(self, fields: dict) -> None:
        """Building a record and parsing it back yields the same structure."""
        assert parse_record(build_record(fields)) == fields

    def test_a_record_with_a_thousand_fields_round_trips(self) -> None:
        """Wide records are ordinary in MultiValue and must not be truncated."""
        fields = {str(n): f"value{n}" for n in range(1, 1001)}

        assert parse_record(build_record(fields)) == fields

    def test_a_field_with_ten_thousand_values_round_trips(self) -> None:
        """A repeating group can be very large; it must not be silently capped."""
        fields = {"1": [f"v{n}" for n in range(10000)]}

        parsed = parse_record(build_record(fields))

        assert len(parsed["1"]) == 10000
        assert parsed["1"][-1] == "v9999"

    def test_building_an_empty_record_produces_nothing(self) -> None:
        """No fields means no record, not a record of one empty field."""
        assert build_record({}) == ""


class TestExtractionBounds:
    """Direct field access must refuse to read past the end of a record."""

    @pytest.fixture
    def record(self) -> str:
        """A record with two fields, the second holding two values."""
        return "ONE" + AM + "A" + VM + "B"

    @pytest.mark.parametrize("position", [0, -1, 3, 999])
    def test_an_out_of_range_field_returns_empty(self, record: str, position: int) -> None:
        """Reading a field that does not exist yields empty, not an exception."""
        assert extract_field(record, position) == ""

    @pytest.mark.parametrize("position", [0, -1, 5, 999])
    def test_an_out_of_range_value_returns_empty(self, record: str, position: int) -> None:
        """Reading a value that does not exist yields empty, not an exception."""
        assert extract_value(record, 2, position) == ""

    def test_an_out_of_range_subvalue_returns_empty(self, record: str) -> None:
        """Reading a subvalue that does not exist yields empty."""
        assert extract_subvalue(record, 2, 1, 99) == ""

    def test_counting_an_absent_field_is_zero(self, record: str) -> None:
        """Counting values in a field that does not exist is zero, not an error."""
        assert count_values(record, 99) == 0

    def test_counting_subvalues_of_an_absent_value_is_zero(self, record: str) -> None:
        """Counting subvalues of a value that does not exist is zero."""
        assert count_subvalues(record, 99, 1) == 0


class TestTypeCoercion:
    """Values arriving as numbers must not break the builder."""

    def test_numeric_values_are_written_as_text(self) -> None:
        """A number in a field is stored as its text form."""
        assert parse_record(build_record({"1": 42})) == {"1": "42"}

    def test_numeric_multivalues_are_written_as_text(self) -> None:
        """Numbers inside a repeating group are stored as text too."""
        assert parse_record(build_record({"1": [1, 2, 3]})) == {"1": ["1", "2", "3"]}
