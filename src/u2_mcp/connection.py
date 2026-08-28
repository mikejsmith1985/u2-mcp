"""Connection management for Universe/UniData databases."""

import logging
import socket
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import TYPE_CHECKING, Any

# Workaround for uopy bug on macOS - TCP_KEEPIDLE doesn't exist on macOS
if not hasattr(socket, "TCP_KEEPIDLE"):
    socket.TCP_KEEPIDLE = socket.TCP_KEEPALIVE  # type: ignore[attr-defined]

from .config import U2Config
from .credentials import U2Credentials
from .driver import get_driver

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _uopy() -> ModuleType:
    """Return the configured driver.

    Resolved per call rather than captured at import time, so configuration can
    change between tests without the module needing to be reloaded.
    """
    return get_driver()


# MultiValue delimiters, named so the sanitizer reads as intent rather than magic numbers.
AM = chr(254)  # Attribute mark - separates fields
VM = chr(253)  # Value mark - separates values within a field
SM = chr(252)  # Subvalue mark - separates subvalues within a value

# Unicode categories that must never reach a user's screen: control codes,
# formatting marks, surrogates, private-use and unassigned code points.
_NON_DISPLAYABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


@dataclass
class ConnectionInfo:
    """Information about an active database connection."""

    name: str
    host: str
    account: str
    service: str
    connected_at: datetime
    is_active: bool = True


@dataclass
class TransactionState:
    """Tracks the current transaction state."""

    in_transaction: bool = False
    started_at: datetime | None = None


class ConnectionError(Exception):
    """Raised when a database connection fails."""

    pass


class FileNotFoundError(Exception):
    """Raised when a Universe file cannot be opened."""

    pass


class ConnectionManager:
    """Manages connections to Universe/UniData servers.

    Provides connection lifecycle management, auto-reconnect capability,
    file access, and transaction state tracking.

    Args:
        config: U2Config instance with connection parameters
    """

    def __init__(self, config: U2Config, credentials: "U2Credentials | None" = None) -> None:
        self._config = config
        # Credentials may belong to one person rather than the whole server, so the
        # database can enforce its own security against the real caller.
        self._credentials = credentials or U2Credentials(
            user=config.user,
            password=config.password,
            account=config.account,
            is_shared=True,
        )
        self._session: Any | None = None  # uopy.Session
        self._connections: dict[str, ConnectionInfo] = {}
        self._default_connection: str = "default"
        self._transaction = TransactionState()
        self._open_files: dict[str, Any] = {}  # Cache of open file handles
        # One conversation per session: commands must not interleave on the wire.
        self._command_lock = threading.RLock()
        # Establishing a session is serialized separately, so a burst of callers
        # arriving to find no session opens one connection rather than one each.
        self._session_lock = threading.RLock()
        self._abandoned_queries = 0

    @property
    def config(self) -> U2Config:
        """Return the configuration object."""
        return self._config

    @property
    def credentials(self) -> U2Credentials:
        """Return the database login this manager connects with."""
        return self._credentials

    @property
    def in_transaction(self) -> bool:
        """Return whether a transaction is currently active."""
        return self._transaction.in_transaction

    def connect(self, name: str = "default") -> ConnectionInfo:
        """Establish a connection to the Universe/UniData server.

        Args:
            name: Connection name for reference (supports multiple connections)

        Returns:
            ConnectionInfo with connection details

        Raises:
            ConnectionError: If connection fails
        """
        if name in self._connections and self._connections[name].is_active:
            logger.info(f"Reusing existing connection '{name}'")
            return self._connections[name]

        try:
            logger.info(
                f"Connecting to {self._config.host}/{self._credentials.account} "
                f"as {self._credentials.user}"
            )

            self._session = _uopy().connect(
                host=self._config.host,
                user=self._credentials.user,
                password=self._credentials.password,
                account=self._credentials.account,
                service=self._config.service,
            )

            info = ConnectionInfo(
                name=name,
                host=self._config.host,
                account=self._credentials.account,
                service=self._config.service,
                connected_at=datetime.now(),
                is_active=True,
            )
            self._connections[name] = info

            logger.info(f"Connected successfully to {self._credentials.account}")
            return info

        except _uopy().UOError as e:
            logger.error(f"Connection failed: {e}")
            raise ConnectionError(f"Failed to connect to {self._config.host}: {e}") from e

    def disconnect(self, name: str = "default") -> bool:
        """Close a named connection.

        Args:
            name: Name of the connection to close

        Returns:
            True if connection was closed, False if not found
        """
        if name not in self._connections:
            return False

        try:
            # Close any open files first
            self._open_files.clear()

            if self._session:
                self._session.close()
                self._session = None

            self._connections[name].is_active = False
            del self._connections[name]

            # Reset transaction state
            self._transaction = TransactionState()

            logger.info(f"Disconnected connection '{name}'")
            return True

        except _uopy().UOError as e:
            logger.warning(f"Error during disconnect: {e}")
            return False

    def disconnect_all(self) -> int:
        """Close all connections.

        Returns:
            Count of closed connections
        """
        names = list(self._connections.keys())
        count = 0
        for name in names:
            if self.disconnect(name):
                count += 1
        return count

    def list_connections(self) -> dict[str, ConnectionInfo]:
        """Return all active connections."""
        return {k: v for k, v in self._connections.items() if v.is_active}

    def _discard_connection(self, name: str) -> None:
        """Forget a connection so the next connect() genuinely opens a new session.

        Without this, connect() sees a connection record still marked active and
        returns early, leaving the caller holding a dead (or absent) session.

        Args:
            name: Name of the connection record to discard
        """
        self._session = None
        self._open_files.clear()
        self._connections.pop(name, None)
        self._transaction = TransactionState()

    def get_session(self) -> Any:
        """Get the active uopy session, auto-reconnecting if necessary.

        Establishing a session is serialized: when a burst of requests arrives to
        find no session, every one of them would otherwise open its own. Those
        duplicates leak a database connection each and stay invisible until the
        server runs out of them.

        Returns:
            Active uopy.Session object

        Raises:
            ConnectionError: If reconnection fails
        """
        name = self._default_connection

        with self._session_lock:
            return self._establish_session(name)

    def _establish_session(self, name: str) -> Any:
        """Return a live session for this login, reconnecting if needed.

        Args:
            name: Connection record name

        Returns:
            Active uopy.Session object

        Raises:
            ConnectionError: If a session cannot be established
        """
        if self._session is None:
            self._discard_connection(name)
            self.connect(name)

        # Verify the connection is still alive before handing it to a caller.
        session = self._session
        try:
            is_alive = session is not None and bool(session.is_active)
        except (_uopy().UOError, AttributeError):
            is_alive = False

        if not is_alive:
            logger.warning("Connection lost, attempting reconnect")
            self._discard_connection(name)
            self.connect(name)

        if self._session is None:
            raise ConnectionError(f"Failed to establish a session to {self._config.host}")

        return self._session

    def open_file(self, file_name: str) -> Any:
        """Open a Universe file.

        File handles are cached for efficiency.

        Args:
            file_name: Name of the file to open

        Returns:
            _uopy().File object

        Raises:
            FileNotFoundError: If file cannot be opened
        """
        # Return cached handle if available
        if file_name in self._open_files:
            return self._open_files[file_name]

        session = self.get_session()
        try:
            file_handle = _uopy().File(file_name, session=session)
            self._open_files[file_name] = file_handle
            return file_handle
        except _uopy().UOError as e:
            raise FileNotFoundError(f"Cannot open file '{file_name}': {e}") from e

    def close_file(self, file_name: str) -> bool:
        """Close a cached file handle.

        Args:
            file_name: Name of the file to close

        Returns:
            True if file was closed, False if not found
        """
        if file_name in self._open_files:
            del self._open_files[file_name]
            return True
        return False

    @property
    def abandoned_query_count(self) -> int:
        """Return how many queries have been abandoned to a timeout.

        Abandoned work is worth surfacing: a rising count means queries are
        routinely outrunning the timeout and the limit or the queries need review.
        """
        return self._abandoned_queries

    def execute_command(self, command_text: str, timeout: float | None = None) -> str:
        """Execute a TCL command and return the response.

        A uopy session is a single conversation with the server, so commands are
        serialized: two at once on one session would interleave and corrupt each
        other's replies.

        Args:
            command_text: TCL command to execute
            timeout: Optional timeout in seconds (defaults to config.query_timeout)

        Returns:
            Command response string (sanitized for display)

        Raises:
            TimeoutError: If command exceeds timeout
        """
        effective_timeout = timeout if timeout is not None else self._config.query_timeout

        with self._command_lock:
            session = self.get_session()
            result, completed = self._start_command(command_text, session)

            if completed.wait(timeout=effective_timeout):
                if result["error"]:
                    raise result["error"]
                return self._sanitize_output(result["response"] or "")

            self._abandon_command(command_text, effective_timeout)
            raise TimeoutError(
                f"Query exceeded {effective_timeout} second timeout and was abandoned. "
                "The database session was closed to stop the work; the next request will "
                "reconnect. Consider adding a SAMPLE clause or narrowing the selection "
                "criteria."
            )

    def _start_command(self, command_text: str, session: Any) -> tuple[dict[str, Any], Any]:
        """Run a command on a worker thread so the caller can time it out.

        Args:
            command_text: TCL command to execute
            session: The uopy session to run it on

        Returns:
            Tuple of (result holder, completion event)
        """
        import threading

        result: dict[str, Any] = {"response": None, "error": None}
        completed = threading.Event()

        def run_command() -> None:
            try:
                cmd = _uopy().Command(command_text, session=session)
                cmd.run()
                result["response"] = str(cmd.response) if cmd.response else ""
            except Exception as e:
                result["error"] = e
            finally:
                completed.set()

        threading.Thread(target=run_command, daemon=True).start()
        return result, completed

    def _abandon_command(self, command_text: str, effective_timeout: float) -> None:
        """Stop a query that outran its timeout.

        uopy gives no way to cancel a command in flight, so returning an error
        would leave the query running on the server and the thread alive against a
        session another request would reuse. Closing the session is what actually
        stops the work: the socket drops and the server abandons the command.

        Args:
            command_text: The command being abandoned, for the log
            effective_timeout: The timeout it exceeded, for the log
        """
        self._abandoned_queries += 1
        logger.error(
            f"Query timed out after {effective_timeout}s and was abandoned "
            f"(total abandoned: {self._abandoned_queries}): {command_text[:100]}"
        )
        self.force_disconnect()

    def _sanitize_output(self, text: str) -> str:
        """Clean up Universe output for display.

        Replaces MultiValue delimiters with readable separators and strips
        terminal control codes. Real business data -- accented customer names,
        currency symbols -- is preserved: filtering by Unicode category rather
        than by "is it plain ASCII" is what keeps MULLER from losing its umlaut.

        Args:
            text: Raw output from Universe

        Returns:
            Cleaned text suitable for JSON/display
        """
        # Normalize terminal line endings and page breaks to plain newlines.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\f", "\n")

        # Convert MultiValue delimiters to readable text
        # AM (chr 254) = field/attribute separator -> newline
        # VM (chr 253) = multivalue separator -> pipe
        # SM (chr 252) = subvalue separator -> semicolon
        text = text.replace(AM, "\n")
        text = text.replace(VM, " | ")
        text = text.replace(SM, " ; ")

        return "".join(char for char in text if self._is_displayable(char))

    @staticmethod
    def _is_displayable(char: str) -> bool:
        """Return whether a character is safe to show to a user.

        Keeps newlines and tabs, drops the control, format, surrogate and
        private-use categories, and keeps every ordinary printable character
        regardless of alphabet.

        Args:
            char: A single character

        Returns:
            True if the character should survive sanitization
        """
        if char in ("\n", "\t"):
            return True
        return unicodedata.category(char) not in _NON_DISPLAYABLE_CATEGORIES

    def create_select_list(self) -> Any:
        """Create a new select list object.

        Returns:
            _uopy().List object for select operations
        """
        session = self.get_session()
        return _uopy().List(session=session)

    def health_check(self) -> bool:
        """Perform a quick health check on the connection.

        Executes a minimal TCL command to verify the connection is responsive.

        Returns:
            True if connection is healthy, False otherwise
        """
        if self._session is None:
            return True  # No connection to check

        try:
            # Use a minimal command that should return quickly
            cmd = _uopy().Command("WHO", session=self._session)
            cmd.run()
            return True
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False

    def force_disconnect(self) -> None:
        """Force disconnect the current connection without cleanup.

        Used by the watchdog to reset a hung connection.
        """
        import contextlib

        logger.warning("Force disconnecting database connection")
        try:
            if self._session:
                with contextlib.suppress(Exception):
                    self._session.close()
                self._session = None

            # Clear all cached state
            self._open_files.clear()
            self._connections.clear()
            self._transaction = TransactionState()

        except Exception as e:
            logger.error(f"Error during force disconnect: {e}")
        finally:
            # Ensure session is cleared even if errors occur
            self._session = None

    def begin_transaction(self) -> bool:
        """Begin a database transaction.

        Returns:
            True if transaction started successfully

        Raises:
            RuntimeError: If already in a transaction
        """
        if self._transaction.in_transaction:
            raise RuntimeError("Transaction already in progress")

        session = self.get_session()
        try:
            session.tx_start()
            self._transaction.in_transaction = True
            self._transaction.started_at = datetime.now()
            logger.info("Transaction started")
            return True
        except _uopy().UOError as e:
            logger.error(f"Failed to start transaction: {e}")
            raise

    def commit_transaction(self) -> bool:
        """Commit the current transaction.

        Returns:
            True if committed successfully

        Raises:
            RuntimeError: If not in a transaction
        """
        if not self._transaction.in_transaction:
            raise RuntimeError("No transaction in progress")

        session = self.get_session()
        try:
            session.tx_commit()
            self._transaction = TransactionState()
            logger.info("Transaction committed")
            return True
        except _uopy().UOError as e:
            logger.error(f"Failed to commit transaction: {e}")
            raise

    def rollback_transaction(self) -> bool:
        """Rollback the current transaction.

        Returns:
            True if rolled back successfully

        Raises:
            RuntimeError: If not in a transaction
        """
        if not self._transaction.in_transaction:
            raise RuntimeError("No transaction in progress")

        session = self.get_session()
        try:
            session.tx_rollback()
            self._transaction = TransactionState()
            logger.info("Transaction rolled back")
            return True
        except _uopy().UOError as e:
            logger.error(f"Failed to rollback transaction: {e}")
            raise
