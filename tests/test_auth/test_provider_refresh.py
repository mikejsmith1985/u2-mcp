"""Tests for token rotation.

A refresh must carry the user's identity across to the new tokens. If it does
not, everything downstream that depends on knowing who is calling -- the audit
trail, and per-user database credentials -- quietly stops working the first time
a client refreshes, which is roughly an hour into every session.
"""

from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.provider import RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from u2_mcp.auth.provider import U2OAuthProvider
from u2_mcp.auth.sqlite_storage import SQLiteAuthStorage
from u2_mcp.auth.storage import InMemoryAuthStorage, StoredToken

ALICE_CLAIMS = {"email": "alice@example.com", "name": "Alice Nguyen"}


class StubIdP:
    """An identity provider that is never reached during a refresh."""

    async def get_authorization_url(self, *args: Any, **kwargs: Any) -> str:
        """Unused in these tests."""
        raise AssertionError("the identity provider must not be contacted on refresh")


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """Provide each backend, so rotation behaves the same on both."""
    if request.param == "memory":
        return InMemoryAuthStorage()
    return SQLiteAuthStorage(tmp_path / "auth.db")


@pytest.fixture
def provider(storage: Any) -> U2OAuthProvider:
    """An OAuth provider holding one issued refresh token for Alice."""
    provider = U2OAuthProvider(
        idp_adapter=StubIdP(),  # type: ignore[arg-type]
        issuer_url="https://u2-mcp.example.com",
        storage=storage,
    )
    storage.store_refresh_token(
        StoredToken(
            token="old-refresh",
            token_type="refresh",
            client_id="client-1",
            user_subject="auth0|alice",
            scope="u2:read",
            user_claims=ALICE_CLAIMS,
            resource="https://u2-mcp.example.com",
        )
    )
    return provider


@pytest.fixture
def client() -> OAuthClientInformationFull:
    """The registered client presenting the refresh token."""
    return OAuthClientInformationFull(
        client_id="client-1",
        client_secret="secret",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="u2:read",
    )


def presented_token() -> RefreshToken:
    """The refresh token as the MCP SDK hands it to the provider."""
    return RefreshToken(token="old-refresh", client_id="client-1", scopes=["u2:read"])


class TestRotationPreservesIdentity:
    """The point of a refresh is a new token for the same person."""

    @pytest.mark.asyncio
    async def test_refreshing_does_not_raise(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull
    ) -> None:
        """A refresh completes rather than failing on a missing attribute."""
        assert await provider.exchange_refresh_token(client, presented_token(), ["u2:read"])

    @pytest.mark.asyncio
    async def test_the_new_access_token_still_names_the_user(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull, storage: Any
    ) -> None:
        """Attribution survives rotation, so the audit trail keeps working."""
        issued = await provider.exchange_refresh_token(client, presented_token(), ["u2:read"])

        stored = storage.get_access_token(issued.access_token)
        assert stored is not None
        assert stored.user_subject == "auth0|alice"

    @pytest.mark.asyncio
    async def test_the_new_access_token_keeps_the_users_claims(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull, storage: Any
    ) -> None:
        """A readable name and email survive rotation too."""
        issued = await provider.exchange_refresh_token(client, presented_token(), ["u2:read"])

        stored = storage.get_access_token(issued.access_token)
        assert stored is not None
        assert stored.user_claims == ALICE_CLAIMS

    @pytest.mark.asyncio
    async def test_the_new_refresh_token_also_names_the_user(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull, storage: Any
    ) -> None:
        """The next rotation must work as well as this one."""
        issued = await provider.exchange_refresh_token(client, presented_token(), ["u2:read"])

        assert issued.refresh_token is not None
        stored = storage.get_refresh_token(issued.refresh_token)
        assert stored is not None
        assert stored.user_subject == "auth0|alice"

    @pytest.mark.asyncio
    async def test_the_resource_indicator_is_carried_across(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull, storage: Any
    ) -> None:
        """The audience the token was issued for does not change on refresh."""
        issued = await provider.exchange_refresh_token(client, presented_token(), ["u2:read"])

        stored = storage.get_access_token(issued.access_token)
        assert stored is not None
        assert stored.resource == "https://u2-mcp.example.com"


class TestRotationInvalidatesTheOldToken:
    """A rotated refresh token must not be usable twice."""

    @pytest.mark.asyncio
    async def test_the_old_refresh_token_stops_working(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull, storage: Any
    ) -> None:
        """Rotation revokes what it replaces."""
        await provider.exchange_refresh_token(client, presented_token(), ["u2:read"])

        assert storage.get_refresh_token("old-refresh") is None

    @pytest.mark.asyncio
    async def test_an_unknown_refresh_token_is_refused(
        self, provider: U2OAuthProvider, client: OAuthClientInformationFull
    ) -> None:
        """A token this server never issued cannot be exchanged for one it did."""
        unknown = RefreshToken(token="never-issued", client_id="client-1", scopes=["u2:read"])

        with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable
            await provider.exchange_refresh_token(client, unknown, ["u2:read"])
