"""
Async SSRF shield.

Validates a URL target before it is stored or redirected.  DNS resolution
uses aiodns exclusively — never socket.getaddrinfo, which would block the
ASGI event loop.

Fail-safe: any resolution error is treated as a block.
"""
import ipaddress
import socket
from urllib.parse import urlparse

import aiodns

ALLOWED_SCHEMES: frozenset = frozenset({"http", "https"})

BLOCKED_HOSTNAMES: frozenset = frozenset({"localhost"})

_PRIVATE_V4 = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]

_PRIVATE_V6 = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]


class SSRFValidationError(Exception):
    """Raised when a URL violates the SSRF policy."""


def _is_blocked_address(ip_str: str) -> bool:
    """Return True if ip_str falls in any blocked IP range."""
    # Strip IPv6 zone IDs (e.g. fe80::1%eth0 or fe80::1%25eth0)
    clean = ip_str.replace("%25", "%").split("%")[0].strip()
    try:
        addr = ipaddress.ip_address(clean)
    except ValueError:
        return False

    if isinstance(addr, ipaddress.IPv6Address):
        # IPv4-mapped: ::ffff:192.168.1.1 → check the embedded IPv4 part
        if addr.ipv4_mapped is not None:
            return any(addr.ipv4_mapped in net for net in _PRIVATE_V4)
        return any(addr in net for net in _PRIVATE_V6)

    return any(addr in net for net in _PRIVATE_V4)


async def validate_url(url: str, *, resolver: aiodns.DNSResolver) -> None:
    """
    Validate that `url` is safe to store and redirect.

    Returns None on success.
    Raises SSRFValidationError on any policy violation.

    The `resolver` parameter must be an aiodns.DNSResolver instance,
    injected by the FastAPI dependency system from app.state.
    """
    url = url.strip()
    if not url:
        raise SSRFValidationError("URL must not be empty")

    parsed = urlparse(url)

    # ── scheme check ─────────────────────────────────────────────────────────
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFValidationError(
            f"Scheme '{scheme or '(none)'}' is not permitted; only http and https are allowed"
        )

    # ── host extraction ───────────────────────────────────────────────────────
    # urlparse.hostname is already lowercased and strips IPv6 brackets.
    host = parsed.hostname
    if not host:
        raise SSRFValidationError("URL must contain a non-empty host")

    # ── hostname blocklist (covers 'localhost' regardless of DNS) ─────────────
    if host in BLOCKED_HOSTNAMES:
        raise SSRFValidationError(f"Host '{host}' is explicitly blocked")

    # ── literal IP fast-path (no DNS call needed) ─────────────────────────────
    # urlparse strips brackets from IPv6 literals; strip zone IDs too.
    host_clean = host.replace("%25", "%").split("%")[0]
    try:
        ipaddress.ip_address(host_clean)
        # Successfully parsed → it's a literal IP; check ranges directly.
        if _is_blocked_address(host_clean):
            raise SSRFValidationError(
                f"IP address '{host_clean}' is in a blocked private/reserved range"
            )
        return
    except ValueError:
        pass  # Not a literal IP — fall through to DNS resolution.

    # ── async DNS resolution via aiodns ──────────────────────────────────────
    try:
        result = await resolver.gethostbyname(host, socket.AF_INET)
        for ip in result.addresses:
            if _is_blocked_address(ip):
                raise SSRFValidationError(
                    f"Host '{host}' resolves to blocked address '{ip}'"
                )
    except SSRFValidationError:
        raise
    except Exception as exc:
        raise SSRFValidationError(
            f"DNS resolution failed for '{host}': {exc}"
        ) from exc
