"""Mock implementations of uopy objects for testing.

These mocks simulate Rocket's `uopy` package without requiring a live
Universe/UniData server. They deliberately mirror the *current* uopy 1.4 API
that `u2_mcp` calls -- constructor-style objects (`uopy.File(name, session=...)`,
`uopy.Command(text, session=...)`) rather than the older session-factory style --
so a test that passes here means the production code path really works.

Install them with the `mock_uopy` fixture in conftest.py.
"""

from typing import Any


class UOError(Exception):
    """Stand-in for `uopy.UOError`."""

    pass


class MockFile:
    """Mock Universe file handle, created as `uopy.File(name, session=...)`."""

    def __init__(self, name: str = "", session: Any = None, records: dict[str, str] | None = None):
        """Open a mock file, raising UOError if the session says it does not exist."""
        self.name = name
        self.session = session
        # A session may pre-seed records, and may declare a file missing.
        if session is not None and hasattr(session, "_files"):
            if name in getattr(session, "_missing_files", set()):
                raise UOError(f"File '{name}' not found")
            self._records: dict[str, str] = session._files.setdefault(name, {})
        else:
            self._records = records if records is not None else {}
        self.is_open = True

    def read(self, record_id: str) -> str:
        """Read a record's raw data, raising UOError when the key is absent."""
        if record_id not in self._records:
            raise UOError(f"Record '{record_id}' not found in '{self.name}'")
        return self._records[record_id]

    def write(self, record_id: str, data: Any) -> None:
        """Write raw record data under the given key."""
        self._records[record_id] = str(data)

    def delete(self, record_id: str) -> None:
        """Delete a record, raising UOError when the key is absent."""
        if record_id not in self._records:
            raise UOError(f"Record '{record_id}' not found in '{self.name}'")
        del self._records[record_id]

    def close(self) -> None:
        """Close the file handle."""
        self.is_open = False


class MockCommand:
    """Mock TCL command, created as `uopy.Command(text, session=...)`."""

    def __init__(self, command_text: str = "", session: Any = None):
        """Prepare a command; the session supplies canned responses."""
        self.command_text = command_text
        self.session = session
        self.response: str = ""
        self.status: int = 0

    def run(self) -> None:
        """Execute the command, recording it on the session and setting a response."""
        session = self.session
        if session is not None:
            session.executed_commands.append(self.command_text)
            if session.command_error is not None:
                raise session.command_error
            if session.command_delay_seconds:
                import time

                time.sleep(session.command_delay_seconds)
            for pattern, canned in session.command_responses.items():
                if pattern.upper() in self.command_text.upper():
                    self.response = canned
                    return
        self.response = _default_response(self.command_text)


def _default_response(command_text: str) -> str:
    """Return a plausible Universe response for common commands."""
    upper = command_text.upper()
    if upper.startswith("WHO"):
        return "1 test-user TEST"
    if upper.startswith("LISTFILES") or upper.startswith("LIST.FILE"):
        return "CUSTOMERS\nORDERS\nPRODUCTS\n3 files listed."
    if upper.startswith("FILE.STAT"):
        return "File name ..... CUSTOMERS\nNumber of records ..... 500"
    if upper.startswith("COUNT"):
        return "5 records counted."
    if upper.startswith("LIST") or upper.startswith("SORT"):
        return "ID001 Value1\nID002 Value2\n2 records listed."
    if upper.startswith("SELECT") or upper.startswith("SSELECT"):
        return "3 records selected to list 0."
    return f"Command executed: {command_text}"


class MockList:
    """Mock select list, created as `uopy.List(session=...)`."""

    def __init__(self, session: Any = None, list_no: int = 0):
        """Create a select list backed by the session's pending record ids."""
        self.session = session
        self.list_no = list_no
        pending = list(getattr(session, "select_list", [])) if session is not None else []
        self._pending: list[str] = pending
        self._index = 0

    def next(self) -> str | None:
        """Return the next record id, or None when the list is exhausted."""
        if self._index >= len(self._pending):
            return None
        value = self._pending[self._index]
        self._index += 1
        return value


class MockSubroutine:
    """Mock BASIC subroutine, created as `uopy.Subroutine(name, n, session=...)`."""

    def __init__(self, name: str = "", num_args: int = 0, session: Any = None):
        """Prepare a subroutine call with `num_args` argument slots."""
        self.name = name
        self.num_args = num_args
        self.session = session
        self.args: list[str] = [""] * num_args

    def call(self) -> None:
        """Execute the subroutine, prefixing each non-empty argument with RESULT:."""
        if self.session is not None and self.session.subroutine_error is not None:
            raise self.session.subroutine_error
        for i, value in enumerate(self.args):
            if value:
                self.args[i] = f"RESULT:{value}"


class MockSession:
    """Mock uopy session returned by `uopy.connect(...)`.

    Test helpers let a test declare exactly what the fake server should do:
    seeded records, canned command output, a select list, or a raised error.
    """

    def __init__(self, **connect_kwargs: Any):
        """Create an active session, remembering the connect arguments."""
        self.connect_kwargs = connect_kwargs
        self.is_active: bool = True
        self.executed_commands: list[str] = []
        self.command_responses: dict[str, str] = {}
        self.command_error: Exception | None = None
        self.command_delay_seconds: float = 0.0
        self.subroutine_error: Exception | None = None
        self.select_list: list[str] = []
        self.in_transaction: bool = False
        self.transaction_log: list[str] = []
        self._files: dict[str, dict[str, str]] = {}
        self._missing_files: set[str] = set()

    # -- production API -----------------------------------------------------

    def close(self) -> None:
        """Close the session."""
        self.is_active = False

    def tx_start(self) -> None:
        """Begin a transaction."""
        self.in_transaction = True
        self.transaction_log.append("start")

    def tx_commit(self) -> None:
        """Commit the current transaction."""
        self.in_transaction = False
        self.transaction_log.append("commit")

    def tx_rollback(self) -> None:
        """Roll back the current transaction."""
        self.in_transaction = False
        self.transaction_log.append("rollback")

    # -- test helpers -------------------------------------------------------

    def add_file(self, file_name: str, records: dict[str, str] | None = None) -> None:
        """Seed a file with records so `uopy.File` can read them."""
        self._files[file_name] = dict(records or {})

    def set_missing_file(self, file_name: str) -> None:
        """Declare a file that should fail to open."""
        self._missing_files.add(file_name)

    def set_command_responses(self, responses: dict[str, str]) -> None:
        """Set substring-matched canned responses for TCL commands."""
        self.command_responses = responses

    def set_select_list(self, record_ids: list[str]) -> None:
        """Set the record ids a `uopy.List` will hand back."""
        self.select_list = list(record_ids)
