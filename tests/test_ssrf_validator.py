"""
Contract tests for the SSRF Validator.

Per architect.md §4:

  During URL creation, the async SSRF shield resolves the domain ONCE via
  non-blocking aiodns.  If it resolves to a private IP, the write is rejected.

  Key mandates enforced here:
    1. DNS resolution MUST use aiodns.DNSResolver.gethostbyname — never
       socket.getaddrinfo, which blocks the ASGI event loop.
    2. Literal IPs in the URL skip DNS resolution entirely.
    3. The validator fails closed: DNS errors are treated as blocks.
    4. Only http and https schemes are permitted.

API contract under test:
    async def validate_url(url: str, *, resolver: aiodns.DNSResolver) -> None
        Returns None on success.
        Raises SSRFValidationError on any violation.

    class SSRFValidationError(Exception): ...

The `resolver` parameter is a live aiodns.DNSResolver passed from the FastAPI
app-state lifespan.  Tests inject an AsyncMock in its place.
"""
import socket

import pytest

from app.ssrf_validator import SSRFValidationError, validate_url
from tests.conftest import make_aiodns_result

_PUBLIC = make_aiodns_result("93.184.216.34")   # example.com — a safe public IP


# ══════════════════════════════════════════════════════════════════════════════
# Group 1 — Happy Path
# ══════════════════════════════════════════════════════════════════════════════

class TestHappyPath:
    async def test_valid_https_hostname_passes(self, mock_resolver):
        await validate_url("https://example.com", resolver=mock_resolver)

    async def test_valid_http_hostname_passes(self, mock_resolver):
        await validate_url("http://example.com", resolver=mock_resolver)

    async def test_url_with_path_and_query_passes(self, mock_resolver):
        await validate_url(
            "https://example.com/search?q=hello&page=2", resolver=mock_resolver
        )

    async def test_url_with_non_standard_port_passes(self, mock_resolver):
        await validate_url("https://example.com:8443/api/v1", resolver=mock_resolver)

    async def test_url_with_fragment_passes(self, mock_resolver):
        await validate_url("https://example.com/page#section", resolver=mock_resolver)

    async def test_valid_literal_public_ipv4_passes(self, mock_resolver):
        # Literal IPs skip DNS — resolver must NOT be called.
        await validate_url("https://8.8.8.8", resolver=mock_resolver)
        mock_resolver.gethostbyname.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Group 2 — Explicit aiodns Usage Assertions
# ══════════════════════════════════════════════════════════════════════════════

class TestAiodnsUsage:
    """
    These tests are the canonical proof that the implementation uses aiodns
    and not the blocking socket.getaddrinfo.  They explicitly assert on the
    mock_resolver call record.
    """

    async def test_gethostbyname_is_called_for_hostname_url(self, mock_resolver):
        await validate_url("https://example.com", resolver=mock_resolver)
        mock_resolver.gethostbyname.assert_called_once()

    async def test_gethostbyname_receives_the_bare_hostname(self, mock_resolver):
        """The host passed to aiodns must be stripped of scheme, port, and path."""
        await validate_url("https://example.com:8443/path?q=1", resolver=mock_resolver)
        positional_args = mock_resolver.gethostbyname.call_args[0]
        assert positional_args[0] == "example.com"

    async def test_gethostbyname_called_with_af_inet_family(self, mock_resolver):
        await validate_url("https://example.com", resolver=mock_resolver)
        positional_args = mock_resolver.gethostbyname.call_args[0]
        assert positional_args[1] == socket.AF_INET

    async def test_gethostbyname_not_called_for_literal_ipv4(self, mock_resolver):
        await validate_url("https://8.8.8.8", resolver=mock_resolver)
        mock_resolver.gethostbyname.assert_not_called()

    async def test_gethostbyname_not_called_for_literal_ipv6(self, mock_resolver):
        # Public IPv6 — no DNS lookup required.
        mock_resolver.gethostbyname.return_value = make_aiodns_result("2001:4860:4860::8888")
        await validate_url("https://[2001:4860:4860::8888]", resolver=mock_resolver)
        mock_resolver.gethostbyname.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Group 3 — Private IPv4 Ranges (RFC-1918, literal IPs)
# ══════════════════════════════════════════════════════════════════════════════

class TestPrivateIPv4:
    async def test_blocks_10_0_0_1(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://10.0.0.1", resolver=mock_resolver)

    async def test_blocks_10_network_upper_boundary(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://10.255.255.255", resolver=mock_resolver)

    async def test_blocks_172_16_first_address(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://172.16.0.1", resolver=mock_resolver)

    async def test_blocks_172_31_last_address(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://172.31.255.255", resolver=mock_resolver)

    async def test_does_not_block_172_15(self, mock_resolver):
        # 172.15.x.x is just below 172.16.0.0/12 — must pass.
        mock_resolver.gethostbyname.return_value = make_aiodns_result("172.15.255.255")
        await validate_url("https://172.15.255.255", resolver=mock_resolver)

    async def test_does_not_block_172_32(self, mock_resolver):
        # 172.32.x.x is just above 172.31.255.255 — must pass.
        mock_resolver.gethostbyname.return_value = make_aiodns_result("172.32.0.0")
        await validate_url("https://172.32.0.0", resolver=mock_resolver)

    async def test_blocks_192_168_1_1(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://192.168.1.1", resolver=mock_resolver)

    async def test_blocks_192_168_upper_boundary(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://192.168.255.255", resolver=mock_resolver)


# ══════════════════════════════════════════════════════════════════════════════
# Group 4 — Loopback
# ══════════════════════════════════════════════════════════════════════════════

class TestLoopback:
    async def test_blocks_127_0_0_1(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://127.0.0.1", resolver=mock_resolver)

    async def test_blocks_entire_127_slash_8_range(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://127.0.0.2", resolver=mock_resolver)

    async def test_blocks_127_upper_boundary(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://127.255.255.255", resolver=mock_resolver)

    async def test_blocks_localhost_hostname(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://localhost", resolver=mock_resolver)

    async def test_blocks_localhost_with_port(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("http://localhost:8080/admin", resolver=mock_resolver)

    async def test_blocks_localhost_uppercase(self, mock_resolver):
        # Case-insensitive check prevents trivial bypass via "LOCALHOST".
        with pytest.raises(SSRFValidationError):
            await validate_url("https://LOCALHOST", resolver=mock_resolver)


# ══════════════════════════════════════════════════════════════════════════════
# Group 5 — Link-Local (169.254.0.0/16)
# ══════════════════════════════════════════════════════════════════════════════

class TestLinkLocal:
    async def test_blocks_169_254_0_1(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://169.254.0.1", resolver=mock_resolver)

    async def test_blocks_aws_metadata_ip(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://169.254.169.254", resolver=mock_resolver)

    async def test_blocks_aws_metadata_with_credentials_path(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                resolver=mock_resolver,
            )

    async def test_blocks_169_254_upper_boundary(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://169.254.255.255", resolver=mock_resolver)


# ══════════════════════════════════════════════════════════════════════════════
# Group 6 — IPv6 Private Ranges
# ══════════════════════════════════════════════════════════════════════════════

class TestIPv6Private:
    async def test_blocks_ipv6_loopback_compressed(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://[::1]", resolver=mock_resolver)

    async def test_blocks_ipv6_loopback_full_form(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "https://[0000:0000:0000:0000:0000:0000:0000:0001]",
                resolver=mock_resolver,
            )

    async def test_blocks_ipv6_link_local_fe80(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://[fe80::1]", resolver=mock_resolver)

    async def test_blocks_ipv6_link_local_with_zone_id(self, mock_resolver):
        # Zone-ID notation: fe80::1%eth0, percent-encoded as %25eth0 in URL.
        with pytest.raises(SSRFValidationError):
            await validate_url("https://[fe80::1%25eth0]", resolver=mock_resolver)

    async def test_blocks_ipv6_unique_local_fc(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://[fc00::1]", resolver=mock_resolver)

    async def test_blocks_ipv6_unique_local_fd(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://[fd00::1]", resolver=mock_resolver)

    async def test_blocks_ipv4_mapped_private_in_ipv6(self, mock_resolver):
        # ::ffff:192.168.1.1 embeds a private IPv4 address inside IPv6.
        with pytest.raises(SSRFValidationError):
            await validate_url("https://[::ffff:192.168.1.1]", resolver=mock_resolver)


# ══════════════════════════════════════════════════════════════════════════════
# Group 7 — Forbidden Schemes
# ══════════════════════════════════════════════════════════════════════════════

class TestForbiddenSchemes:
    async def test_blocks_file_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("file:///etc/passwd", resolver=mock_resolver)

    async def test_blocks_ftp_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("ftp://example.com", resolver=mock_resolver)

    async def test_blocks_javascript_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("javascript:alert(1)", resolver=mock_resolver)

    async def test_blocks_data_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "data:text/html,<script>alert(1)</script>", resolver=mock_resolver
            )

    async def test_blocks_gopher_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("gopher://127.0.0.1:70/", resolver=mock_resolver)

    async def test_blocks_dict_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "dict://localhost:2628/d:password:1", resolver=mock_resolver
            )

    async def test_scheme_check_is_case_insensitive(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("FILE:///etc/passwd", resolver=mock_resolver)


# ══════════════════════════════════════════════════════════════════════════════
# Group 8 — Malformed URLs
# ══════════════════════════════════════════════════════════════════════════════

class TestMalformedURLs:
    async def test_blocks_empty_string(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("", resolver=mock_resolver)

    async def test_blocks_whitespace_only(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("   ", resolver=mock_resolver)

    async def test_blocks_bare_hostname_without_scheme(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("example.com", resolver=mock_resolver)

    async def test_blocks_scheme_with_no_host(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("https://", resolver=mock_resolver)

    async def test_blocks_relative_path(self, mock_resolver):
        with pytest.raises(SSRFValidationError):
            await validate_url("/relative/path", resolver=mock_resolver)


# ══════════════════════════════════════════════════════════════════════════════
# Group 9 — DNS Rebinding Defence (aiodns mock returns private IPs)
# ══════════════════════════════════════════════════════════════════════════════

class TestDNSRebindingDefence:
    """
    A public-looking hostname may DNS-resolve to a private IP — the classic
    DNS-rebinding attack vector.  aiodns resolves the name; the result is
    then range-checked identically to a literal IP.
    """

    async def test_hostname_resolving_to_rfc1918_is_blocked(self, mock_resolver):
        mock_resolver.gethostbyname.return_value = make_aiodns_result("10.0.0.1")
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "https://totally-legitimate.example.com", resolver=mock_resolver
            )

    async def test_hostname_resolving_to_loopback_is_blocked(self, mock_resolver):
        mock_resolver.gethostbyname.return_value = make_aiodns_result("127.0.0.1")
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "https://totally-legitimate.example.com", resolver=mock_resolver
            )

    async def test_hostname_resolving_to_link_local_is_blocked(self, mock_resolver):
        mock_resolver.gethostbyname.return_value = make_aiodns_result("169.254.169.254")
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "https://totally-legitimate.example.com", resolver=mock_resolver
            )

    async def test_hostname_resolving_to_ipv6_loopback_is_blocked(self, mock_resolver):
        mock_resolver.gethostbyname.return_value = make_aiodns_result("::1")
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "https://totally-legitimate.example.com", resolver=mock_resolver
            )

    async def test_hostname_resolving_to_ipv6_unique_local_is_blocked(self, mock_resolver):
        mock_resolver.gethostbyname.return_value = make_aiodns_result("fd12:3456:789a::1")
        with pytest.raises(SSRFValidationError):
            await validate_url(
                "https://totally-legitimate.example.com", resolver=mock_resolver
            )

    async def test_hostname_resolving_to_public_ip_passes(self, mock_resolver):
        mock_resolver.gethostbyname.return_value = make_aiodns_result("93.184.216.34")
        await validate_url("https://example.com", resolver=mock_resolver)  # must not raise

    async def test_aiodns_error_is_treated_as_block(self, mock_resolver):
        """
        Fail-safe: if aiodns cannot resolve the domain, the validator must
        reject the URL.  Unknown resolution = unverifiable safety.
        """
        import aiodns
        mock_resolver.gethostbyname.side_effect = aiodns.error.DNSError(
            1, "DNS query timed out"
        )
        with pytest.raises(SSRFValidationError):
            await validate_url("https://nonexistent.internal", resolver=mock_resolver)

    async def test_aiodns_gethostbyname_called_once_per_validation(self, mock_resolver):
        """
        The validator must resolve the hostname exactly once.
        Resolving multiple times per request would degrade write-path latency.
        """
        await validate_url("https://example.com", resolver=mock_resolver)
        assert mock_resolver.gethostbyname.call_count == 1
