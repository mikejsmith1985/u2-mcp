"""Command validation and safety controls for u2-mcp.

The validator is the only thing between a model-generated string and a
production database, so it is treated as a security boundary rather than a
convenience. Two principles govern it:

* **Queries use an allowlist.** Only read verbs are accepted, so a verb nobody
  anticipated is refused rather than permitted.
* **TCL uses a blocklist, so the blocklist must cover the escapes.** TCL can
  reach the operating system, switch accounts, and compile and run code. A list
  naming only the obviously destructive file verbs leaves the host exposed, so
  every escape route is enumerated below with the reason it is there.
"""

import re

# Commands that leave the database and reach the operating system, another
# account, or the compiler. Any one of these turns database access into control
# of the host, so they are blocked even when write operations are permitted.
ESCAPE_COMMANDS: set[str] = {
    # Shell and operating-system escapes
    "SH",
    "SHELL",
    "DOS",
    "OSDELETE",
    "OSWRITE",
    "OSREAD",
    "OSCOPY",
    "OSBREAD",
    "OSBWRITE",
    "OSOPEN",
    "OSEXECUTE",
    # Compiling or running arbitrary code
    "RUN",
    "BASIC",
    "CATALOG",
    "COMPILE",
    "PHANTOM",
    "EXECUTE",
    # Leaving the configured account, which escapes every other control here
    "LOGTO",
    "LOGIN",
    "LOGOUT",
    # Environment and system state
    "SETPTR",
    "CLEAR.LOCKS",
    "CLEARLOCKS",
    "SUPERCLEAR",
    "UNLOCK",
    "SET.SQL",
    "CONFIGURE",
}

# SQL verbs that Universe also accepts. The query allowlist already excludes
# them; TCL would not, without naming them here.
DESTRUCTIVE_SQL_COMMANDS: set[str] = {
    "DROP",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "INSERT",
    "UPDATE",
    "CREATE",
    "MERGE",
}

# Commands that destroy or restructure files.
DESTRUCTIVE_FILE_COMMANDS: set[str] = {
    "DELETE.FILE",
    "DELETE-FILE",
    "CLEAR.FILE",
    "CLEAR-FILE",
    "CLEARFILE",
    "CLEARDATA",
    "CNAME",
    "CREATE.FILE",
    "CREATE-FILE",
    "RESIZE",
    "ACCOUNT.RESTORE",
    "ACCOUNT.SAVE",
    "T.LOAD",
    "T.DUMP",
}

# Interactive editors, which expect a terminal this server does not have and can
# rewrite any record they open.
EDITOR_COMMANDS: set[str] = {"ED", "SED", "AE", "XED", "EDIT"}

# The full default blocklist.
DEFAULT_BLOCKED_COMMANDS: set[str] = (
    ESCAPE_COMMANDS | DESTRUCTIVE_SQL_COMMANDS | DESTRUCTIVE_FILE_COMMANDS | EDITOR_COMMANDS
)

# Characters that begin a shell escape rather than a command word.
ESCAPE_PREFIXES: tuple[str, ...] = ("!", "$", "|", "`", ">", "<", "&", ";")

# The characters a TCL verb is made of. Matching the leading run of these -- and
# stopping at the first character that is not one -- means punctuation attached
# to a verb cannot disguise it: `basic:` is recognised as BASIC.
_LEADING_VERB = re.compile(r"[A-Za-z0-9._-]+")

# Commands that modify data (blocked in read-only mode)
WRITE_COMMANDS: set[str] = {
    "DELETE",
    "COPY",
    "CNAME",
    "ED",
    "SED",
    "AE",
    "REFORMAT",
    "T.DUMP",
    "T.LOAD",
    "ACCOUNT.RESTORE",
    "CLEARFILE",
}

# Allowed query commands (RetrieVe/UniQuery read operations)
ALLOWED_QUERY_COMMANDS: set[str] = {
    "LIST",
    "SELECT",
    "SSELECT",
    "SORT",
    "COUNT",
    "SUM",
    "GET.LIST",
    "QSELECT",
    "SEARCH",
}


class CommandValidator:
    """Validates TCL commands against blocklist and safety rules.

    Args:
        blocked_commands: List of TCL commands to block
        read_only: If True, also block write operations
    """

    def __init__(self, blocked_commands: list[str], read_only: bool = False) -> None:
        self._blocked: set[str] = {cmd.upper() for cmd in blocked_commands}
        self._blocked.update(DEFAULT_BLOCKED_COMMANDS)
        self._read_only = read_only

    @staticmethod
    def _first_word(command: str) -> str:
        """Return the leading verb of a command, normalized for comparison.

        Three things must not change a verdict: leading whitespace, case, and
        punctuation attached to the verb. `basic:` is the command `BASIC`, and
        splitting on whitespace alone would read it as the unrecognised token
        `BASIC:` and let it through. A command beginning with a shell
        metacharacter has no verb at all -- `!ls` is a shell escape.

        Args:
            command: The raw command string

        Returns:
            The upper-case verb, the escape character, or "" if there is no verb
        """
        stripped = command.strip()
        if not stripped:
            return ""
        if stripped.startswith(ESCAPE_PREFIXES):
            return stripped[0]

        match = _LEADING_VERB.match(stripped)
        return match.group(0).upper() if match else ""

    def validate(self, command: str) -> tuple[bool, str]:
        """Validate a TCL command.

        Args:
            command: The TCL command string to validate

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is empty.
        """
        if not command or not command.strip():
            return False, "Command cannot be empty"

        first_word = self._first_word(command)

        if first_word in ESCAPE_PREFIXES:
            return (
                False,
                f"Commands beginning with '{first_word}' are blocked: they escape to the "
                "operating system rather than running a database command",
            )

        if first_word in ESCAPE_COMMANDS:
            return (
                False,
                f"Command '{first_word}' is blocked: it can reach the operating system, "
                "another account, or the compiler",
            )

        if first_word in self._blocked:
            return False, f"Command '{first_word}' is blocked for safety"

        if self._read_only and first_word in WRITE_COMMANDS:
            return False, f"Command '{first_word}' not allowed in read-only mode"

        return True, ""

    def is_query_safe(self, query: str) -> tuple[bool, str]:
        """Validate a RetrieVe/UniQuery statement.

        Only allows read operations (LIST, SELECT, SORT, COUNT, etc.)

        Args:
            query: The query statement to validate

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is empty.
        """
        if not query or not query.strip():
            return False, "Query cannot be empty"

        first_word = self._first_word(query)

        if first_word not in ALLOWED_QUERY_COMMANDS:
            allowed_list = ", ".join(sorted(ALLOWED_QUERY_COMMANDS))
            return False, f"Query command '{first_word}' not allowed. Allowed: {allowed_list}"

        return True, ""

    def is_blocked(self, command: str) -> bool:
        """Check if a command is in the blocklist.

        Args:
            command: Command name to check

        Returns:
            True if command is blocked
        """
        return command.upper() in self._blocked

    @property
    def read_only(self) -> bool:
        """Return whether read-only mode is enabled."""
        return self._read_only
