"""
Integration tests for POST /shorten and GET /{short_code}.

These are the two highest-traffic routes and the primary service function.
Tests run against the real FastAPI app via httpx.ASGITransport with a
null lifespan and dependency overrides — no live MongoDB, Redis, or DNS.

Design decisions under test:
  POST /shorten:
    - Valid URL → 201, {short_code, short_url}, short_url contains short_code
    - short_code is 7 chars (IDGenerator contract)
    - IDGenerator.generate() called exactly once per request
    - URLRepository.save() called with (short_code, original_url)
    - SSRF-blocked literal-IP URL → 400 before ID is consumed
    - Missing/malformed body → 422

  GET /{short_code}:
    - Known code → 302, Location = original URL
    - Unknown code → 404
    - TelemetryLogger.record_redirect() called (fire-and-forget)
    - X-Real-IP header passed to telemetry, not transport-layer peer IP
    - No telemetry on 404
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient


# ── Mock factories ────────────────────────────────────────────────────────────

def _make_mock_id_gen(short_code: str = "test123") -> MagicMock:
    """IDGenerator that always returns the given short code."""
    gen = MagicMock()
    gen.generate = AsyncMock(return_value=short_code)
    return gen


def _make_mock_repo(stored_url: str | None = None) -> AsyncMock:
    """URLRepository where save() is a no-op and get() returns stored_url."""
    repo = AsyncMock()
    repo.save.return_value = None
    repo.get.return_value = stored_url
    return repo


def _make_mock_telemetry() -> AsyncMock:
    """TelemetryLogger where record_redirect() is a no-op coroutine."""
    tel = AsyncMock()
    tel.record_redirect.return_value = None
    return tel


def _make_mock_resolver(public_ip: str = "93.184.216.34") -> AsyncMock:
    """
    aiodns.DNSResolver mock that resolves all hostnames to a safe public IP.
    Using example.com's real IP (RFC 5737 space is fine too, but a true
    public address confirms the is-private check does not false-positive).
    """
    resolver = AsyncMock()
    result = MagicMock()
    result.addresses = [public_ip]
    resolver.gethostbyname.return_value = result
    return resolver


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def shorten_client():
    """
    AsyncClient wired for POST /shorten tests.
    Overrides: IDGenerator, URLRepository, aiodns resolver (safe default).
    Lifespan replaced with a no-op to avoid live MongoDB/Redis connections.
    """
    from app.app import app, get_id_generator, get_resolver, get_url_repo

    mock_id_gen = _make_mock_id_gen()
    mock_repo = _make_mock_repo()
    mock_resolver = _make_mock_resolver()

    app.dependency_overrides[get_id_generator] = lambda: mock_id_gen
    app.dependency_overrides[get_url_repo] = lambda: mock_repo
    app.dependency_overrides[get_resolver] = lambda: mock_resolver

    @asynccontextmanager
    async def null_lifespan(_):
        yield

    original = app.router.lifespan_context
    app.router.lifespan_context = null_lifespan

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, mock_id_gen, mock_repo

    app.router.lifespan_context = original
    app.dependency_overrides.pop(get_id_generator, None)
    app.dependency_overrides.pop(get_url_repo, None)
    app.dependency_overrides.pop(get_resolver, None)


@pytest.fixture
async def redirect_client():
    """
    AsyncClient wired for GET /{short_code} tests.
    Overrides: URLRepository, TelemetryLogger.
    Lifespan replaced with a no-op.
    """
    from app.app import app, get_telemetry, get_url_repo

    mock_repo = _make_mock_repo()
    mock_telemetry = _make_mock_telemetry()

    app.dependency_overrides[get_url_repo] = lambda: mock_repo
    app.dependency_overrides[get_telemetry] = lambda: mock_telemetry

    @asynccontextmanager
    async def null_lifespan(_):
        yield

    original = app.router.lifespan_context
    app.router.lifespan_context = null_lifespan

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, mock_repo, mock_telemetry

    app.router.lifespan_context = original
    app.dependency_overrides.pop(get_url_repo, None)
    app.dependency_overrides.pop(get_telemetry, None)


# ══════════════════════════════════════════════════════════════════════════════
# POST /shorten
# ══════════════════════════════════════════════════════════════════════════════

class TestShortenRoute:
    """HTTP-layer contracts for the write path."""

    async def test_valid_url_returns_201(self, shorten_client):
        client, *_ = shorten_client
        resp = await client.post("/shorten", json={"url": "https://example.com"})
        assert resp.status_code == 201

    async def test_response_shape(self, shorten_client):
        client, *_ = shorten_client
        data = (await client.post("/shorten", json={"url": "https://example.com"})).json()
        assert set(data.keys()) == {"short_code", "short_url"}

    async def test_short_code_is_seven_chars(self, shorten_client):
        """IDGenerator always produces 7-char codes; the route must not truncate or pad."""
        client, *_ = shorten_client
        data = (await client.post("/shorten", json={"url": "https://example.com"})).json()
        assert len(data["short_code"]) == 7

    async def test_short_url_embeds_short_code(self, shorten_client):
        client, *_ = shorten_client
        data = (await client.post("/shorten", json={"url": "https://example.com"})).json()
        assert data["short_code"] in data["short_url"]

    async def test_id_generator_called_once(self, shorten_client):
        client, mock_id_gen, _ = shorten_client
        await client.post("/shorten", json={"url": "https://example.com"})
        mock_id_gen.generate.assert_called_once()

    async def test_url_persisted_with_correct_args(self, shorten_client):
        """save() must receive (short_code, original_url) — order matters."""
        client, mock_id_gen, mock_repo = shorten_client
        target = "https://example.com/path?q=1"
        await client.post("/shorten", json={"url": target})
        mock_repo.save.assert_called_once_with(
            mock_id_gen.generate.return_value,
            target,
        )

    async def test_ssrf_literal_private_ip_returns_400(self, shorten_client):
        """
        A literal RFC-1918 address in the target URL must be rejected by
        the SSRF shield before any ID is consumed. The resolver is not
        called for literal IPs — the validator's fast-path catches them.
        """
        client, *_ = shorten_client
        resp = await client.post("/shorten", json={"url": "http://192.168.1.1/secret"})
        assert resp.status_code == 400

    async def test_ssrf_block_does_not_consume_id(self, shorten_client):
        """ID generator must NOT be called when SSRF validation fails."""
        client, mock_id_gen, _ = shorten_client
        await client.post("/shorten", json={"url": "http://10.0.0.1/internal"})
        mock_id_gen.generate.assert_not_called()

    async def test_missing_url_field_returns_422(self, shorten_client):
        client, *_ = shorten_client
        resp = await client.post("/shorten", json={})
        assert resp.status_code == 422

    async def test_malformed_json_returns_422(self, shorten_client):
        client, *_ = shorten_client
        resp = await client.post(
            "/shorten",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# GET /{short_code}
# ══════════════════════════════════════════════════════════════════════════════

class TestRedirectRoute:
    """HTTP-layer contracts for the read path."""

    async def test_known_code_returns_302(self, redirect_client):
        client, mock_repo, _ = redirect_client
        mock_repo.get.return_value = "https://example.com"
        resp = await client.get("/abc1234", follow_redirects=False)
        assert resp.status_code == 302

    async def test_location_header_is_original_url(self, redirect_client):
        client, mock_repo, _ = redirect_client
        target = "https://example.com/destination?ref=snip"
        mock_repo.get.return_value = target
        resp = await client.get("/abc1234", follow_redirects=False)
        assert resp.headers["location"] == target

    async def test_unknown_code_returns_404(self, redirect_client):
        client, mock_repo, _ = redirect_client
        mock_repo.get.return_value = None
        resp = await client.get("/notexist", follow_redirects=False)
        assert resp.status_code == 404

    async def test_repo_queried_with_exact_short_code(self, redirect_client):
        client, mock_repo, _ = redirect_client
        mock_repo.get.return_value = "https://example.com"
        await client.get("/abc1234", follow_redirects=False)
        mock_repo.get.assert_called_once_with("abc1234")

    async def test_telemetry_called_on_redirect(self, redirect_client):
        """
        record_redirect is scheduled via asyncio.create_task (fire-and-forget).
        asyncio.sleep(0) drains the pending task before the assertion.
        """
        client, mock_repo, mock_telemetry = redirect_client
        mock_repo.get.return_value = "https://example.com"
        await client.get("/abc1234", follow_redirects=False)
        await asyncio.sleep(0)
        mock_telemetry.record_redirect.assert_called_once()

    async def test_telemetry_receives_correct_short_code(self, redirect_client):
        client, mock_repo, mock_telemetry = redirect_client
        mock_repo.get.return_value = "https://example.com"
        await client.get("/abc1234", follow_redirects=False)
        await asyncio.sleep(0)
        args, _ = mock_telemetry.record_redirect.call_args
        assert args[0] == "abc1234"

    async def test_x_real_ip_forwarded_to_telemetry(self, redirect_client):
        """
        Nginx sets X-Real-IP to the originating client address.
        The route must pass it to telemetry — not the transport-layer peer IP.
        """
        client, mock_repo, mock_telemetry = redirect_client
        mock_repo.get.return_value = "https://example.com"
        await client.get(
            "/abc1234",
            follow_redirects=False,
            headers={"X-Real-IP": "203.0.113.55"},
        )
        await asyncio.sleep(0)
        args, _ = mock_telemetry.record_redirect.call_args
        assert args[1] == "203.0.113.55"

    async def test_telemetry_not_called_on_404(self, redirect_client):
        """No telemetry must be recorded for an unknown short code."""
        client, mock_repo, mock_telemetry = redirect_client
        mock_repo.get.return_value = None
        await client.get("/notexist", follow_redirects=False)
        await asyncio.sleep(0)
        mock_telemetry.record_redirect.assert_not_called()
