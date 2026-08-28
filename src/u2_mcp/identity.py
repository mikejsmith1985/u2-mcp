"""Who is calling: the authenticated person behind an MCP request.

Signing in to this server and being known to the database are two different
things. Upstream had the first without the second: an operator could prove who
they were to the MCP server, and every query still arrived at Universe under one
shared account, with an audit trail that recorded a per-process session id rather
than a name.

This module carries the caller through a request so the rest of the server can
answer "who asked for this?" -- for the audit trail, and for choosing which
database credentials to use.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallerIdentity:
    """The person or client behind the current request.

    Attributes:
        subject: Stable identifier from the identity provider
        email: Email address, when the provider supplied one
        name: Human-readable name, when the provider supplied one
        client_id: The OAuth client that presented the token
        scopes: Scopes granted to this caller
        is_authenticated: False for local stdio use, where no OAuth takes place
    """

    subject: str
    email: str | None = None
    name: str | None = None
    client_id: str | None = None
    scopes: tuple[str, ...] = ()
    is_authenticated: bool = True
    claims: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def identity_key(self) -> str:
        """Return the key used to keep one caller's resources apart from another's."""
        return self.subject

    @property
    def display_name(self) -> str:
        """Return the most human-readable label available for this caller."""
        return self.name or self.email or self.subject

    def for_audit(self) -> dict[str, Any]:
        """Return the fields an audit record should carry about this caller."""
        return {
            "user": self.subject,
            "user_name": self.display_name,
            "user_email": self.email,
            "client_id": self.client_id,
            "authenticated": self.is_authenticated,
        }


# Stdio mode has no OAuth: the caller is whoever started the process.
LOCAL_CALLER = CallerIdentity(
    subject="local",
    name="local operator",
    is_authenticated=False,
)

# Set by the HTTP server, which knows how to turn a bearer token into a person.
_identity_resolver: Callable[[], CallerIdentity | None] | None = None

# Set for the duration of one request, or by tests.
_caller_var: contextvars.ContextVar[CallerIdentity | None] = contextvars.ContextVar(
    "u2_caller", default=None
)


def set_identity_resolver(resolver: Callable[[], CallerIdentity | None] | None) -> None:
    """Install the function that identifies the caller of the current request.

    Args:
        resolver: Returns the authenticated caller, or None when there is none.
            Pass None to remove the resolver.
    """
    global _identity_resolver
    _identity_resolver = resolver


def current_caller() -> CallerIdentity:
    """Return the caller behind the current request.

    Falls back to the local operator when no authentication is in play, which is
    the normal case for stdio mode.

    Returns:
        The current CallerIdentity, never None
    """
    scoped = _caller_var.get()
    if scoped is not None:
        return scoped

    if _identity_resolver is not None:
        try:
            resolved = _identity_resolver()
        except Exception as exc:  # noqa: BLE001 - identity must never break a request
            logger.warning(f"Could not identify caller: {exc}")
            resolved = None
        if resolved is not None:
            return resolved

    return LOCAL_CALLER


@contextlib.contextmanager
def use_caller(caller: CallerIdentity) -> Iterator[CallerIdentity]:
    """Treat the given identity as the caller for the duration of the block.

    Args:
        caller: The identity to install

    Yields:
        The installed identity
    """
    token = _caller_var.set(caller)
    try:
        yield caller
    finally:
        _caller_var.reset(token)


def caller_from_stored_token(stored: Any) -> CallerIdentity:
    """Build a caller from a token record issued by this server's OAuth provider.

    Args:
        stored: A StoredToken carrying the subject and claims from the identity provider

    Returns:
        The caller that token was issued to
    """
    claims = dict(getattr(stored, "user_claims", {}) or {})
    return CallerIdentity(
        subject=stored.user_subject,
        email=claims.get("email"),
        name=claims.get("name") or claims.get("preferred_username"),
        client_id=getattr(stored, "client_id", None),
        scopes=tuple((getattr(stored, "scope", "") or "").split()),
        is_authenticated=True,
        claims=claims,
    )
