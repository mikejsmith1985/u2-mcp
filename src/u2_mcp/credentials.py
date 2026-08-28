"""Which database login a request should use.

Upstream connected to Universe with one set of credentials from the environment,
whoever was asking. That is a defensible choice for a single-operator desktop
setup and an unsafe one for a shared deployment: the database's own file and
field security never sees the real person, so it cannot enforce anything about
them, and neither can anyone reading the logs afterwards.

Two resolvers are offered. `shared` preserves the upstream behaviour but says
plainly what it gives up. `mapped` gives each authenticated person their own
Universe login, and refuses anyone who has not been given one -- failing closed,
rather than quietly falling back to the shared account.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import U2Config
from .identity import CallerIdentity

logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """Raised when a caller has no usable database credentials."""

    pass


@dataclass(frozen=True)
class U2Credentials:
    """A database login for one request.

    Attributes:
        user: Universe/UniData username
        password: Password for that account
        account: Universe account to connect to
        is_shared: True when this login is used by more than one person, which
            means the database cannot tell those people apart
    """

    user: str
    password: str
    account: str
    is_shared: bool

    @property
    def pool_key(self) -> str:
        """Return the key that keeps one login's session apart from another's."""
        return f"{self.user}@{self.account}"


class CredentialResolver(Protocol):
    """Turns a caller into the database login their request should use."""

    def resolve(self, caller: CallerIdentity) -> U2Credentials:
        """Return the credentials for this caller.

        Args:
            caller: Who is making the request

        Returns:
            The database login to use

        Raises:
            CredentialError: If this caller may not reach the database
        """
        ...


class SharedCredentialResolver:
    """Give every caller the one account configured for the server.

    This is the upstream behaviour. It is kept as the default so existing
    deployments are unaffected, but it now reports itself as shared so the audit
    trail can record that the database could not distinguish callers.

    Args:
        config: Configuration holding the server's own database credentials
    """

    def __init__(self, config: U2Config) -> None:
        self._credentials = U2Credentials(
            user=config.user,
            password=config.password,
            account=config.account,
            is_shared=True,
        )

    def resolve(self, caller: CallerIdentity) -> U2Credentials:
        """Return the single configured login, whoever is asking."""
        return self._credentials


class MappedCredentialResolver:
    """Give each authenticated person their own Universe login.

    The map is a JSON object keyed by the identity provider's subject (or the
    caller's email), each entry naming a `user`, `password` and optionally an
    `account`. A caller with no entry is refused: falling back to a shared login
    would undo the point of mapping in the first place.

    Args:
        config: Configuration supplying the default account
        map_path: Path to the JSON credential map

    Raises:
        CredentialError: If the map is missing or cannot be read
    """

    def __init__(self, config: U2Config, map_path: Path | str) -> None:
        self._default_account = config.account
        self._map_path = Path(map_path).expanduser()
        self._entries = self._load(self._map_path)
        logger.info(
            f"Per-user database credentials loaded for {len(self._entries)} callers "
            f"from {self._map_path}"
        )

    @staticmethod
    def _load(map_path: Path) -> dict[str, dict[str, str]]:
        """Read and validate the credential map.

        Args:
            map_path: Path to the JSON file

        Returns:
            Mapping of caller key to credential fields

        Raises:
            CredentialError: If the file is missing, unreadable, or malformed
        """
        if not map_path.exists():
            raise CredentialError(
                f"Credential map not found at {map_path}. Set U2_CREDENTIAL_MAP_PATH, or "
                "use U2_IDENTITY_MODE=shared to connect with one account for everyone."
            )
        try:
            loaded = json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError(f"Could not read credential map {map_path}: {exc}") from exc

        if not isinstance(loaded, dict):
            raise CredentialError(
                f"Credential map {map_path} must be a JSON object keyed by user subject"
            )
        return loaded

    def resolve(self, caller: CallerIdentity) -> U2Credentials:
        """Return this caller's own database login.

        Raises:
            CredentialError: If the caller has no entry in the map
        """
        entry = self._entries.get(caller.subject)
        if entry is None and caller.email:
            entry = self._entries.get(caller.email)

        if entry is None:
            raise CredentialError(
                f"No database credentials are mapped for '{caller.subject}'. Add an entry "
                f"to {self._map_path} to grant this person access."
            )

        missing = [key for key in ("user", "password") if not entry.get(key)]
        if missing:
            raise CredentialError(
                f"Credential map entry for '{caller.subject}' is missing: {', '.join(missing)}"
            )

        return U2Credentials(
            user=entry["user"],
            password=entry["password"],
            account=entry.get("account") or self._default_account,
            is_shared=False,
        )


def create_credential_resolver(config: U2Config) -> CredentialResolver:
    """Build the credential resolver named by configuration.

    Args:
        config: Configuration carrying `identity_mode` and the credential map path

    Returns:
        The resolver to use for every request

    Raises:
        CredentialError: If the configured mode is not recognised
    """
    mode = (config.identity_mode or "shared").strip().lower()

    if mode == "shared":
        logger.warning(
            "Every request will reach the database as '%s'. The database cannot tell "
            "callers apart, so its own security cannot act on them. Set "
            "U2_IDENTITY_MODE=mapped to give each person their own login.",
            config.user,
        )
        return SharedCredentialResolver(config)

    if mode == "mapped":
        return MappedCredentialResolver(config, config.credential_map_path)

    raise CredentialError(
        f"Unknown U2_IDENTITY_MODE '{config.identity_mode}'. Use 'shared' or 'mapped'."
    )
