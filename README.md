# URL Shortener

A stateless, distributed URL shortener engineered for high-throughput
redirection at production scale.

| Metric | Target |
|---|---|
| Daily read volume | 1 billion redirects (~11,600 avg QPS) |
| Daily write volume | 100 million creations (~2,320 peak QPS) |
| Key length | 7 characters, Base-62 (3.52 trillion unique keys) |

## System Architecture

```
Internet
   │
   ▼
┌─────────────────────────────────┐
│  Nginx  (port 80/443 — public)  │   ← Only component with a public IP.
│  Rate limiting · SSL termination │     Buffers slow clients; protects
│  Reverse proxy to internal net  │     ASGI workers from I/O starvation.
└──────────────┬──────────────────┘
               │  private network
   ┌───────────┴───────────┐
   ▼                       ▼
FastAPI worker 0      FastAPI worker N     ← Stateless. No local session
   │                       │                 or presentation state.
   ├─── read ──────────────┼──▶  Redis cluster   (O(1) cache hit)
   │                       │
   └─── write/miss ────────┴──▶  MongoDB sharded cluster
```

Nginx is the **only** component exposed to the public internet. All
FastAPI, Redis, and MongoDB communication happens over a private internal
network.

---

## Design Decisions

### 1 — 42-Bit Feistel Cipher (ID Scrambling)

**The problem.** Sequential auto-increment IDs allow a client to enumerate
every shortened URL by incrementing the counter. UUIDs avoid this but are
128 bits — far too long for a 7-character short code. Hash functions are
not bijective: two inputs can collide on the same output, requiring an
expensive database read-before-write check.

**The solution.** Each sequential integer produced by the lease manager is
scrambled by a custom in-memory 42-bit Feistel network before encoding.

A balanced Feistel cipher splits the 42-bit integer into two 21-bit halves
and applies 4 rounds of the following transform:

```
Input: (L, R)  ← two 21-bit halves of the plaintext

For each round i:
    (L, R) = (R,  L XOR f(R, key[i]))

Output: join(L, R)  → 42-bit ciphertext
```

The Feistel structure **mathematically guarantees bijectivity** — every
input maps to a unique output — regardless of the round function `f`. This
eliminates collisions without any database lookup.

**Why 42 bits.** 62⁷ = 3,521,614,606,208 fits between 2⁴¹ and 2⁴², so
a 42-bit cipher domain is the smallest that covers the full 7-character
Base-62 space. Values that land above 62⁷ − 1 are walked through the
permutation chain (cycle-walking) until they fall within the encodable
range — a standard format-preserving encryption technique.

Relevant source: [`app/id_generator.py`](app/id_generator.py) — `FeistelCipher`

---

### 2 — MongoDB Range Pre-Allocation (Segment Leasing)

**The problem.** A central atomic counter contacted on every write is a
single point of network contention. Snowflake-style IDs require
synchronized clocks across workers. UUID4 is unordered and at 128 bits
cannot be encoded in 7 characters.

**The solution.** On startup (and whenever its local block is exhausted),
each FastAPI worker contacts MongoDB once to atomically claim a contiguous
block of one million sequence numbers:

```python
result = await collection.find_one_and_update(
    {"_id": "url_sequence"},
    {"$inc": {"seq": 1_000_000}},
    upsert=True,
    return_document=True,
)
```

The worker stores the range locally (`current`, `ceiling`) and increments
a counter in O(1) memory. **999,999 of every 1,000,000 ID assignments
involve zero network I/O.**

**Durability.** The MongoDB connection uses `w="majority"` and `j=True`
(journaling). No lease is granted until the sequence document is durably
written to a majority of replica nodes, preventing lease rollback on a
primary failover.

Relevant source: [`app/id_generator.py`](app/id_generator.py) — `SequenceLeaseManager`

#### Architectural Trade-off: Lease Size

The `DEFAULT_LEASE_SIZE` is set to **10,000** (not 1,000,000).

A larger block reduces database round-trips further, but introduces a
crash-leakage problem. FastAPI workers run inside stateless containers that can
be killed at any time — by an OOM killer, a rolling deployment, or a
node preemption. When a worker is killed mid-lease, every ID it claimed but
never used is permanently lost; those sequence numbers will never be encoded
into a short code, creating gaps in the ID space.

With a lease of 1,000,000 a single OOM kill can leak up to 999,999 IDs. Over
time, on a fleet of containers subject to regular restarts, this leakage
accumulates and pushes the global counter far ahead of the number of URLs
actually created — wasting a non-trivial portion of the 3.52 trillion available
key space.

Reducing the lease to 10,000 caps worst-case leakage at 9,999 IDs per
crash — a **99% reduction** — while the resulting database load remains
completely trivial:

| Metric | Value |
|---|---|
| Daily URL creations (target) | 100,000,000 |
| Lease size | 10,000 |
| Maximum MongoDB lease fetches per day | 10,000 |
| Comparison: MongoDB write capacity | millions of ops/day |

10,000 `find_one_and_update` calls per day is negligible against the
database's capacity. The durability guarantee (`w="majority"`, `j=True`) is
preserved on every one of those calls.

---

### 3 — Non-Blocking aiodns SSRF Shield

**The problem.** `socket.getaddrinfo` is a blocking system call. When
called from an ASGI event loop thread it stalls **every concurrent
coroutine** for the full round-trip duration — often 50–300 ms under load.
This is event-loop starvation: a standard library call silently serialises
all in-flight requests.

**The solution.** DNS resolution is performed exclusively through
[aiodns](https://github.com/saghul/aiodns), which wraps the c-ares
asynchronous resolver. Resolution is dispatched as a non-blocking I/O
event; the coroutine yields until the result arrives without blocking the
thread.

```python
# ✗  Blocks the event loop
socket.getaddrinfo(host, None)

# ✓  Yields to the event loop; other coroutines continue
await resolver.gethostbyname(host, socket.AF_INET)
```

**SSRF policy enforced on every write request:**

| Check | Blocked ranges |
|---|---|
| Private IPv4 (RFC 1918) | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` |
| Loopback | `127.0.0.0/8`, `localhost` (case-insensitive) |
| Link-local / cloud metadata | `169.254.0.0/16` (incl. `169.254.169.254`) |
| IPv6 loopback | `::1/128` |
| IPv6 link-local | `fe80::/10` |
| IPv6 unique-local | `fc00::/7` |
| IPv4-mapped IPv6 | `::ffff:192.168.x.x` etc. (embedded IPv4 checked) |
| Schemes | Only `http` and `https` |
| DNS failure | Treated as a block (fail-safe) |

Literal IPs in the URL bypass DNS resolution entirely. Hostnames that
resolve to any blocked range are rejected (DNS rebinding defence).

Relevant source: [`app/ssrf_validator.py`](app/ssrf_validator.py) — `validate_url`

---

### 4 — Redis Read-Through Cache (URL Persistence)

**The problem.** Serving one billion redirects per day at ~11,600 QPS means
the dominant operation is a key lookup by short code. Hitting MongoDB on
every redirect would saturate the database's read capacity and add network
round-trip latency to the hot path (target: sub-millisecond redirection).

**The solution.** `URLRepository` implements a read-through cache pattern
that keeps the hot path entirely in Redis RAM:

```
Write path — save(short_code, url)
  insert_one → MongoDB (w="majority", j=True)
  Redis is NOT touched. Cache population is strictly lazy.

Read path — get(short_code)
  1. redis.get(short_code)
     ├─ HIT  → decode bytes → return URL  ← MongoDB never opened
     └─ MISS → find_one({"_id": short_code})
               ├─ FOUND     → redis.set(short_code, url) → return URL
               └─ NOT FOUND → return None  (Redis NOT written)
```

**Why lazy population on the write path.** Writing to Redis on `save()`
would couple the write-path latency to two sequential network calls (Mongo
+ Redis). Lazy population means only the first read miss pays that cost;
every subsequent read for the same code is a sub-millisecond Redis hit.

**Why not write `None` to Redis on a miss.** A short code that does not
exist today may be created tomorrow. Caching `None` would serve stale
404s until the TTL expires. The repository intentionally skips the Redis
write on not-found.

**Durability.** The MongoDB client is configured with `w="majority"` and
`j=True`. No URL document is acknowledged until it is durably written to a
majority of replica nodes and flushed to the journal, preventing data loss
on a primary failover.

**The call sequence is a first-class contract.** The interaction order
`redis.get → mongo.find_one → redis.set` is enforced by `TestReadOrder`
using a side-effect call log, not just presence/absence checks.

Relevant source: [`app/url_repository.py`](app/url_repository.py) — `URLRepository`

---

### 5 — Async Telemetry Pipeline (File-Based, Not Redis)

**The problem.** Buffering click analytics directly in Redis is an
anti-pattern at this scale: every `INCR` or `PFADD` competes with the
hot URL cache for RAM, forcing LRU evictions of frequently-redirected
short codes. Evicting a hot URL from Redis turns a sub-millisecond cache
hit into a full MongoDB round-trip — exactly the latency the cache exists
to prevent.

**The solution.** Each FastAPI worker appends structured JSON telemetry
entries to a local append-only log file. An offline Python script
(`app/analytics.py`) streams the log independently of the hot path.

```
Redirect request
      │
      ▼
 repo.get() → 302 Response  ← returned to the caller immediately
      │
      └──▶ asyncio.create_task(telemetry.record_redirect(...))
                │
                ▼ (background — does not block the response)
           asyncio.to_thread(_append)
                │
                ▼
           /var/log/url_shortener_analytics.log
           {"ts":"2026-08-09T10:00:00+00:00","code":"aB3cD4e","ip":"203.0.113.42"}
           {"ts":"2026-08-09T10:00:01+00:00","code":"xY9zW1q","ip":"198.51.100.7"}
           ...
```

**Why `asyncio.create_task`, not `await`.** The `create_task` call
schedules the log write on the running event loop and returns
immediately. The 302 response is transmitted to the client before the
log entry hits disk. Even if the log write fails (disk full, missing
directory), the exception is swallowed internally — telemetry loss is
preferable to a failed redirect.

**Why `asyncio.to_thread` inside the logger.** Python's built-in file
I/O is blocking. Calling `open()` / `write()` directly in a coroutine
would stall every other coroutine on the thread for the duration of the
syscall. `asyncio.to_thread` offloads the write to the thread-pool
executor, keeping the event loop free.

**Offline analytics consumer.** `AnalyticsConsumer` reads the log file
as a lazy generator — one line at a time, never the whole file — and
computes two aggregates:

| Method | Algorithm | Space |
|---|---|---|
| `compute_dau(entries)` | Insert each `ip` into a Python `set`; return `len(set)` | O(U) — unique users only |
| `compute_total_clicks(entries)` | `collections.Counter` over `code` field | O(K) — unique short codes |

`entries_for_date(log_path, target_date)` filters the stream to a single
UTC date before aggregation, preventing yesterday's traffic from
inflating today's DAU.

Relevant sources:
[`app/telemetry.py`](app/telemetry.py) — `TelemetryLogger` ·
[`app/analytics.py`](app/analytics.py) — `AnalyticsConsumer`

---

## Frontend

A single-page React application that provides a clean UI for shortening URLs.

**Tech:** Vite 5 + React 18 + Tailwind CSS 3 — no Redux, no routing library.

**Features:**

- Centered layout with a large URL input and "Shorten →" button
- Async `POST /shorten` with loading state (spinner on button)
- SSRF 400 errors displayed as a "REQUEST BLOCKED" banner with the exact server message
- Network errors surfaced as a user-friendly fallback message
- Success panel with the full short URL as a clickable `<a target="_blank" rel="noopener noreferrer">` link and a one-click "Copy" button
- "Start over" action resets all state and returns focus to the input

### Frontend environment variables

| Variable | Default | Description |
|---|---|---|
| `VITE_API_BASE` | _(empty)_ | Base URL for API calls (e.g. `http://localhost:8000`). Omit in production when the SPA is served behind the same Nginx origin as the API. |

### Running the frontend (development)

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

During development Vite proxies `/shorten` to `http://localhost:8000`, so the FastAPI backend must be running locally on port 8000.

### Building for production

```bash
cd frontend
npm run build
# Static assets emitted to frontend/dist/
```

Serve `frontend/dist/` from Nginx's root so the SPA and API share the same origin, eliminating CORS and the need for the `VITE_API_BASE` variable.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://mongo:27017` | MongoDB connection string. Targets the `url_shortener` database. |
| `REDIS_URI` | `redis://redis:6379` | Redis connection string. Used by `URLRepository` for the read-through cache. |
| `BASE_URL` | `http://localhost` | Public base URL prepended to short codes in the `POST /shorten` response. |
| `LOG_PATH` | `/var/log/url_shortener_analytics.log` | Append-only telemetry log written by `TelemetryLogger`. Feed this path to `AnalyticsConsumer` for offline DAU and click aggregation. |
| `FEISTEL_KEY_0` – `FEISTEL_KEY_3` | `0xDEADBEEF`, `0xCAFEBABE`, `0x12345678`, `0xABCDEF01` | Four 32-bit Feistel round keys. Rotate per deployment to prevent short-code prediction. |

---

## Running the Test Suite

The test suite is written with `pytest` and `pytest-asyncio` following a
strict contract-first (test-first) approach. Tests were written before any
implementation code.

**Install dependencies**

```bash
pip install -r requirements.txt
```

**Run the full suite (146 tests)**

```bash
pytest tests/ -v
```

**Run without throughput benchmarks**

```bash
pytest tests/ -v -m "not slow"
```

**Run a single module**

```bash
pytest tests/test_id_generator.py -v
pytest tests/test_ssrf_validator.py -v
pytest tests/test_url_repository.py -v
pytest tests/test_telemetry.py -v
```

### Test coverage

| File | Class | What it enforces |
|---|---|---|
| `test_id_generator.py` | `TestFeistelCipherBijectiveReversibility` | `decrypt(encrypt(x)) == x` for boundary and mid-range values |
| | `TestFeistelCipherOutputRange` | All outputs stay within `[0, 2⁴²)` |
| | `TestFeistelCipherNonCollision` | 10,000 sequential inputs → 10,000 distinct outputs |
| | `TestFeistelCipherDeterminism` | Same keys + same input = same output, always |
| | `TestFeistelCipherKeySensitivity` | Different round keys → different ciphertexts |
| | `TestFeistelCipherRoundCount` | `ROUNDS == 4`, `BITS == 42`; constructor rejects ≠ 4 keys |
| | `TestSequenceLeaseManagerDBInteraction` | `find_one_and_update` called once for 1,000 sequential IDs; exhaustion boundary tested at `lease_size=5` |
| | `TestSequenceLeaseManagerOOneComplexity` | 10,000 in-lease calls in under 1 s with zero DB calls |
| | `TestSequenceLeaseManagerConstants` | `DEFAULT_LEASE_SIZE == 1_000_000` |
| | `TestIDGenerator` | Full pipeline: seq ID → Feistel → 7-char Base-62; 1,000 IDs are unique |
| `test_ssrf_validator.py` | `TestHappyPath` | Valid public URLs pass; literal IPs skip DNS |
| | `TestAiodnsUsage` | `resolver.gethostbyname` is called (never `socket.getaddrinfo`); bare hostname passed; called exactly once |
| | `TestPrivateIPv4` | RFC-1918 ranges blocked; off-by-one guards for `172.16/12` |
| | `TestLoopback` | Full `127.0.0.0/8` range; `LOCALHOST` case-insensitive |
| | `TestLinkLocal` | `169.254.0.0/16` including AWS metadata endpoint |
| | `TestIPv6Private` | Loopback, link-local (with zone ID), unique-local, IPv4-mapped |
| | `TestForbiddenSchemes` | `file`, `ftp`, `javascript`, `data`, `gopher`, `dict`; case-insensitive |
| | `TestMalformedURLs` | Empty string, whitespace, bare hostname, missing host |
| | `TestDNSRebindingDefence` | DNS rebinding (hostname resolves to private IP); `aiodns.error.DNSError` → block |
| `test_url_repository.py` | `TestWritePath` | `save()` calls `insert_one` once with `{"_id": code, "url": url}`; `redis.get` and `redis.set` are never called |
| | `TestCacheHit` | Redis hit → URL decoded from bytes returned; `find_one` not called; `redis.set` not called; `redis.get` called with short code |
| | `TestCacheMiss` | Redis miss → `find_one` called with `{"_id": code}`; URL returned; `redis.set` called once with `(code, url)` as positional args |
| | `TestNotFound` | Both stores return nothing → `None` returned; `redis.set` never called (no null-cache poisoning) |
| | `TestReadOrder` | Hit: `redis.get` called before `find_one` is ever reached; Miss: side-effect log asserts exact sequence `redis.get → mongo.find_one → redis.set` |
| `test_telemetry.py` | `TestLogEntryFormat` | `record_redirect` writes exactly one valid JSON line per call containing `ts`, `code`, and `ip`; successive calls append without overwriting |
| | `TestLogEntryTimestamp` | `ts` is ISO-8601 parseable; carries non-`None` UTC `tzinfo`; reflects today's UTC date |
| | `TestAsyncAndErrorIsolation` | `record_redirect` is a coroutine function; calling it returns an awaitable; bad log path swallows `OSError`; healthy logger unaffected by a prior failure |
| | `TestStreamEntries` | `stream_entries` is a generator; yields dicts; empty file → empty iterator; skips malformed JSON and blank lines; all three fields present per entry |
| | `TestDAUComputation` | One IP → 1; same IP twice → 1 (dedup); two IPs → 2; empty → 0; 1,000 entries / 500 unique IPs → 500; return type is `int` |
| | `TestClickComputation` | One click → 1; two clicks same code → 2; two codes counted independently; empty → `{}`; values are `int` |
| | `TestDateIsolation` | Today's filter excludes yesterday; yesterday's filter excludes today; `entries_for_date` is a generator; full pipeline `entries_for_date → compute_dau` excludes prior-day IPs |
