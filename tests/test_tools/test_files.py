"""Tests for the record access tools.

These are the tools that touch business data, so they carry the heaviest safety
obligations: read-only mode must hold, writes must be confirmed deliberately,
and MultiValue structure must survive both directions of the trip.
"""

from collections.abc import Iterator

import pytest

from tests.mocks.mock_uopy import MockSession
from u2_mcp.config import U2Config
from u2_mcp.connection import ConnectionManager
from u2_mcp.tools.files import (
    delete_record,
    get_file_info,
    list_files,
    read_record,
    read_records,
    write_record,
)

AM = chr(254)
VM = chr(253)

# A customer with three phone numbers in one field -- the shape that makes
# MultiValue MultiValue.
CUSTOMER_RECORD = "SMITH JOHN" + AM + "555-1234" + VM + "555-9876" + AM + "CA"


@pytest.fixture
def customers(connection_manager: ConnectionManager, mock_uopy: MockSession) -> MockSession:
    """Seed a CUSTOMERS file with one structured record."""
    mock_uopy.add_file("CUSTOMERS", {"AR1042": CUSTOMER_RECORD})
    return mock_uopy


class TestReadingRecords:
    """Reading data out without flattening it."""

    def test_a_record_is_returned_by_id(self, customers: MockSession) -> None:
        """Reading a known key returns that record."""
        result = read_record("CUSTOMERS", "AR1042")

        assert result["id"] == "AR1042"
        assert "error" not in result

    def test_multivalues_come_back_as_a_list(self, customers: MockSession) -> None:
        """Three phone numbers in one field arrive as three list items, not one string."""
        result = read_record("CUSTOMERS", "AR1042")

        assert result["fields"]["2"] == ["555-1234", "555-9876"]

    def test_single_values_stay_scalar(self, customers: MockSession) -> None:
        """A field with one value is a string, not a one-item list."""
        result = read_record("CUSTOMERS", "AR1042")

        assert result["fields"]["1"] == "SMITH JOHN"

    def test_a_missing_record_reports_an_error(self, customers: MockSession) -> None:
        """An unknown key returns an error rather than an empty record."""
        result = read_record("CUSTOMERS", "NOSUCH")

        assert "error" in result

    def test_a_missing_file_reports_an_error(self, customers: MockSession) -> None:
        """A file that will not open reports why."""
        customers.set_missing_file("GHOST")

        result = read_record("GHOST", "AR1042")

        assert "error" in result
        assert result["file"] == "GHOST"


class TestReadingManyRecords:
    """Batch reads report both what was found and what was not."""

    def test_found_records_are_returned(self, customers: MockSession) -> None:
        """Known keys come back as records."""
        result = read_records("CUSTOMERS", ["AR1042"])

        assert result["count"] == 1

    def test_missing_keys_are_reported_individually(self, customers: MockSession) -> None:
        """A key that does not exist is named, rather than silently dropped."""
        result = read_records("CUSTOMERS", ["AR1042", "NOSUCH"])

        assert result["count"] == 1
        assert result["errors"][0]["id"] == "NOSUCH"

    def test_a_batch_over_the_limit_is_refused(
        self, customers: MockSession, connection_manager: ConnectionManager
    ) -> None:
        """Asking for more records than the configured cap is refused up front."""
        too_many = [f"ID{n}" for n in range(connection_manager.config.max_records + 1)]

        result = read_records("CUSTOMERS", too_many)

        assert "error" in result


class TestWriteSafety:
    """Writes must be deliberate, and must not happen at all in read-only mode."""

    def test_a_write_without_confirmation_does_nothing(self, customers: MockSession) -> None:
        """An unconfirmed write asks for confirmation instead of writing."""
        result = write_record("CUSTOMERS", "NEW01", {"1": "TEST"})

        assert result["status"] == "confirmation_required"

    def test_an_unconfirmed_write_leaves_the_data_untouched(self, customers: MockSession) -> None:
        """Nothing reaches the file until confirm is set."""
        write_record("CUSTOMERS", "NEW01", {"1": "TEST"})

        assert "NEW01" not in customers._files["CUSTOMERS"]

    def test_a_confirmed_write_stores_the_record(self, customers: MockSession) -> None:
        """With confirmation, the record is written."""
        result = write_record("CUSTOMERS", "NEW01", {"1": "TEST"}, confirm=True)

        assert result["status"] == "success"
        assert "NEW01" in customers._files["CUSTOMERS"]

    def test_multivalues_survive_the_round_trip(self, customers: MockSession) -> None:
        """A list written as multivalues reads back as the same list."""
        write_record("CUSTOMERS", "NEW02", {"1": "ACME", "2": ["a@x.com", "b@x.com"]}, confirm=True)

        assert read_record("CUSTOMERS", "NEW02")["fields"]["2"] == ["a@x.com", "b@x.com"]

    def test_an_unconfirmed_delete_does_nothing(self, customers: MockSession) -> None:
        """A delete also requires explicit confirmation."""
        result = delete_record("CUSTOMERS", "AR1042")

        assert result["status"] == "confirmation_required"
        assert "AR1042" in customers._files["CUSTOMERS"]

    def test_a_confirmed_delete_removes_the_record(self, customers: MockSession) -> None:
        """With confirmation, the record is deleted."""
        result = delete_record("CUSTOMERS", "AR1042", confirm=True)

        assert result["status"] == "deleted"
        assert "AR1042" not in customers._files["CUSTOMERS"]


class TestReadOnlyMode:
    """Read-only mode is the safe setting for a pilot, so it must genuinely hold."""

    @pytest.fixture
    def read_only_manager(
        self, mock_config_read_only: U2Config, mock_uopy: MockSession
    ) -> Iterator[ConnectionManager]:
        """Install a read-only connection manager as the server's global."""
        import u2_mcp.server as server_module

        manager = ConnectionManager(mock_config_read_only)
        server_module._connection_manager = manager
        mock_uopy.add_file("CUSTOMERS", {"AR1042": CUSTOMER_RECORD})
        try:
            yield manager
        finally:
            server_module._connection_manager = None

    def test_writes_are_refused(self, read_only_manager: ConnectionManager) -> None:
        """A confirmed write is still refused when the server is read-only."""
        result = write_record("CUSTOMERS", "NEW01", {"1": "TEST"}, confirm=True)

        assert "error" in result

    def test_a_refused_write_changes_nothing(
        self, read_only_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """Read-only means the data is untouched, not merely that an error is returned."""
        write_record("CUSTOMERS", "NEW01", {"1": "TEST"}, confirm=True)

        assert "NEW01" not in mock_uopy._files["CUSTOMERS"]

    def test_deletes_are_refused(self, read_only_manager: ConnectionManager) -> None:
        """A confirmed delete is refused when the server is read-only."""
        result = delete_record("CUSTOMERS", "AR1042", confirm=True)

        assert "error" in result

    def test_a_refused_delete_leaves_the_record(
        self, read_only_manager: ConnectionManager, mock_uopy: MockSession
    ) -> None:
        """The record survives a delete attempt in read-only mode."""
        delete_record("CUSTOMERS", "AR1042", confirm=True)

        assert "AR1042" in mock_uopy._files["CUSTOMERS"]

    def test_reads_still_work(self, read_only_manager: ConnectionManager) -> None:
        """Read-only blocks writing, not reading."""
        assert read_record("CUSTOMERS", "AR1042")["id"] == "AR1042"


class TestFileDiscovery:
    """Finding out what is on the server."""

    def test_files_are_listed(self, customers: MockSession) -> None:
        """The account's files are returned as a list with a count."""
        result = list_files()

        assert result["count"] >= 1
        assert "CUSTOMERS" in result["files"]

    def test_file_statistics_are_returned(self, customers: MockSession) -> None:
        """File information comes back without error."""
        result = get_file_info("CUSTOMERS")

        assert "error" not in result
