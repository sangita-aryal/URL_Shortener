"""
FastAPI application.

Design mandates enforced here:
  - Stateless compute: no mutable state in module scope or route handlers.
  - Dependency Injection: all shared resources are read from request.app.state
    via Depends() providers.  Route handlers never reference app.state directly.
  - Separation of concerns: business logic lives in id_generator.py and
    ssrf_validator.py; this file owns only HTTP routing and DI wiring.
  - Network isolation: this server binds only on the private network; Nginx
    is the sole public-facing component (see docker-compose.yml).
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated

import aiodns
import motor.motor_asyncio
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app.analytics import AnalyticsConsumer
from app.id_generator import FeistelCipher, IDGenerator, SequenceLeaseManager
from app.ssrf_validator import SSRFValidationError, validate_url
from app.telemetry import TelemetryLogger
from app.url_repository import URLRepository

# ── Feistel round keys ────────────────────────────────────────────────────────
# Load from environment so each deployment can rotate keys without a rebuild.
# Defaults match the test fixture keys in tests/conftest.py.
_FEISTEL_KEYS: list[int] = [
    int(os.environ.get("FEISTEL_KEY_0", str(0xDEAD_BEEF))),
    int(os.environ.get("FEISTEL_KEY_1", str(0xCAFE_BABE))),
    int(os.environ.get("FEISTEL_KEY_2", str(0x1234_5678))),
    int(os.environ.get("FEISTEL_KEY_3", str(0xABCD_EF01))),
]

_MONGO_URI: str = os.environ.get("MONGO_URI", "mongodb://mongo:27017")
_REDIS_URI: str = os.environ.get("REDIS_URI", "redis://redis:6379")
_BASE_URL: str = os.environ.get("BASE_URL", "http://localhost")
_LOG_PATH:  str = os.environ.get("LOG_PATH", "/var/log/url_shortener_analytics.log")


# ── Application lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create shared singletons on startup and clean up on shutdown.

    Singletons are stored in app.state, not in module-level globals, so
    multiple FastAPI instances in the same process remain independent.
    """
    mongo = motor.motor_asyncio.AsyncIOMotorClient(
        _MONGO_URI,
        w="majority",
        journal=True,
    )
    db = mongo["url_shortener"]

    redis_client = aioredis.from_url(_REDIS_URI, decode_responses=False)

    app.state.resolver = aiodns.DNSResolver()
    app.state.cipher = FeistelCipher(keys=_FEISTEL_KEYS)
    app.state.lease_manager = SequenceLeaseManager(collection=db["sequence"])
    app.state.url_repo = URLRepository(
        collection=db["urls"],
        redis_client=redis_client,
    )
    app.state.telemetry = TelemetryLogger(log_path=_LOG_PATH)
    app.state.analytics = AnalyticsConsumer()

    yield

    await redis_client.aclose()
    mongo.close()


app = FastAPI(lifespan=lifespan)


# ── Dependency providers ──────────────────────────────────────────────────────
# Pure functions that read from app.state — never write or cache locally.

def get_resolver(request: Request) -> aiodns.DNSResolver:
    return request.app.state.resolver


def get_cipher(request: Request) -> FeistelCipher:
    return request.app.state.cipher


def get_lease_manager(request: Request) -> SequenceLeaseManager:
    return request.app.state.lease_manager


def get_id_generator(
    cipher: Annotated[FeistelCipher, Depends(get_cipher)],
    lease_manager: Annotated[SequenceLeaseManager, Depends(get_lease_manager)],
) -> IDGenerator:
    # IDGenerator is a thin wrapper — cheap to create per request.
    # The stateful SequenceLeaseManager singleton is injected from app.state.
    return IDGenerator(lease_manager=lease_manager, cipher=cipher)


def get_url_repo(request: Request) -> URLRepository:
    return request.app.state.url_repo


def get_telemetry(request: Request) -> TelemetryLogger:
    return request.app.state.telemetry


def get_analytics_consumer(request: Request) -> AnalyticsConsumer:
    return request.app.state.analytics


# ── Convenience type aliases for route signatures ─────────────────────────────

ResolverDep   = Annotated[aiodns.DNSResolver, Depends(get_resolver)]
IDGenDep      = Annotated[IDGenerator,         Depends(get_id_generator)]
RepoDep       = Annotated[URLRepository,       Depends(get_url_repo)]
TelemetryDep  = Annotated[TelemetryLogger,     Depends(get_telemetry)]
AnalyticsDep  = Annotated[AnalyticsConsumer,   Depends(get_analytics_consumer)]


# ── Schemas ───────────────────────────────────────────────────────────────────

class ShortenRequest(BaseModel):
    url: str


class ShortenResponse(BaseModel):
    short_code: str
    short_url: str


class StatsResponse(BaseModel):
    code: str
    total_clicks: int


class TopCode(BaseModel):
    code: str
    clicks: int


class AnalyticsSummaryResponse(BaseModel):
    date: str
    dau: int
    total_clicks: int
    top_codes: list[TopCode]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """Liveness probe consumed by the docker-compose healthcheck."""
    return {"status": "ok"}



@app.post("/shorten", response_model=ShortenResponse, status_code=201)
async def shorten_url(
    body: ShortenRequest,
    resolver: ResolverDep,
    id_gen: IDGenDep,
    repo: RepoDep,
) -> ShortenResponse:
    """
    Write path: validate the target URL, generate a short code, persist.

    SSRF validation runs before any ID is consumed so a rejected URL never
    wastes a sequence number.
    """
    try:
        await validate_url(body.url, resolver=resolver)
    except SSRFValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    short_code = await id_gen.generate()
    await repo.save(short_code, body.url)

    return ShortenResponse(
        short_code=short_code,
        short_url=f"{_BASE_URL}/{short_code}",
    )


@app.get("/stats/{code}", response_model=StatsResponse)
async def code_stats(
    code: str,
    analytics: AnalyticsDep,
) -> StatsResponse:
    """All-time click count for a single short code."""
    return StatsResponse(code=code, total_clicks=analytics.click_count_for_code(_LOG_PATH, code))


@app.get("/analytics", response_model=AnalyticsSummaryResponse)
async def analytics_summary(
    analytics: AnalyticsDep,
    target_date: date | None = Query(default=None, alias="date"),
) -> AnalyticsSummaryResponse:
    """
    Batch analytics for a calendar day.

    Ambiguous requirement interpreted as:
      - Batch (reads log file); no new streaming infra needed.
      - Defaults to today UTC when date param is omitted.
      - Metrics: DAU (unique IPs), total clicks, top-5 codes by clicks.
      - No auth: internal network only, consistent with the rest of the API.
    """
    resolved = target_date or datetime.now(UTC).date()
    summary = analytics.summary_for_date(_LOG_PATH, resolved)
    return AnalyticsSummaryResponse(**summary)


@app.get("/{short_code}")
async def redirect(
    short_code: str,
    request: Request,
    repo: RepoDep,
    telemetry: TelemetryDep,
) -> Response:
    """
    Read path: look up the original URL and return HTTP 302.

    URLRepository applies the read-through cache (Redis first, then
    MongoDB on a miss with lazy Redis population).
    The URL is placed directly in the Location header — no server-side
    fetch avoids a secondary SSRF surface (architect.md §4).

    Telemetry is scheduled as a background asyncio task so the 302
    is returned before the log write touches disk.
    """
    url = await repo.get(short_code)
    if url is None:
        raise HTTPException(status_code=404, detail="Short code not found")

    # Nginx sets X-Real-IP to the originating client address.
    # Fall back to the transport-layer peer when running without a proxy.
    client_ip = (
        request.headers.get("x-real-ip")
        or (request.client.host if request.client else "unknown")
    )
    asyncio.create_task(telemetry.record_redirect(short_code, client_ip))

    return Response(status_code=302, headers={"Location": url})
