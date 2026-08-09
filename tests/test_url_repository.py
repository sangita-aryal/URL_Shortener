"""
Contract tests for the URL persistence layer.

Per architect.md §5:

  Write path  — save(short_code, url)
    Persists to MongoDB only.  Redis is NOT touched on a write; cache
    population is strictly lazy — it happens on the first read miss.

  Read path   — get(short_code) -> str | None
    1. Check Redis first.
    2. Cache hit  → return the URL immediately; MongoDB is never queried.
    3. Cache miss → query MongoDB (secondaryPreferred offloads the primary).
    4. Found      → lazily write the URL back to Redis; return the URL.
    5. Not found  → return None; Redis is NOT written.

  Read order is a first-class contract: Redis must be consulted before
  MongoDB on every get(), and Redis must be written AFTER MongoDB on a miss.

API contract under test:

    class URLRepository:
        def __init__(self, collection, redis_client) -> None: ...
        async def save(self, short_code: str, url: str) -> None: ...
        async def get(self, short_code: str) -> str | None: ...
"""
import pytest

_CODE = "aB3cD4e"
_URL  = "https://example.com/some/long/path?q=test"
_URL_BYTES = _URL.encode()      # redis.get returns bytes


# ══════════════════════════════════════════════════════════════════════════════
# Write path
# ══════════════════════════════════════════════════════════════════════════════

class TestWritePath:
    """
    save() writes to MongoDB and touches Redis in no way whatsoever.
    Cache population is the read path's responsibility.
    """

    async def test_save_calls_insert_one(self, url_repo, mock_urls_collection):
        await url_repo.save(_CODE, _URL)
        mock_urls_collection.insert_one.assert_called_once()

    async def test_save_uses_short_code_as_document_id(self, url_repo, mock_urls_collection):
        await url_repo.save(_CODE, _URL)
        doc = mock_urls_collection.insert_one.call_args[0][0]
        assert doc["_id"] == _CODE

    async def test_save_stores_original_url_in_document(self, url_repo, mock_urls_collection):
        await url_repo.save(_CODE, _URL)
        doc = mock_urls_collection.insert_one.call_args[0][0]
        assert doc["url"] == _URL

    async def test_save_does_not_read_redis(self, url_repo, mock_redis):
        await url_repo.save(_CODE, _URL)
        mock_redis.get.assert_not_called()

    async def test_save_does_not_write_redis(self, url_repo, mock_redis):
        await url_repo.save(_CODE, _URL)
        mock_redis.set.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Read path — cache hit
# ══════════════════════════════════════════════════════════════════════════════

class TestCacheHit:
    """
    When Redis holds the key, get() must return the cached value and
    must not open a MongoDB connection at all.
    """

    @pytest.fixture(autouse=True)
    def prime_cache(self, mock_redis):
        """Seed Redis mock to simulate a warm cache entry."""
        mock_redis.get.return_value = _URL_BYTES

    async def test_cache_hit_returns_correct_url(self, url_repo):
        result = await url_repo.get(_CODE)
        assert result == _URL

    async def test_cache_hit_does_not_query_mongodb(self, url_repo, mock_urls_collection):
        await url_repo.get(_CODE)
        mock_urls_collection.find_one.assert_not_called()

    async def test_cache_hit_does_not_rewrite_redis(self, url_repo, mock_redis):
        """A hit must not trigger a redundant redis.set."""
        await url_repo.get(_CODE)
        mock_redis.set.assert_not_called()

    async def test_cache_hit_get_called_with_short_code(self, url_repo, mock_redis):
        await url_repo.get(_CODE)
        mock_redis.get.assert_called_once_with(_CODE)


# ══════════════════════════════════════════════════════════════════════════════
# Read path — cache miss, document found in MongoDB
# ══════════════════════════════════════════════════════════════════════════════

class TestCacheMiss:
    """
    When Redis has no entry, get() must query MongoDB, lazily populate
    Redis with the result, and return the URL.
    """

    @pytest.fixture(autouse=True)
    def prime_miss(self, mock_redis, mock_urls_collection):
        """Cold cache, document present in MongoDB."""
        mock_redis.get.return_value = None
        mock_urls_collection.find_one.return_value = {"_id": _CODE, "url": _URL}

    async def test_cache_miss_queries_mongodb(self, url_repo, mock_urls_collection):
        await url_repo.get(_CODE)
        mock_urls_collection.find_one.assert_called_once()

    async def test_cache_miss_queries_with_correct_filter(self, url_repo, mock_urls_collection):
        await url_repo.get(_CODE)
        filter_arg = mock_urls_collection.find_one.call_args[0][0]
        assert filter_arg == {"_id": _CODE}

    async def test_cache_miss_returns_url_from_mongodb(self, url_repo):
        result = await url_repo.get(_CODE)
        assert result == _URL

    async def test_cache_miss_lazily_populates_redis(self, url_repo, mock_redis):
        await url_repo.get(_CODE)
        mock_redis.set.assert_called_once()

    async def test_cache_miss_redis_set_uses_short_code_as_key(self, url_repo, mock_redis):
        await url_repo.get(_CODE)
        key = mock_redis.set.call_args[0][0]
        assert key == _CODE

    async def test_cache_miss_redis_set_uses_url_as_value(self, url_repo, mock_redis):
        await url_repo.get(_CODE)
        value = mock_redis.set.call_args[0][1]
        assert value == _URL


# ══════════════════════════════════════════════════════════════════════════════
# Read path — not found in either store
# ══════════════════════════════════════════════════════════════════════════════

class TestNotFound:
    """
    When neither Redis nor MongoDB holds the short code, get() returns
    None and must not corrupt Redis with an empty write.
    """

    @pytest.fixture(autouse=True)
    def prime_not_found(self, mock_redis, mock_urls_collection):
        mock_redis.get.return_value = None
        mock_urls_collection.find_one.return_value = None

    async def test_not_found_returns_none(self, url_repo):
        result = await url_repo.get(_CODE)
        assert result is None

    async def test_not_found_does_not_populate_redis(self, url_repo, mock_redis):
        await url_repo.get(_CODE)
        mock_redis.set.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Read order — Redis before MongoDB; Redis written after MongoDB
# ══════════════════════════════════════════════════════════════════════════════

class TestReadOrder:
    """
    The sequence  redis.get → [mongo.find_one → redis.set]  is a first-class
    architectural constraint, not an implementation detail.

    A cache hit test alone cannot prove order: it shows MongoDB is skipped,
    but not that Redis was consulted first.  These tests use a call log to
    assert the exact interaction sequence.
    """

    async def test_redis_queried_first_on_cache_hit(
        self, url_repo, mock_redis, mock_urls_collection
    ):
        """
        On a cache hit, Redis answers before MongoDB is ever reached.
        Proved by: redis.get called, find_one not called at all.
        """
        mock_redis.get.return_value = _URL_BYTES

        await url_repo.get(_CODE)

        mock_redis.get.assert_called_once_with(_CODE)
        mock_urls_collection.find_one.assert_not_called()

    async def test_call_sequence_on_cache_miss_is_get_then_find_then_set(
        self, url_repo, mock_redis, mock_urls_collection
    ):
        """
        On a miss the exact order must be:
            redis.get  →  mongo.find_one  →  redis.set

        Verified with side_effect call-log — no parent-mock attachment tricks
        that obscure what is actually being asserted.
        """
        log: list[str] = []

        async def _redis_get(key):
            log.append("redis.get")

        async def _find_one(filter_doc, **kw):
            log.append("mongo.find_one")
            return {"_id": _CODE, "url": _URL}

        async def _redis_set(key, value, **kw):
            log.append("redis.set")
            return True

        mock_redis.get.side_effect = _redis_get
        mock_urls_collection.find_one.side_effect = _find_one
        mock_redis.set.side_effect = _redis_set

        await url_repo.get(_CODE)

        assert log == ["redis.get", "mongo.find_one", "redis.set"], (
            f"Unexpected call sequence: {log}"
        )
