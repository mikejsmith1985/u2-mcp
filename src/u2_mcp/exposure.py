"""Refuse to expose an unauthenticated database to the network.

The legacy HTTP/SSE transport has no authentication of any kind: every tool,
including those that write records and run system commands, is reachable by
anyone who can open the port. Upstream defaulted that transport to every network
interface with `allow_credentials` and a wildcard CORS origin, which turns a
misplaced `--http` into unauthenticated access to a production database.

Two rules are enforced here, at startup rather than on the first request:

* An unauthenticated server may listen only on loopback, unless an operator
  overrides that deliberately.
* Credentialed requests may not be accepted from any origin.

Both fail closed, and both name the specific settings that resolve them.
"""

from __future__ import annotations

import ipaddress
import logging

from .config import U2Config

logger = logging.getLogger(__name__)

# Host names that resolve to this machine and cannot be reached from elsewhere.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})

# Transports that open a network port. stdio does not.
_NETWORK_TRANSPORTS = frozenset({"sse", "streamable-http"})


class ExposureError(Exception):
    """Raised when a configuration would expose the database unsafely."""

    pass


def is_loopback(address: str) -> bool:
    """Return whether a bind address is reachable only from this machine.

    Args:
        address: The configured bind address or host name

    Returns:
        True if traffic to this address cannot originate on another machine
    """
    if not address:
        return False

    candidate = address.strip().strip("[]").lower()
    if candidate in _LOOPBACK_NAMES:
        return True

    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # An unresolvable name might point anywhere, so treat it as reachable.
        return False


def check_network_exposure(config: U2Config, transport: str) -> None:
    """Refuse to start a server whose configuration exposes the database unsafely.

    Args:
        config: The configuration the server is about to run with
        transport: 'stdio', 'sse' or 'streamable-http'

    Raises:
        ExposureError: If the configuration would expose the database unsafely
    """
    if transport not in _NETWORK_TRANSPORTS:
        return

    _check_unauthenticated_reach(config, transport)
    _check_credentialed_wildcard_cors(config)


def _check_unauthenticated_reach(config: U2Config, transport: str) -> None:
    """Refuse an unauthenticated server that listens beyond this machine.

    Raises:
        ExposureError: If the server would be reachable and unauthenticated
    """
    if config.auth_enabled or is_loopback(config.http_host):
        return

    if config.allow_unauthenticated_network_access:
        logger.warning(
            "Serving unauthenticated MCP on %s:%s. Every tool, including record "
            "writes and system commands, is available to anyone who can reach this "
            "port. This was permitted explicitly by "
            "U2_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS.",
            config.http_host,
            config.http_port,
        )
        return

    raise ExposureError(
        f"Refusing to serve unauthenticated MCP on {config.http_host}:{config.http_port}. "
        f"The {transport} transport performs no authentication, so every tool -- including "
        "record writes and system commands -- would be available to anyone who can reach "
        "this port.\n"
        "Choose one:\n"
        "  - Listen locally only:      U2_HTTP_HOST=127.0.0.1\n"
        "  - Require sign-in:          U2_AUTH_ENABLED=true (with --streamable-http)\n"
        "  - Accept the risk on purpose: U2_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS=true"
    )


def _check_credentialed_wildcard_cors(config: U2Config) -> None:
    """Refuse to accept credentialed requests from any origin.

    The server sends `allow_credentials`, so a wildcard origin would let any web
    page a signed-in user visits drive this server with their session.

    Raises:
        ExposureError: If credentials would be accepted from every origin
    """
    if "*" not in config.http_cors_origins:
        return

    raise ExposureError(
        "Refusing to accept credentialed requests from any origin. With "
        "U2_HTTP_CORS_ORIGINS='*', any web page a signed-in user visits could drive this "
        "server using their session.\n"
        "Name the origins that may connect instead, for example:\n"
        "  U2_HTTP_CORS_ORIGINS=https://claude.ai"
    )
