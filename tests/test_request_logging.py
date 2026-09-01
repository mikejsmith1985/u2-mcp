"""The request log records what happened, never what was in it.

This exists because the middleware it covers used to capture the response body
of `/token` and `/register` and write it to the log at INFO: access tokens,
refresh tokens and client secrets, in cleartext, on the production HTTP path,
with no flag to turn it off.

The README says credentials are never logged. The commit that hashed tokens in
storage was titled "stop storing tokens in the clear" while this wrote them to
the log on the way out — which made the log an easier place to steal them from
than the database had ever been.

These tests read the shipped source rather than exercising the middleware,
because the failure is the presence of the code, not a behaviour that only
appears under a particular request. A body that is never captured cannot be
logged by any path, and that is the property worth holding.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

SERVER_SOURCE = Path(__file__).resolve().parent.parent / "src" / "u2_mcp" / "server.py"


@pytest.fixture(scope="module")
def middleware_source() -> str:
    """The request-logging middleware, as shipped."""
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    start = source.index("class RequestLoggingMiddleware")
    end = source.index("app.add_middleware(RequestLoggingMiddleware)", start)

    return source[start:end]


def test_the_middleware_never_reads_a_response_body(middleware_source: str) -> None:
    """A body that is never read cannot be logged, whatever the log line says."""
    assert "body_iterator" not in middleware_source
    assert ".body" not in middleware_source


def test_the_middleware_never_logs_a_body_or_headers(middleware_source: str) -> None:
    # The specific shapes the old code used. Named individually so a failure
    # says which one came back rather than only that something did.
    assert "body=" not in middleware_source
    assert "headers=" not in middleware_source
    assert "dict(response.headers)" not in middleware_source


def test_the_token_and_registration_endpoints_are_not_singled_out(
    middleware_source: str,
) -> None:
    """The old code branched on these paths in order to capture them."""
    for path in ('"/token"', '"/register"', '"/.well-known/'):
        assert path not in middleware_source, (
            f"The logger names {path}. The only reason to name an OAuth endpoint "
            "in a logger is to treat its contents specially."
        )


def test_the_authorization_header_value_is_never_logged(middleware_source: str) -> None:
    """Presence, not content.

    The previous version logged the first twenty characters of the header,
    which is still twenty characters of a bearer token.
    """
    assert "auth_header[:20]" not in middleware_source
    assert 'request.headers.get("authorization"' not in middleware_source

    # Whether one was sent is the useful, safe fact, and is what is logged.
    assert '"authorization" in request.headers' in middleware_source


def test_no_logging_call_anywhere_interpolates_a_token(middleware_source: str) -> None:
    """A sweep, in case a later change reintroduces this by another name."""
    forbidden = re.compile(
        r"(access_token|refresh_token|client_secret|authorization\s*=\s*auth_header)",
        re.IGNORECASE,
    )

    assert not forbidden.search(middleware_source)


def test_the_middleware_still_logs_the_request_and_its_outcome() -> None:
    """Removing the leak must not remove the log.

    A request log that records nothing is not safer, it is useless -- and the
    audit story this server tells depends on being able to say what was asked.
    """
    from u2_mcp import server

    source = inspect.getsource(server)
    start = source.index("class RequestLoggingMiddleware")
    end = source.index("app.add_middleware(RequestLoggingMiddleware)", start)
    middleware = source[start:end]

    assert "REQUEST:" in middleware
    assert "RESPONSE:" in middleware
    assert "request.method" in middleware
    assert "response.status_code" in middleware
