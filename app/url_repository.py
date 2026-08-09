"""
URL persistence layer.

Read-through cache pattern (per architect.md §5):

  Write path — save(short_code, url)
    Writes to MongoDB only.  Redis is not touched; cache population is
    strictly lazy and happens only on the first read miss.

  Read path — get(short_code) -> str | None
    1. Redis first.
    2. Hit  → return URL decoded from bytes; MongoDB never queried.
    3. Miss → query MongoDB with {"_id": short_code}.
    4. Found     → lazily write (short_code, url) to Redis; return URL.
    5. Not found → return None; Redis is NOT written.

The exact interaction sequence on a miss is a first-class contract:
    redis.get  →  mongo.find_one  →  redis.set
"""


class URLRepository:
    def __init__(self, collection, redis_client) -> None:
        self._collection = collection
        self._redis = redis_client

    async def save(self, short_code: str, url: str) -> None:
        await self._collection.insert_one({"_id": short_code, "url": url})

    async def get(self, short_code: str) -> str | None:
        cached = await self._redis.get(short_code)
        if cached is not None:
            return cached.decode() if isinstance(cached, bytes) else cached

        doc = await self._collection.find_one({"_id": short_code})
        if doc is None:
            return None

        url: str = doc["url"]
        await self._redis.set(short_code, url)
        return url
