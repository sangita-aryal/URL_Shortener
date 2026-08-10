# URL Shortener

A stateless, distributed URL shortener engineered for high-throughput redirection at production scale.

---

## Table of Contents

1. [Project Overview & Tech Stack](#1-project-overview--tech-stack)
2. [Capacity Planning & Scale](#2-capacity-planning--scale)
3. [Architectural Decisions & Trade-offs](#3-architectural-decisions--trade-offs)
   - [NoSQL (MongoDB) vs. Relational (SQL)](#31-architectural-decision-nosql-mongodb-vs-relational-sql)
   - [Distributed ID Generation](#32-distributed-id-generation)
   - [HTTP 301 vs. HTTP 302 Redirects](#33-architectural-decision-http-301-vs-http-302-redirects)
   - [Security — SSRF Shield](#34-security--ssrf-shield)
   - [Telemetry Pipeline](#35-telemetry-pipeline)
4. [Local Setup & Test-Driven Development](#4-local-setup--test-driven-development)
5. [Environment Variables](#5-environment-variables)
6. [Running the Stack](#6-running-the-stack)
7. [Frontend](#7-frontend)
8. [Limitations and Trade-offs](#8-limitations-and-trade-offs)

---

## 1. Project Overview & Tech Stack

The system is a fully distributed URL shortener built around four layers, each chosen for a specific role at scale:

```
Internet
   │
   ▼
┌─────────────────────────────────┐
│  Nginx  (port 80/443 — public)  │   ← Only component with a public IP.
│  Rate limiting · SSL termination │     Buffers slow clients; protects
│  Reverse proxy to internal net  │     ASGI workers from I/O starvation.
└──────────────┬──────────────────┘
               │  private internal network
   ┌───────────┴───────────┐
   ▼                       ▼
FastAPI worker 0      FastAPI worker N     ← 100% stateless. No session
   │                       │                 or presentation state stored
   ├─── read ──────────────┼──▶  Redis cluster      (O(1) cache hit)
   │                       │
   └─── write/miss ────────┴──▶  MongoDB sharded cluster
```

| Layer | Technology | Role |
|---|---|---|
| Edge Proxy / API Gateway | Containerized Nginx (Layer 7) | Public entry point; rate limiting, SSL termination, slow-client buffering |
| Application Tier | Stateless Python FastAPI (ASGI / Uvicorn) | Business logic; 100% stateless — all state lives in the data tier |
| Cache Infrastructure | Redis RAM Cluster | Read-through cache; O(1) short-code lookups on the hot redirect path |
| Database Tier | Horizontally Sharded MongoDB Cluster | Durable URL persistence; segment lease counter; replica set for majority writes |
| AI Development Accelerator | Claude Sonnet 4.6 (Anthropic) | Drove the full TDD workflow — test authoring, implementation, architecture review, and documentation — accelerating delivery across every layer of the stack |

**Architectural constraints enforced across every layer:**

- Nginx is the **only** component with a public-facing IP. FastAPI, Redis, and MongoDB communicate exclusively over a private internal Docker network.
- FastAPI workers store **no** client state, session tokens, or presentation state in local memory.
- Every write is acknowledged only after `w="majority"` and `j=True` (journaling) are satisfied on the MongoDB replica set.

---

## 2. Capacity Planning & Scale

Back-of-the-envelope estimates that drive every architectural decision in this system:

| Dimension | Daily | Per Second (avg) |
|---|---|---|
| URL creations (writes) | 100,000,000 | ~1,157 (peak ~2,320) |
| Redirects (reads) | 1,000,000,000 | ~11,600 |

**10-year storage projection:**

| Dimension | Estimate |
|---|---|
| Daily URL creations | 100,000,000 |
| 10-year record count | ~365,000,000,000 (365 billion) |
| Storage per record (document + indexes) | ~100 bytes |
| Total persistent storage required | ~36.5 TB |
| Short code keyspace (7-char Base-62) | 3,521,614,606,208 unique keys |

These numbers impose hard requirements: no single database node can hold 36.5 TB of hot data, 11,600 read QPS demands sub-millisecond cache hits, and 7-character short codes must be collision-free across 365 billion records without expensive read-before-write checks.

---

## 3. Architectural Decisions & Trade-offs

### 3.1 Architectural Decision: NoSQL (MongoDB) vs. Relational (SQL)

Two capacity constraints drove the database technology choice away from a traditional RDBMS.

#### Massive scale demands horizontal scalability

No single relational database node can hold 36.5 TB of hot, indexed data while sustaining millions of writes per day. The standard answer — horizontal sharding — is where SQL breaks down. Cross-shard joins require coordinated scatter-gather queries across nodes, and dynamic resharding (rebalancing data when a shard becomes full) is a notoriously complex, error-prone operation that typically requires significant downtime or custom migration tooling.

MongoDB is natively engineered for this model. Its sharding layer distributes documents across nodes transparently; adding capacity is an operational procedure, not a schema migration. Our data access pattern (lookup by `_id`, write by `_id`) requires no cross-shard joins — every query is shard-key routable to a single node.

#### Predictable low-latency lookups on a simple data model

The redirect read path must sustain **11,600 requests per second** at sub-millisecond latency. The data model is a pure key-value mapping: resolve a 7-character string to a destination URL. There are no foreign keys, no aggregations, no multi-table joins.

Relational databases use B-tree indexes for row lookups. B-tree performance degrades as the index grows: at hundreds of billions of rows, the tree becomes deep enough that a single key lookup requires multiple random disk reads to traverse from root to leaf. As the dataset grows, B-tree traversal cost grows logarithmically — producing an unpredictable and growing latency tail that violates the sub-millisecond requirement.

MongoDB's WiredTiger storage engine maintains a B-tree index on `_id` per shard. Because each shard owns only a partition of the keyspace, the index per shard stays shallow and fast regardless of total cluster size. Adding shards reduces both index depth and per-shard I/O load proportionally, keeping redirect latency stable as the dataset scales to hundreds of billions of documents.

---

### 3.2 Distributed ID Generation

#### The hybrid approach: Feistel Cipher + MongoDB Segment Leasing

Standard auto-incrementing IDs create enumeration vulnerabilities. UUIDs are 128 bits — far too long for a 7-character short code. Hash functions are not bijective: two inputs can collide on the same output, requiring an expensive read-before-write database check. The solution combines two components:

**42-Bit Feistel Cipher (scrambling)**

Each sequential integer produced by the lease manager is scrambled by a custom in-memory 42-bit Feistel network before encoding. A balanced Feistel cipher splits the 42-bit integer into two 21-bit halves and applies 4 rounds:

```
Input: (L, R)  ← two 21-bit halves of the plaintext

For each round i:
    (L, R) = (R,  L XOR f(R, key[i]))

Output: join(L, R)  → 42-bit ciphertext
```

The Feistel structure **mathematically guarantees bijectivity** — every input maps to a unique output, regardless of the round function `f`. This eliminates collisions without any database lookup.

**Why 42 bits:** 62⁷ = 3,521,614,606,208 fits between 2⁴¹ and 2⁴², so a 42-bit cipher domain is the smallest that covers the full 7-character Base-62 space. Values that land above 62⁷ − 1 are walked through the permutation chain (cycle-walking) until they fall within the encodable range — a standard format-preserving encryption technique.

Relevant source: [`app/id_generator.py`](app/id_generator.py) — `FeistelCipher`

**MongoDB Segment Leasing (O(1) ID generation)**

On startup (and whenever its local block is exhausted), each FastAPI worker contacts MongoDB once to atomically claim a contiguous block of 10,000 sequence numbers:

```python
result = await collection.find_one_and_update(
    {"_id": "url_sequence"},
    {"$inc": {"seq": 10_000}},
    upsert=True,
    return_document=True,
)
```

The worker stores the range locally (`current`, `ceiling`) and increments an in-memory counter in O(1). **9,999 of every 10,000 ID assignments involve zero network I/O.**

**Durability:** The MongoDB connection uses `w="majority"` and `j=True` (journaling). No lease is granted until the sequence document is durably written to a majority of replica nodes, preventing lease rollback on a primary failover.

Relevant source: [`app/id_generator.py`](app/id_generator.py) — `SequenceLeaseManager`

#### Trade-off: Lease size (10,000 vs. 1,000,000)

FastAPI workers run inside stateless containers that can be killed at any time — by an OOM killer, a rolling deployment, or a node preemption. When a worker is killed mid-lease, every ID it claimed but never used is permanently lost, creating gaps in the ID space.

| Lease size | Max IDs leaked per crash | MongoDB fetches/day (at 100M writes) |
|---|---|---|
| 1,000,000 | 999,999 | 100 |
| **10,000** | **9,999** | **10,000** |

Reducing the lease to 10,000 caps worst-case leakage at 9,999 IDs per crash — a **99% reduction** — while 10,000 `find_one_and_update` calls per day is completely negligible against the replica set's capacity (millions of ops/day).

#### Evaluated Alternatives & Why We Rejected Them

**1. Relational DB Auto-Increment & Multi-Master Replication**

Sequential IDs are trivially guessable — an attacker can iterate through every shortened URL ever created by simply incrementing the short code. The common multi-master workaround (Server A generates odd numbers, Server B generates even) is operationally brittle: the step size must be fixed at cluster inception, adding or removing a node mid-operation breaks the scheme, and chronological ordering is not maintained across nodes.

**2. Hashing (MD5 / SHA-256)**

Hashing the destination URL and truncating the digest to 7 characters retains only ~41 bits of the original digest. By the birthday paradox, the probability of a collision reaches 50% after approximately 1.5 million URLs. Each collision requires a read-before-write round-trip to the database, doubling write-path latency in the common case and adding an unbounded retry loop in the worst case.

**3. Universally Unique Identifiers (UUIDs)**

A standard UUID v4 is 36 characters long (e.g., `550e8400-e29b-41d4-a716-446655440000`). Using one as a short code defeats the primary product requirement. Truncating a UUID to 7 characters strips its collision-resistance properties and reintroduces the birthday-paradox collision risk described above.

---

### 3.3 Architectural Decision: HTTP 301 vs. HTTP 302 Redirects

The choice of redirect status code has a direct and permanent consequence on what the telemetry pipeline can observe.

| | HTTP 301 — Permanent Redirect | HTTP 302 — Temporary Redirect ✓ |
|---|---|---|
| **Browser behaviour** | Caches the redirect indefinitely | Does not cache; re-requests on every click |
| **Repeat clicks** | Resolved client-side; never reach the server | Every click hits the server for resolution |
| **Server load** | Minimal after the first visit per user | Full load on every redirection event |
| **Telemetry visibility** | Blind to repeat traffic from returning visitors | Every click is observable by the backend |

**HTTP 301 (Permanent Redirect):** The browser stores the destination URL in its local cache and performs all future redirects for that short code entirely client-side. This minimises backend load and infrastructure cost, making it the right choice for static content that will never change destination — but it blinds the backend to repeat traffic.

**HTTP 302 (Temporary Redirect) — Our Choice:** The browser does not cache the redirect. Every click on a shortened URL — whether the user's first visit or their hundredth — resolves through our server.

We selected HTTP 302 because our system requirements mandate precise, real-time tracking of click metrics, source analytics, and user telemetry. Sacrificing the caching benefits of a 301 is a deliberate and necessary trade-off: a 301 would silently discard the repeat-visit traffic that our DAU and total-click aggregations depend on, making the analytics irrecoverably incomplete. Every redirection event must reach the server so the `TelemetryLogger` can append it to the pipeline.

Relevant source: [`app/app.py`](app/app.py) — `GET /{short_code}` route

---

### 3.4 Security — SSRF Shield

#### The problem: blocking DNS in an async event loop

`socket.getaddrinfo` is a blocking system call. When called from an ASGI event loop thread it stalls **every concurrent coroutine** for the full round-trip duration — often 50–300 ms under load. This is event-loop starvation: a standard library call silently serialises all in-flight requests.

#### The solution: aiodns

DNS resolution is performed exclusively through [aiodns](https://github.com/saghul/aiodns), which wraps the c-ares asynchronous resolver. Resolution is dispatched as a non-blocking I/O event; the coroutine yields until the result arrives without blocking the thread.

```python
# ✗  Blocks the event loop — stalls all concurrent coroutines
socket.getaddrinfo(host, None)

# ✓  Yields to the event loop; other coroutines continue unblocked
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

Literal IPs in the URL bypass DNS resolution entirely. Hostnames that resolve to any blocked range are rejected (DNS rebinding defence).

Relevant source: [`app/ssrf_validator.py`](app/ssrf_validator.py) — `validate_url`

---

### 3.5 Telemetry Pipeline

#### The problem: analytics must not compete with the cache

Buffering click analytics directly in Redis is an anti-pattern at this scale: every `INCR` or `PFADD` competes with the hot URL cache for RAM, forcing LRU evictions of frequently-redirected short codes. Evicting a hot URL from Redis turns a sub-millisecond cache hit into a full MongoDB round-trip — exactly the latency the cache exists to prevent.

#### The solution: async local logging + offline stream processing

Each FastAPI worker appends structured JSON telemetry entries to a local append-only log file as a fire-and-forget background task:

```
Redirect request
      │
      ▼
 repo.get() → 302 Response  ← returned to the caller immediately
      │
      └──▶ asyncio.create_task(telemetry.record_redirect(...))
                │
                ▼ (background — does not block the 302 response)
           asyncio.to_thread(_append)
                │
                ▼
           /var/log/url_shortener_analytics.log
           {"ts":"2026-08-09T10:00:00+00:00","code":"aB3cD4e","ip":"203.0.113.42"}
           {"ts":"2026-08-09T10:00:01+00:00","code":"xY9zW1q","ip":"198.51.100.7"}
```

- **`asyncio.create_task`** (not `await`) schedules the log write and returns immediately. The 302 response is transmitted to the client before the log entry hits disk. If the write fails, the exception is swallowed — telemetry loss is preferable to a failed redirect.
- **`asyncio.to_thread`** offloads the blocking `open()` / `write()` call to the thread-pool executor, keeping the event loop free.

**Offline analytics consumer:** `AnalyticsConsumer` reads the log file as a lazy generator — one line at a time, never the whole file — maintaining O(U) space complexity bounded strictly by unique users:

| Method | Algorithm | Space complexity |
|---|---|---|
| `compute_dau(entries)` | Insert each `ip` into a Python `set`; return `len(set)` | O(U) — unique users only |
| `compute_total_clicks(entries)` | `collections.Counter` over `code` field | O(K) — unique short codes |

`entries_for_date(log_path, target_date)` filters the stream to a single UTC date before aggregation, preventing prior-day traffic from inflating today's DAU.

Relevant sources: [`app/telemetry.py`](app/telemetry.py) — `TelemetryLogger` · [`app/analytics.py`](app/analytics.py) — `AnalyticsConsumer`

---

## 4. Local Setup & Test-Driven Development

The entire codebase was written using **contract-driven, test-first development**: every test was written and approved before a single line of implementation code. Tests define the observable contracts; implementation exists to satisfy them.

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run the full test suite

```bash
pytest tests/ -v
```

### Run without slow throughput benchmarks

```bash
pytest tests/ -v -m "not slow"
```

### Run a single module

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
| | `TestSequenceLeaseManagerOOneComplexity` | 5,000 in-lease calls complete in under 1 s with zero DB calls |
| | `TestSequenceLeaseManagerConstants` | `DEFAULT_LEASE_SIZE == 10_000` |
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

---

## 5. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://mongo:27017/?replicaSet=rs0` | MongoDB connection string. Must include `replicaSet=rs0` when running via Compose so Motor can enforce `w="majority"` write concerns. |
| `REDIS_URI` | `redis://redis:6379` | Redis connection string. Used by `URLRepository` for the read-through cache. |
| `BASE_URL` | `http://localhost` | Public base URL prepended to short codes in the `POST /shorten` response. |
| `LOG_PATH` | `/var/log/url_shortener_analytics.log` | Append-only telemetry log written by `TelemetryLogger`. Feed this path to `AnalyticsConsumer` for offline DAU and click aggregation. |
| `FEISTEL_KEY_0` – `FEISTEL_KEY_3` | `0xDEADBEEF`, `0xCAFEBABE`, `0x12345678`, `0xABCDEF01` | Four 32-bit Feistel round keys. Rotate per deployment to prevent short-code prediction. |

---

## 6. Running the Stack

### Prerequisites

- Docker Engine ≥ 24 and Docker Compose v2 (`docker compose` command, not `docker-compose`)

### Project layout (orchestration files)

```
docker-compose.yaml   — full stack definition
Dockerfile            — FastAPI image (python:3.13-slim + uvicorn)
.dockerignore         — excludes tests/, docs, and dev artefacts from the build context
nginx/nginx.conf      — rate limiting, keepalive upstream, X-Real-IP forwarding
scripts/report.py     — offline analytics report (run via the analytics profile)
```

### Start the full stack

```bash
docker compose up --build
```

This starts six services in dependency order:

```
mongo → mongo-init → fastapi → nginx
redis ────────────────────▲
```

`mongo-init` is a one-shot container that calls `rs.initiate()` to form the
`rs0` replica set. FastAPI waits on `service_completed_successfully` before
connecting, so it never opens a MongoDB connection against a standalone node.

### Scale FastAPI workers

```bash
docker compose up --build --scale fastapi=4
```

All replicas share the same `telemetry_logs` named volume and inherit round-robin
load balancing from Nginx's upstream block. Add replica `server` lines to
`nginx/nginx.conf` for weighted or least-connections scheduling.

### Network isolation

| Service | Public port | Network |
|---|---|---|
| `nginx` | **80** (HTTP→HTTPS redirect), **443** (TLS) | `internal` |
| `fastapi` | none | `internal` |
| `redis` | none | `internal` |
| `mongo` | none | `internal` |

No FastAPI, Redis, or MongoDB port is published to the host. External traffic
enters through Nginx on port 443 (TLS-terminated); port 80 issues a permanent
301 redirect to HTTPS so no plaintext application traffic reaches the network.
The bridge is not flagged `internal: true` so FastAPI workers retain outbound
connectivity for aiodns SSRF validation.

### Liveness probe

```
GET /health  →  {"status": "ok"}
```

Used by the docker-compose `healthcheck` (`curl -f http://localhost:8000/health`).
Nginx waits for this to pass before accepting traffic.

### Daily analytics report (opt-in profile)

The `analytics` service reads the shared `telemetry_logs` volume (read-only)
and prints DAU, total redirects, and a click-count leaderboard for today's UTC
date.

```bash
# Today's report
docker compose --profile analytics run --rm analytics

# Specific date
docker compose --profile analytics run --rm analytics --date 2026-08-09

# Extend the leaderboard to top 50
docker compose --profile analytics run --rm -e TOP_N=50 analytics
```

### Tear down

```bash
# Stop containers, keep volumes
docker compose down

# Stop and delete all data (mongo_data + telemetry_logs)
docker compose down -v
```

---

## 7. Frontend

A single-page React application providing a clean UI for shortening URLs.

**Tech:** Vite 5 + React 18 + Tailwind CSS 3 — no Redux, no routing library.

**Features:**

- Centered layout with a large URL input and "Shorten →" button
- Async `POST /shorten` with loading spinner
- SSRF 400 errors displayed as a "REQUEST BLOCKED" banner with the exact server message
- Network errors surfaced as a user-friendly fallback message
- Success panel with the full short URL as a clickable `<a target="_blank" rel="noopener noreferrer">` link and a one-click Copy button
- "Start over" action resets all state and returns focus to the input

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `VITE_API_BASE` | _(empty)_ | Base URL for API calls (e.g. `http://localhost:8000`). Omit in production when the SPA is served behind the same Nginx origin as the API. |

### Running (development)

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

During development, Vite proxies `/shorten` to `http://localhost:80` (Nginx). Start the full stack with `docker compose up` before running the frontend dev server — FastAPI is not published directly on the host; all traffic routes through Nginx on port 80.

### Building for production

```bash
cd frontend
npm run build
# Static assets emitted to frontend/dist/
```

Serve `frontend/dist/` from Nginx's root so the SPA and API share the same origin, eliminating CORS and the need for `VITE_API_BASE`.

---

## 8. Limitations and Trade-offs

This section documents the known constraints of the current implementation — gaps between what the architecture specifies and what is actually deployed — and the trade-offs that were deliberately accepted.

---

### 8.1 Infrastructure — Single-Node Replica Set is Not Truly Highly Available

The Compose stack runs MongoDB as a **single-node replica set** (`rs0` with one member). This was chosen because a standalone `mongod` does not support `w="majority"` write concerns, and a true multi-node replica set requires either multiple physical hosts or a complex single-host Compose configuration with named volumes per node.

The consequence: if the `mongo` container crashes, the replica set has no secondary to elect as primary. Writes stall until the container restarts. The `restart: unless-stopped` policy mitigates this in practice, but the system is not HA in the production sense.

**To make it genuinely HA:** deploy a three-member replica set across separate hosts, or use MongoDB Atlas, which manages replica membership automatically.

---

### 8.2 Redis — No Persistence, Data Lost on Restart

Redis is configured with no `--appendonly` or `--save` flags (default: in-memory only). If the `redis` container is killed and restarted, the entire redirect cache is lost. The system remains **correct** — cache misses fall through to MongoDB — but redirect latency spikes until the cache is rebuilt by organic traffic.

**Accepted trade-off:** cache warm-up time after restart vs. the operational complexity of configuring RDB snapshots or AOF persistence. For a read cache with immutable values, a cold start is safe; it is a latency event, not a correctness event.

---

### 8.3 Nginx — Manual Upstream Configuration Does Not Auto-Discover Replicas

When scaling FastAPI with `docker compose up --scale fastapi=4`, Nginx resolves the upstream `server fastapi:8000` once at startup via Docker's embedded DNS. New containers started after Nginx has already launched are not automatically added to the upstream pool unless Nginx is reloaded (`nginx -s reload`).

The current `nginx.conf` lists one upstream server. Adding replicas requires adding `server fastapi:8000` lines and reloading Nginx, or switching to a service mesh / dynamic upstream discovery mechanism (e.g., Nginx Plus, Consul, or an Envoy-based proxy).

---

### 8.4 Telemetry — IP-Based DAU Is an Approximation

`compute_dau` counts unique source IPs per day. This produces a **lower bound**, not an exact unique-user count, for two reasons:

1. **NAT and shared egress.** An entire office, university, or mobile carrier behind a single public IP appears as one user regardless of how many individuals click.
2. **VPNs and proxies.** A single user switching VPN endpoints appears as multiple users.

The telemetry pipeline captures `X-Real-IP` (set by Nginx from `$remote_addr`), which is the IP of the immediate TCP peer — the real client when accessed directly, but the proxy's IP when users are behind a forward proxy.

**Accepted trade-off:** IP-based deduplication is simple and requires no user accounts or cookies. Exact unique-user counting would require either browser fingerprinting (privacy implications) or authentication (product scope change).

---

### 8.5 Telemetry — Fire-and-Forget Means Data Loss Is Possible

`asyncio.create_task(telemetry.record_redirect(...))` schedules log writes asynchronously. All exceptions inside `record_redirect` are silently swallowed. This means:

- If the log volume runs out of disk space, redirect events are silently dropped.
- If the worker process is killed between the 302 response and the log write completing, the event is lost.
- There is no dead-letter queue, retry mechanism, or alerting on telemetry write failures.

**Accepted trade-off:** A failed telemetry write must not fail or delay a redirect. Availability of the core product feature takes precedence over telemetry completeness. At high QPS, the window between the 302 and the write completing is microseconds — loss rate in practice is negligible.

---

### 8.6 ID Generation — Lease Leakage Creates Gaps in the Sequence Space

Each FastAPI worker claims a block of 10,000 sequence IDs from MongoDB at startup. If a worker is OOM-killed or evicted mid-lease, up to 9,999 IDs are permanently lost — never encoded into short codes, never stored in MongoDB. These gaps are **harmless for correctness** (the Feistel cipher remains bijective over the gaps) but mean the sequence counter advances faster than the URL count, narrowing the 3.5-trillion-key headroom over time.

At 100M URL creations per day with a 10,000-ID lease and ten container restarts per day, worst-case leakage is ~99,990 IDs/day — less than 0.1% of daily capacity. Over the 10-year horizon, this is negligible.

---

### 8.7 SSRF — Validation Window Between DNS Resolution and MongoDB Write

The SSRF validator resolves the target hostname at URL creation time. There is a small window — measured in milliseconds — between the DNS resolution completing and the URL being written to MongoDB. A sophisticated attacker controlling their DNS TTL could respond with a public IP during validation and rotate to a private IP afterwards (DNS rebinding).

**Mitigation in place:** the SSRF check runs before any sequence ID is consumed. There is no subsequent re-validation on the redirect path. The redirect route reads the stored URL from MongoDB and issues a 302 directly; it does not re-resolve the destination hostname.

**Residual risk:** a URL that was valid at creation time could, through DNS rebinding or destination-server-side redirects, eventually route a user's browser to an internal resource. The server itself is not the vector in this case — the user's browser is. This is a client-side risk outside the server's enforcement boundary.

---

### 8.8 Analytics — Offline Only, No Real-Time Dashboard

The `AnalyticsConsumer` reads from the log file offline. There is no streaming aggregation, no real-time dashboard, and no alerting. Metrics are available only by running `docker compose --profile analytics run --rm analytics`, which produces a point-in-time report.

**To add real-time analytics:** replace the file-append log with a message queue (Kafka, Redis Streams) and add a streaming consumer (Flink, Spark Structured Streaming, or a simple asyncio consumer). This was explicitly out of scope to avoid adding queue infrastructure to a system that already manages five containers.

---

### 8.9 No URL Expiration or Deletion

Short codes are permanent. There is no TTL on URL documents in MongoDB and no `DELETE /shorten/{code}` endpoint. Redis cache entries are permanent until LRU eviction. A URL created today will remain resolvable indefinitely.

**Accepted trade-off:** expiration requires either a background TTL-sweeper job (additional operational complexity) or MongoDB TTL indexes on a `created_at` field (not currently stored). Deletion requires an authenticated admin API (out of scope for this prototype). Permanence is the simpler and safer default for a prototype.

---

### 8.10 TLS — Self-Signed Certificate, CA Sign-Off Required Before Internal Deployment

**Risk classification: unmitigated security risk without explicit security sign-off.**

Without encryption in transit, `POST /shorten` request bodies are visible in plaintext to any process on the host network. In internal tooling, destination URLs frequently carry OAuth authorisation codes, session tokens, and sensitive query parameters — making plaintext transmission a data-exposure risk regardless of whether the network is "private."

**What is in place:** The Compose stack now terminates TLS at Nginx using a self-signed RSA-2048 certificate generated at image build time (`nginx/Dockerfile`). Port 80 issues a permanent HTTP 301 → HTTPS redirect; no plaintext application traffic reaches the network. TLSv1.0 and TLSv1.1 are explicitly excluded (`ssl_protocols TLSv1.2 TLSv1.3`) in compliance with RFC 8996 and PCI-DSS 3.2+.

**What requires sign-off before internal deployment:**

- **Certificate authority.** The self-signed cert triggers browser warnings and provides no identity assurance. Replace with a CA-signed certificate (internal PKI, Let's Encrypt, or ACM) before any internal deployment. This step requires the security team to approve the CA chain and validate the SAN list.
- **Certificate rotation.** No automated renewal is configured. Manual rotation before the 10-year expiry must be scheduled and owned by the operating team.
- **mTLS for service-to-service traffic.** Traffic between Nginx and FastAPI travels over the unencrypted Docker bridge. For a regulated environment, mutual TLS between all service pairs (or a service mesh with automatic cert provisioning) is the production-grade control.

**Production path:** replace the self-signed cert with a CA-signed certificate, add OCSP stapling (`ssl_stapling on`), and terminate mTLS at the service mesh layer or via a sidecar proxy.
