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
import os
from contextlib import asynccontextmanager
from typing import Annotated

import aiodns
import motor.motor_asyncio
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from app.id_generator import FeistelCipher, IDGenerator, SequenceLeaseManager
from app.ssrf_validator import SSRFValidationError, validate_url

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
_BASE_URL: str = os.environ.get("BASE_URL", "http://localhost")


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
        j=True,
    )
    db = mongo["url_shortener"]

    app.state.resolver = aiodns.DNSResolver()
    app.state.cipher = FeistelCipher(keys=_FEISTEL_KEYS)
    app.state.lease_manager = SequenceLeaseManager(collection=db["sequence"])
    app.state.urls = db["urls"]

    yield

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


def get_urls(request: Request):
    return request.app.state.urls


# ── Convenience type aliases for route signatures ─────────────────────────────

ResolverDep = Annotated[aiodns.DNSResolver, Depends(get_resolver)]
IDGenDep = Annotated[IDGenerator, Depends(get_id_generator)]
UrlsDep = Annotated[object, Depends(get_urls)]


# ── Schemas ───────────────────────────────────────────────────────────────────

class ShortenRequest(BaseModel):
    url: str


class ShortenResponse(BaseModel):
    short_code: str
    short_url: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/shorten", response_model=ShortenResponse, status_code=201)
async def shorten_url(
    body: ShortenRequest,
    resolver: ResolverDep,
    id_gen: IDGenDep,
    urls: UrlsDep,
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
    await urls.insert_one({"_id": short_code, "url": body.url})

    return ShortenResponse(
        short_code=short_code,
        short_url=f"{_BASE_URL}/{short_code}",
    )


@app.get("/{short_code}")
async def redirect(short_code: str, urls: UrlsDep) -> Response:
    """
    Read path: look up the original URL and return HTTP 302.

    The stored URL string is placed directly in the Location header;
    no server-side fetch is performed (preserves SNI / CDN compatibility
    and avoids a secondary SSRF surface as noted in architect.md §4).
    """
    doc = await urls.find_one({"_id": short_code})
    if doc is None:
        raise HTTPException(status_code=404, detail="Short code not found")
    return Response(status_code=302, headers={"Location": doc["url"]})
