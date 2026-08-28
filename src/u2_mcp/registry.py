"""One database connection per login, chosen by who is calling.

Upstream held a single `ConnectionManager` for the whole process, so every
request reached Universe through the same session under the same account. That
is right for a desktop client with one operator and wrong for a shared server:
the database sees one user no matter who asked.

This registry keeps a `ConnectionManager` per database login and hands out the
one belonging to the current caller. In the default shared mode every caller
resolves to the same login, so behaviour is unchanged and there is still just one
connection; in mapped mode each person gets their own session under their own
account, and the database's security applies to them.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from .config import U2Config
from .connection import ConnectionManager
from .credentials import CredentialResolver, SharedCredentialResolver
from .identity import CallerIdentity, current_caller

logger = logging.getLogger(__name__)


class ConnectionRegistry:
    """Holds one ConnectionManager per database login.

    Args:
        config: Server configuration
        resolver: Turns a caller into the login their request should use;
            defaults to the shared account, matching upstream behaviour
    """

    def __init__(self, config: U2Config, resolver: CredentialResolver | None = None) -> None:
        self._config = config
        self._resolver: CredentialResolver = resolver or SharedCredentialResolver(config)
        # Ordered by least-recently-used, so the eviction victim is obvious.
        self._managers: OrderedDict[str, ConnectionManager] = OrderedDict()
        self._max_connections = max(1, int(getattr(config, "max_connections", 25)))
        self._lock = threading.RLock()

    @property
    def resolver(self) -> CredentialResolver:
        """Return the credential resolver in use."""
        return self._resolver

    def for_caller(self, caller: CallerIdentity) -> ConnectionManager:
        """Return the connection manager for this caller's database login.

        Args:
            caller: Who is making the request

        Returns:
            The manager holding that login's session

        Raises:
            CredentialError: If the caller has no database credentials
        """
        credentials = self._resolver.resolve(caller)
        with self._lock:
            manager = self._managers.get(credentials.pool_key)
            if manager is not None:
                self._managers.move_to_end(credentials.pool_key)
                return manager

            evicted = self._take_evictable_managers()
            manager = ConnectionManager(self._config, credentials)
            self._managers[credentials.pool_key] = manager
            logger.info(
                f"Opened connection slot '{credentials.pool_key}' for {caller.display_name} "
                f"({len(self._managers)}/{self._max_connections} in use)"
            )

        # Closing happens outside the lock. The connection being evicted may be
        # the one that stopped responding, and closing a wedged socket can block
        # for as long as the network timeout -- which would freeze every caller.
        self._close_evicted(evicted)
        return manager

    def _take_evictable_managers(self) -> list[tuple[str, ConnectionManager]]:
        """Remove least-recently-used slots to make room, without closing them.

        Without a ceiling, one connection per caller becomes one connection per
        caller *ever seen*, and a crowd of authenticated users could exhaust the
        database's own connection limit. Callers whose slot is evicted simply
        reconnect on their next request.

        Returns:
            The removed slots, for the caller to close outside the lock
        """
        evicted: list[tuple[str, ConnectionManager]] = []
        while len(self._managers) >= self._max_connections:
            evicted.append(self._managers.popitem(last=False))
        return evicted

    @staticmethod
    def _close_evicted(evicted: list[tuple[str, ConnectionManager]]) -> None:
        """Close connections already removed from the registry.

        Args:
            evicted: Slots removed by _take_evictable_managers
        """
        for key, manager in evicted:
            logger.info(f"Evicting least recently used connection slot '{key}'")
            try:
                manager.disconnect_all()
            except Exception as exc:  # noqa: BLE001 - eviction must not fail the request
                logger.warning(f"Error closing evicted connection '{key}': {exc}")

    def current(self) -> ConnectionManager:
        """Return the connection manager for the caller of the current request."""
        return self.for_caller(current_caller())

    def active_logins(self) -> list[str]:
        """Return the database logins that currently hold a connection slot."""
        with self._lock:
            return sorted(self._managers)

    def disconnect_all(self) -> int:
        """Close every connection this registry holds.

        Returns:
            Count of connections closed
        """
        with self._lock:
            closed = sum(manager.disconnect_all() for manager in self._managers.values())
            self._managers.clear()
            return closed

    def health_check(self) -> bool:
        """Report whether every held connection is responsive.

        Returns:
            True if all connections are healthy, or none are open
        """
        with self._lock:
            managers = list(self._managers.values())
        return all(manager.health_check() for manager in managers)

    def force_disconnect(self) -> None:
        """Force every held connection closed, for the watchdog to recover a hang."""
        with self._lock:
            managers = list(self._managers.values())
        for manager in managers:
            manager.force_disconnect()

    def get_stats(self) -> dict[str, Any]:
        """Return what the registry is holding, for operational visibility."""
        with self._lock:
            return {
                "logins": sorted(self._managers),
                "connection_count": len(self._managers),
                "abandoned_queries": sum(
                    manager.abandoned_query_count for manager in self._managers.values()
                ),
            }
