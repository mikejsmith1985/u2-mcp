"""Tests for schema discovery and BASIC subroutine calls.

Discovery is how the AI learns a file's field names before it guesses at them,
and subroutines are how it reuses business logic that already exists. Both need
to behave predictably when the server answers with something unexpected.
"""

from tests.mocks.mock_uopy import MockSession, UOError
from u2_mcp.connection import ConnectionManager
from u2_mcp.tools.dictionary import describe_file, get_field_definition, list_dictionary
from u2_mcp.tools.subroutine import call_subroutine, list_catalog

AM = chr(254)

# A D-type dictionary item, in Universe's field order:
# 1 type, 2 field number, 3 conversion, 4 heading, 5 format, 6 single/multi.
NAME_FIELD = "D" + AM + "1" + AM + "" + AM + "Customer Name" + AM + "30L" + AM + "S"
STATE_FIELD = "D" + AM + "4" + AM + "" + AM + "State" + AM + "2L" + AM + "S"


def seed_dictionary(session: MockSession) -> None:
    """Give CUSTOMERS a dictionary with two data fields."""
    session.add_file("DICT CUSTOMERS", {"NAME": NAME_FIELD, "STATE": STATE_FIELD})
    session.set_command_responses({"LIST DICT CUSTOMERS": "NAME\nSTATE\n2 records listed."})


class TestDictionaryListing:
    """Reading a file's field definitions."""

    def test_dictionary_items_are_listed(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """The file's dictionary items come back by name."""
        seed_dictionary(mock_uopy)

        result = list_dictionary("CUSTOMERS")

        names = [item["name"] for item in result["dictionary_items"]]
        assert "NAME" in names
        assert "STATE" in names

    def test_field_numbers_are_reported(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A data field reports which attribute of the record it reads."""
        seed_dictionary(mock_uopy)

        result = list_dictionary("CUSTOMERS")
        name_item = next(item for item in result["dictionary_items"] if item["name"] == "NAME")

        assert name_item["field_number"] == "1"
        assert name_item["heading"] == "Customer Name"

    def test_a_single_field_definition_is_returned(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """One named field can be looked up on its own."""
        seed_dictionary(mock_uopy)

        result = get_field_definition("CUSTOMERS", "NAME")

        assert "error" not in result
        assert result["name"] == "NAME"
        assert result["type"] == "D"
        assert result["field_number"] == "1"

    def test_an_unknown_field_reports_an_error(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Asking for a field that is not in the dictionary reports an error."""
        seed_dictionary(mock_uopy)

        assert "error" in get_field_definition("CUSTOMERS", "NOSUCH")

    def test_describe_file_summarises_the_structure(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A high-level description names the file and its fields."""
        seed_dictionary(mock_uopy)

        result = describe_file("CUSTOMERS")

        assert result["file"] == "CUSTOMERS"
        assert "error" not in result


class TestSubroutineCalls:
    """Running cataloged BASIC programs."""

    def test_arguments_are_returned_after_the_call(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Output arguments come back to the caller."""
        result = call_subroutine("GET.CUSTOMER", ["AR1042"])

        assert result["status"] == "success"
        assert result["args_out"] == ["RESULT:AR1042"]

    def test_the_subroutine_receives_its_arguments(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """What was passed in is what the program was called with."""
        call_subroutine("GET.CUSTOMER", ["AR1042"])

        assert mock_uopy.called_subroutines == [("GET.CUSTOMER", ["AR1042"])]

    def test_output_only_arguments_are_padded(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A program with output slots is called with all of them present."""
        result = call_subroutine("GET.CUSTOMER", ["AR1042"], num_args=3)

        assert result["num_args"] == 3
        assert len(result["args_out"]) == 3

    def test_too_few_slots_for_the_arguments_is_refused(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Declaring fewer arguments than are supplied is refused before the call."""
        result = call_subroutine("GET.CUSTOMER", ["a", "b", "c"], num_args=2)

        assert "error" in result
        assert mock_uopy.called_subroutines == []

    def test_a_failing_subroutine_reports_the_error(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """A program that fails on the server surfaces as an error, not a crash."""
        mock_uopy.subroutine_error = UOError("Program not cataloged")

        result = call_subroutine("MISSING.PROGRAM", ["x"])

        assert "error" in result

    def test_the_catalog_can_be_listed(
        self, connection_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Available cataloged programs are discoverable."""
        result = list_catalog()

        assert "error" not in result
