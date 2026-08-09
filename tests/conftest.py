from unittest.mock import AsyncMock, MagicMock

import pytest

from app.id_generator import FeistelCipher, SequenceLeaseManager

# Four deterministic round keys for a 42-bit Feistel cipher (21-bit halves).
# Values are arbitrary non-zero constants; the cipher's correctness must hold
# for any valid key set.
SAMPLE_ROUND_KEYS: list[int] = [0xDEAD_BEEF, 0xCAFE_BABE, 0x1234_5678, 0xABCD_EF01]


# ── FeistelCipher fixtures ────────────────────────────────────────────────────

@pytest.fixture
def cipher() -> FeistelCipher:
    return FeistelCipher(keys=SAMPLE_ROUND_KEYS)


@pytest.fixture
def cipher_alt_keys() -> FeistelCipher:
    """A second cipher with different round keys — used in key-sensitivity tests."""
    return FeistelCipher(keys=[0x1111_1111, 0x2222_2222, 0x3333_3333, 0x4444_4444])


# ── SequenceLeaseManager fixtures ─────────────────────────────────────────────

@pytest.fixture
def mock_collection() -> AsyncMock:
    """
    Async mock of a Motor MongoDB collection.
    Returns successive lease start values so the manager can track blocks.
    First call: seq=1_000_000 (block 0–999_999)
    Second call: seq=2_000_000 (block 1_000_000–1_999_999)
    """
    col = AsyncMock()
    col.find_one_and_update.side_effect = [
        {"seq": 1_000_000},
        {"seq": 2_000_000},
    ]
    return col


@pytest.fixture
def lease_manager(mock_collection) -> SequenceLeaseManager:
    return SequenceLeaseManager(collection=mock_collection)


# ── aiodns mock helpers ───────────────────────────────────────────────────────

def make_aiodns_result(ip: str) -> MagicMock:
    """
    Return a minimal object matching aiodns.DNSResolver.gethostbyname's result.
    The real result has an `addresses` attribute: a list of resolved IP strings.
    Supports both IPv4 ("1.2.3.4") and IPv6 ("::1") strings.
    """
    result = MagicMock()
    result.addresses = [ip]
    return result


@pytest.fixture
def mock_resolver() -> AsyncMock:
    """
    Async mock of an aiodns.DNSResolver instance.
    Tests set mock_resolver.gethostbyname.return_value as needed.
    Default: resolves to a known public IP (safe pass-through).
    """
    resolver = AsyncMock()
    resolver.gethostbyname.return_value = make_aiodns_result("93.184.216.34")
    return resolver


# ── URLRepository fixtures ────────────────────────────────────────────────────

@pytest.fixture
def mock_redis() -> AsyncMock:
    """
    Async mock of a redis.asyncio.Redis client.
    Default: cache miss (get returns None).
    Individual tests override return_value or side_effect as needed.
    """
    client = AsyncMock()
    client.get.return_value = None   # default: cache miss
    client.set.return_value = True
    return client


@pytest.fixture
def mock_urls_collection() -> AsyncMock:
    """
    Async mock of the Motor 'urls' MongoDB collection.
    Separate from mock_collection (which drives the sequence counter).
    Default: document not found (find_one returns None).
    """
    col = AsyncMock()
    col.find_one.return_value = None
    col.insert_one.return_value = AsyncMock()
    return col


@pytest.fixture
def url_repo(mock_urls_collection, mock_redis):
    """
    URLRepository wired with mocked MongoDB collection and Redis client.
    Import is deferred so collection succeeds before app/url_repository.py exists.
    """
    from app.url_repository import URLRepository
    return URLRepository(collection=mock_urls_collection, redis_client=mock_redis)


# ── Telemetry / Analytics fixtures ───────────────────────────────────────────

@pytest.fixture
def log_path(tmp_path) -> str:
    """
    Ephemeral log file path inside pytest's tmp_path directory.
    Each test gets a fresh, isolated file; pytest cleans it up automatically.
    """
    return str(tmp_path / "analytics.log")


@pytest.fixture
def telemetry_logger(log_path):
    """
    TelemetryLogger pointed at the ephemeral log file.
    Import is deferred so conftest loads cleanly before app/telemetry.py exists.
    """
    from app.telemetry import TelemetryLogger
    return TelemetryLogger(log_path=log_path)


@pytest.fixture
def analytics_consumer():
    """
    AnalyticsConsumer instance (stateless — no constructor args).
    Import is deferred so conftest loads cleanly before app/analytics.py exists.
    """
    from app.analytics import AnalyticsConsumer
    return AnalyticsConsumer()
