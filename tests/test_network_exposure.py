"""Tests for what the server exposes to the network, and to whom.

The legacy HTTP/SSE mode has no authentication at all: every tool, including
those that write records and run system commands, is reachable by anyone who can
open the port. Upstream defaulted that mode to every network interface. These
tests hold the server to a safe default and to refusing the dangerous
combination outright.
"""

import pytest

from u2_mcp.config import U2Config
from u2_mcp.exposure import ExposureError, check_network_exposure, is_loopback


class TestLoopbackDetection:
    """Knowing whether a bind address reaches beyond this machine."""

    @pytest.mark.parametrize("address", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
    def test_loopback_addresses_are_recognised(self, address: str) -> None:
        """Addresses that stay on this machine are identified as loopback."""
        assert is_loopback(address) is True

    @pytest.mark.parametrize("address", ["0.0.0.0", "10.1.2.3", "::", "192.168.1.10", ""])
    def test_reachable_addresses_are_not_loopback(self, address: str) -> None:
        """Anything reachable from another machine is not loopback."""
        assert is_loopback(address) is False


class TestSafeDefaults:
    """The default configuration must not expose the database to the network."""

    def test_no_browser_origin_is_allowed_by_default(self, mock_config: U2Config) -> None:
        """Cross-origin browser access is off until an operator names an origin."""
        assert mock_config.http_cors_origins == []

    def test_the_default_bind_address_is_loopback(self, mock_config: U2Config) -> None:
        """Out of the box the HTTP server listens only on this machine."""
        assert is_loopback(mock_config.http_host) is True

    def test_authentication_is_still_off_by_default(self, mock_config: U2Config) -> None:
        """The default is unauthenticated, which is why the bind address matters."""
        assert mock_config.auth_enabled is False


class TestDangerousCombination:
    """Unauthenticated plus network-reachable must not start silently."""

    def test_unauthenticated_on_a_public_interface_is_refused(self, mock_config: U2Config) -> None:
        """Binding every interface with no authentication is refused."""
        mock_config.http_host = "0.0.0.0"
        mock_config.auth_enabled = False

        with pytest.raises(ExposureError):
            check_network_exposure(mock_config, transport="sse")

    def test_the_refusal_explains_the_three_ways_out(self, mock_config: U2Config) -> None:
        """The error tells an operator how to proceed rather than only saying no."""
        mock_config.http_host = "0.0.0.0"
        mock_config.auth_enabled = False

        with pytest.raises(ExposureError) as exc_info:
            check_network_exposure(mock_config, transport="sse")

        message = str(exc_info.value)
        assert "U2_HTTP_HOST" in message
        assert "U2_AUTH_ENABLED" in message
        assert "U2_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS" in message

    def test_an_explicit_override_is_honoured(self, mock_config: U2Config) -> None:
        """An operator who means it can proceed, having said so deliberately."""
        mock_config.http_host = "0.0.0.0"
        mock_config.auth_enabled = False
        mock_config.allow_unauthenticated_network_access = True
        mock_config.http_cors_origins_str = ""

        check_network_exposure(mock_config, transport="sse")

    def test_authenticated_on_a_public_interface_is_allowed(self, mock_config: U2Config) -> None:
        """With authentication in force, binding the network is the normal case."""
        mock_config.http_host = "0.0.0.0"
        mock_config.auth_enabled = True
        mock_config.http_cors_origins_str = "https://claude.ai"

        check_network_exposure(mock_config, transport="streamable-http")

    def test_loopback_without_authentication_is_allowed(self, mock_config: U2Config) -> None:
        """A local-only server needs no authentication to be safe."""
        mock_config.http_host = "127.0.0.1"
        mock_config.auth_enabled = False
        mock_config.http_cors_origins_str = ""

        check_network_exposure(mock_config, transport="sse")


class TestCredentialedWildcardCors:
    """Credentialed requests must not be accepted from any origin."""

    def test_wildcard_origins_with_credentials_is_refused(self, mock_config: U2Config) -> None:
        """Accepting credentials from every origin defeats the point of an origin check."""
        mock_config.http_host = "127.0.0.1"
        mock_config.auth_enabled = True
        mock_config.http_cors_origins_str = "*"

        with pytest.raises(ExposureError) as exc_info:
            check_network_exposure(mock_config, transport="streamable-http")

        assert "U2_HTTP_CORS_ORIGINS" in str(exc_info.value)

    def test_named_origins_with_credentials_are_allowed(self, mock_config: U2Config) -> None:
        """Naming the origins that may send credentials is the supported configuration."""
        mock_config.http_host = "127.0.0.1"
        mock_config.auth_enabled = True
        mock_config.http_cors_origins_str = "https://claude.ai"

        check_network_exposure(mock_config, transport="streamable-http")


class TestStdioIsUnaffected:
    """The desktop transport opens no port, so none of this applies."""

    def test_stdio_never_raises(self, mock_config: U2Config) -> None:
        """A local desktop connection is not a network exposure."""
        mock_config.http_host = "0.0.0.0"
        mock_config.auth_enabled = False

        check_network_exposure(mock_config, transport="stdio")
