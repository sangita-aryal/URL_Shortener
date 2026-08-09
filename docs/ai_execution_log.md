# AI Execution Log — URL Shortener

**Project:** URL Shortener  
**Repository:** URL_Shortener  
**AI Assistant:** Claude Sonnet 4.6 (Anthropic)  
**Log Date:** 2026-08-09  
**Total PRs merged:** 9 (branches `sangita-aryal/feature/*`)  
**Total commits:** 20 (including merges, reverts, and patches)

This document traces every major decision across the project lifecycle: what AI proposed, what was accepted unchanged, what was revised, what was rejected outright, and what alternatives were evaluated and discarded before a final design was chosen. It is intended as an auditable record of human–AI collaboration for accountability, reproducibility, and future architectural review.

---

## How to read this document

Each entry is structured as:

- **AI proposed** — the design or implementation AI introduced
- **Accepted as-is** — proposals the team adopted without modification
- **Rejected** — proposals that were explicitly discarded (with the rejection commit or reason)
- **Modified before acceptance** — proposals that were partially revised
- **Alternatives AI evaluated and discarded** — options AI explicitly ruled out before writing any code
- **Bugs AI introduced** — errors in AI-generated code that required a follow-up fix

---

## Entry 1 — Feistel Cipher & Distributed ID Generation

**Files:** [`app/id_generator.py`](../app/id_generator.py), [`tests/test_id_generator.py`](../tests/test_id_generator.py), [`tests/conftest.py`](../tests/conftest.py)  
**Initial commit:** `573dd0e` (PR #1)  
**Revised:** `bed7773` (lease size), `42fee0d` (docs)

---

### Alternatives AI Evaluated and Discarded

Before writing a single line of implementation, AI considered three canonical approaches and documented why each was incompatible with the system's requirements. These were later formalized in commit `304a6b3`, then pulled out of the README into this document (see [Rejected — "Evaluated Alternatives" README section](#rejected--evaluated-alternatives-readme-section) below).

#### Discarded: Relational DB Auto-Increment + Multi-Master Replication

The most commonly taught approach is a single auto-incrementing primary key in a relational database, with multi-master step replication to distribute writes (Server A issues 1, 3, 5 …; Server B issues 2, 4, 6 …).

**Reasons discarded:**

1. **Enumeration vulnerability.** Sequential IDs are trivially guessable. An attacker knowing one valid short code can iterate every URL ever created by incrementing. There is no cryptographic barrier between one valid code and all of them.
2. **Multi-master rigidity.** The step size must be fixed at cluster inception. Adding or removing a node mid-operation requires a coordinated migration; the scheme does not preserve chronological ordering across nodes; and operationally it is brittle in ways that conflict with the system's stateless scaling model.

#### Discarded: MD5/SHA-256 Hash Truncation

Hashing the destination URL and truncating to 7 characters is stateless and requires no coordination.

**Reasons discarded:**

1. **Birthday-paradox collision.** A 7-character truncation retains only 41 bits of the original digest. By the birthday paradox, collision probability reaches 50% after approximately **1.5 million URLs** — far below the system's 365-billion-record 10-year horizon.
2. **Read-before-write penalty.** Each collision requires a round-trip to the database to check whether the hash slot is occupied, then a retry with a salt or counter. This doubles write-path latency in the common case and introduces an unbounded retry loop in the worst case — directly violating the low-latency write requirement.

#### Discarded: UUID v4

UUID v4 is collision-resistant in practice but is **36 characters long** (e.g., `550e8400-e29b-41d4-a716-446655440000`). A full UUID as a short code produces URLs longer than most destination URLs, defeating the product requirement. Truncating a UUID to 7 characters strips its collision-resistance and reintroduces the birthday-paradox collision risk described above.

---

### AI Proposed

A three-layer pipeline: `SequenceLeaseManager → FeistelCipher → Base-62 encoder`.

**FeistelCipher** — 42-bit, 4-round balanced Feistel network. Splits each integer into two 21-bit halves and applies four keyed mixing rounds. The Feistel structure guarantees bijectivity (injectivity + surjectivity) structurally — for any round function, `decrypt(encrypt(x)) == x` for all `x ∈ [0, 2^42)`. Round keys loaded from environment variables.

**SequenceLeaseManager** — Atomically acquires a block of sequential IDs from MongoDB via a single `find_one_and_update` with `$inc`. All subsequent IDs within that block are pure in-memory increments. An `asyncio.Lock` serialises concurrent coroutines within a single worker.

**IDGenerator** — Thin integration: `seq_id → cipher.encrypt(seq_id) → Base-62 encode`. Cycle-walking handles the domain mismatch between Feistel's `[0, 2^42)` and Base-62's `[0, 62^7)`: if the output exceeds `62^7 - 1`, follow the permutation chain until it lands in range. Guaranteed termination because the cipher is a bijection of a finite set.

**42-bit domain rationale:** `62^7 = 3,521,614,606,208 ≈ 2^41.7`. Choosing 42 bits means two symmetric 21-bit halves, which is required for a balanced Feistel; 41 bits would produce asymmetric 20-/21-bit halves.

**Initial lease size proposed:** `DEFAULT_LEASE_SIZE = 1_000_000`

---

### Accepted as-is

| Item | Why accepted |
|---|---|
| 4-round Feistel cipher | Bijectivity proven structurally; 4 rounds provides strong dispersion with minimal fixed-point risk |
| 42-bit / 21-bit symmetric halves | Mathematically correct split for a balanced Feistel; matches 7-char Base-62 keyspace |
| Cycle-walking FPE for domain mismatch | No wasted sequence IDs; terminates because permutation is finite; simpler than rejection sampling |
| `asyncio.Lock` for lease serialisation | Correct concurrency primitive for a single asyncio event loop per Uvicorn worker |
| Round keys from environment variables | Allows per-deployment key rotation without an image rebuild, preventing short-code enumeration across deployments |
| `IDGenerator` constructed per-request | Stateless shim; stateful `SequenceLeaseManager` is the singleton; cheap to instantiate |
| Contract-first TDD for all 30+ tests | Established correctness guarantees (bijectivity, non-collision, O(1) complexity) before implementation existed |

---

### Modified Before Acceptance — Lease Size: 1,000,000 → 10,000

**Commit:** `bed7773`  
**Original proposal:** `DEFAULT_LEASE_SIZE = 1_000_000`

After the initial implementation landed, AI raised a fault-tolerance concern: Uvicorn workers run inside stateless containers subject to OOM kills, rolling deployments, and node preemptions. When a worker is killed mid-lease, every claimed-but-unused ID is permanently lost — creating gaps in the sequence space.

With a lease of 1,000,000, a single OOM kill can permanently leak up to **999,999 IDs**. Over a fleet of containers with regular restarts, this leakage accumulates and pushes the global counter far ahead of the number of URLs actually created.

**Revised to:** `DEFAULT_LEASE_SIZE = 10_000`

| Metric | Value |
|---|---|
| Worst-case leak per crash (1M lease) | 999,999 IDs |
| Worst-case leak per crash (10K lease) | 9,999 IDs — **99% reduction** |
| Daily lease fetches at 100M creations/day | 10,000 MongoDB round-trips — negligible |

**Cascading test changes required by this revision:**
- `TestSequenceLeaseManagerConstants`: `test_default_lease_size_is_one_million` → `test_default_lease_size_is_ten_thousand`
- `TestSequenceLeaseManagerOOneComplexity`: inner loop reduced from 10,000 to 5,000 iterations to stay within a single 10,000-ID lease after the warm-up call consumed the first ID

---

### Rejected — "Evaluated Alternatives" README Section

**Commit added:** `304a6b3`  
**Commit reverted:** `cda7695` (1 minute 42 seconds later)

AI added a 48-line "Evaluated Alternatives & Trade-offs" section to `README.md` covering the three discarded alternatives above. The section was immediately reverted. The rationale: alternatives analysis belongs in an AI traceability document (this file), not in the user-facing README, which should focus on how to use and operate the system rather than on the design process.

**Content preserved here** rather than in README.

---

## Entry 2 — SSRF Validator

**Files:** [`app/ssrf_validator.py`](../app/ssrf_validator.py), [`tests/test_ssrf_validator.py`](../tests/test_ssrf_validator.py)  
**Initial commit:** `573dd0e` (PR #1)

---

### Alternatives AI Evaluated and Discarded

#### Discarded: `socket.getaddrinfo` for DNS Resolution

`socket.getaddrinfo` is the standard Python DNS function and would have been the most natural choice.

**Reason discarded:** `socket.getaddrinfo` is a synchronous blocking syscall. On an ASGI event loop it stalls every concurrent coroutine for the entire duration of the DNS round-trip (50–300 ms typical under load). With 11,600 read QPS, stalling the event loop on every write-path DNS lookup would saturate workers and cascade into redirect latency degradation.

**Chosen instead:** `aiodns.DNSResolver.gethostbyname()` — wraps the c-ares async resolver; the coroutine yields to the event loop while the DNS query is in flight, allowing concurrent requests to proceed unimpeded.

#### Discarded: Fail-Open on DNS Errors

An alternative policy would be to permit URLs when DNS resolution fails (e.g., timeout, NXDOMAIN), on the grounds that an unresolvable hostname can't be reached anyway.

**Reason discarded:** Unresolvable ≠ safe. An attacker controlling internal DNS can return NXDOMAIN for a hostname that resolves to a private IP only within the internal network — exploiting the fail-open policy to smuggle a private-IP target through validation. AI chose **fail-closed**: any DNS error is treated as a block. Telemetry loss from a failed redirect is less harmful than a successful SSRF.

#### Discarded: Allowlisting Specific Public Domains

Maintaining an allowlist of permitted domains (e.g., `*.github.com`, `*.twitter.com`) and rejecting everything else.

**Reason discarded:** Severely limits the product utility (a URL shortener that only accepts GitHub/Twitter URLs is not general-purpose), requires constant maintenance, and still doesn't protect against DNS rebinding on allowlisted domains. The denylist approach (block private ranges, permit everything else) is more defensible and scales to arbitrary public URLs.

#### Discarded: Server-Side Prefetch for Redirect Validation

Fetching the destination URL server-side on every redirect to verify it's safe before issuing the 302.

**Reason discarded:** Would introduce a secondary SSRF surface on the read path. A URL that was valid at creation time could later be updated (via DNS change or server-side redirect chain) to point to an internal resource. More critically, it would make the redirect path dependent on outbound HTTP latency rather than an in-memory cache lookup. AI explicitly called this out in the `app/app.py` route comment: "The URL is placed directly in the Location header — no server-side fetch avoids a secondary SSRF surface."

---

### AI Proposed

A six-step validation pipeline executed on every `POST /shorten` before any sequence ID is consumed:

1. **Empty/whitespace guard** — reject blank input before any parsing
2. **Scheme allowlist** — only `http` and `https`; case-insensitive; all other schemes (`file://`, `ftp://`, `javascript:`, `data:`, `gopher://`, `dict://`) blocked
3. **Host extraction** — `urlparse.hostname` (auto-lowercased, IPv6 brackets stripped)
4. **Hostname denylist** — `localhost` blocked by string match regardless of what it resolves to
5. **Literal IP fast-path** — if host parses as `ipaddress.ip_address`, range-check directly; skip DNS. Handles IPv4-mapped IPv6 (`::ffff:192.168.1.1`) and zone-ID stripping (`fe80::1%25eth0`)
6. **Async DNS resolution** — `aiodns.DNSResolver.gethostbyname(host, AF_INET)`; all resolved addresses range-checked; DNS error treated as block

**Blocked ranges:**

| Range | Type |
|---|---|
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | RFC 1918 private |
| `127.0.0.0/8` | IPv4 loopback |
| `169.254.0.0/16` | Link-local / AWS EC2 metadata |
| `::1/128` | IPv6 loopback |
| `fe80::/10` | IPv6 link-local |
| `fc00::/7` | IPv6 unique local |
| IPv4-mapped IPv6 with private IPv4 | Bypass prevention |

**Resolver injected via FastAPI `Depends()`** — never instantiated inside the handler, making DNS resolution mockable without monkey-patching.

**SSRF check runs before ID generation** — a rejected URL consumes no sequence numbers.

---

### Accepted as-is

| Item | Why accepted |
|---|---|
| Fail-closed on DNS error | Safety over availability; unknown = unsafe |
| Literal IP fast-path (skip DNS for IPs) | Avoids unnecessary DNS calls; prevents DNS rebinding on literal addresses |
| `aiodns` exclusively (no `getaddrinfo`) | Non-blocking; correct for ASGI |
| Full IPv6 private range coverage | Zone-ID stripping and IPv4-mapped checks prevent trivial bypass vectors |
| Scheme allowlist (not denylist) | New dangerous schemes emerge; allowlist is future-proof |
| Resolver dependency injection | Testable without live DNS infrastructure; consistent with project's DI pattern |
| Validate before consuming a sequence ID | Ensures rejected URLs waste no short-code slots |
| 40+ contract tests covering every boundary | Exhaustive; each test proves a specific claim about the API contract |

---

## Entry 3 — URL Persistence Layer

**Files:** [`app/url_repository.py`](../app/url_repository.py), [`tests/test_url_repository.py`](../tests/test_url_repository.py)  
**Commit:** `929d2dd` (PR #2)

---

### Alternatives AI Evaluated and Discarded

#### Discarded: Write-Through Cache (Populate Redis on Write)

The most common cache strategy is write-through: when `save()` is called, write to both MongoDB and Redis simultaneously so the first read is always a cache hit.

**Reasons discarded:**

1. **Dual-write consistency risk.** If the MongoDB write succeeds but the Redis write fails (or vice versa), the two stores diverge. Handling this correctly requires a distributed transaction or a compensating saga — significant complexity for a system designed to be simple and stateless.
2. **Unnecessary on a cold start.** Many created URLs are never clicked. Writing every URL to Redis on creation wastes cache memory on entries that will never be read from cache.
3. **Chosen instead:** Read-through (lazy population on cache miss). Redis is populated only when a URL is first looked up. If it's never clicked, it never enters the cache.

#### Discarded: No Redis TTL on Cached Entries

An initial option was to cache entries indefinitely (no TTL), relying on Redis LRU eviction to manage memory.

**Why not documented as rejected:** The implementation does not set a TTL (`redis.set(key, value)` with no `ex=` argument). This was a deliberate choice: short codes are permanent (URLs are never deleted), so a cached entry is always valid. LRU eviction still removes cold entries under memory pressure. AI did not add a TTL because it would introduce spurious cache misses for valid, permanent mappings.

#### Discarded: Null-Cache Poisoning (Cache "Not Found" Results)

A common optimisation is to write a sentinel value to Redis when a lookup returns nothing in MongoDB, preventing repeated database queries for nonexistent short codes.

**Reason discarded:** Introduces a consistency hazard — if a URL is created after a null entry is cached, the cache will serve "not found" until the sentinel expires. The simpler invariant ("Redis is only written when a real URL is found") is safer and correct for a system where short codes are permanent.

---

### AI Proposed

`URLRepository` — a single abstraction over Redis and MongoDB implementing the **read-through cache** pattern.

**Write path (`save`):**
- `insert_one({"_id": short_code, "url": url})` on the Motor `urls` collection
- Redis is **not** touched; cache population is strictly lazy

**Read path (`get`):**
1. `redis.get(short_code)` — cache hit → decode bytes → return immediately; MongoDB never queried
2. Cache miss → `collection.find_one({"_id": short_code})`
3. Found → `redis.set(short_code, url)` → return URL
4. Not found → return `None`; Redis **not** written

The exact call sequence `redis.get → mongo.find_one → redis.set` is a first-class contract verified by `TestReadOrder` using a call log rather than mock assertions (which cannot prove ordering).

---

### Accepted as-is

| Item | Why accepted |
|---|---|
| Read-through (not write-through) cache | No dual-write; no null-cache poisoning; natural behaviour for permanent immutable URLs |
| MongoDB `_id` = short code | Zero-index lookup; no secondary index needed; shard-key routable |
| No null-cache sentinel | Simpler invariant; permanent short codes make null caching unnecessary |
| `decode_responses=False` on Redis client | Returns raw bytes; `decode()` called explicitly; avoids silent encoding failures |
| Call-log test for read ordering | The only reliable way to prove sequencing; mock assertion counts alone cannot prove `get` happened before `find_one` |
| 18 contract tests across 5 classes | Each class owns one contract (write path, cache hit, cache miss, not found, read order) |

---

## Entry 4 — Telemetry Pipeline

**Files:** [`app/telemetry.py`](../app/telemetry.py), [`app/analytics.py`](../app/analytics.py), [`scripts/report.py`](../scripts/report.py), [`tests/test_telemetry.py`](../tests/test_telemetry.py)  
**Commit:** `9f41e47` (PR #3)

---

### Alternatives AI Evaluated and Discarded

#### Discarded: Redis Counters for Click Analytics

The most natural approach given Redis is already in the stack: increment a counter per short code (`INCR aB3cD4e`) and a HyperLogLog per day for DAU (`PFADD dau:2026-08-09 <ip>`).

**Reasons discarded:**

1. **RAM exhaustion and LRU eviction of hot redirect mappings.** Redis is sized for the redirect cache. Adding analytics counters competes directly with cache memory. Under memory pressure, Redis LRU eviction removes entries — if analytics counters displace hot redirect mappings, cache miss rates rise, MongoDB load increases, and redirect latency degrades. The telemetry pipeline would actively harm the core product feature.
2. **DAU accuracy with HyperLogLog.** HyperLogLog provides ~2% relative error, which is acceptable for DAU approximations but cannot produce exact counts for auditing or SLA compliance.
3. **Chosen instead:** Append-only structured JSON log file per worker, consumed offline by a streaming Python script.

#### Discarded: Synchronous File Write on the Hot Path

Writing to the log file synchronously inside the redirect route handler, before returning the 302.

**Reason discarded:** File I/O is blocking. On an ASGI event loop, a synchronous `open()` + `write()` stalls every concurrent coroutine for the duration of the disk write — tens to hundreds of microseconds, potentially milliseconds on a slow storage backend. At 11,600 redirect QPS, this would be a significant throughput bottleneck.

**Chosen instead:** `asyncio.create_task(telemetry.record_redirect(...))` — schedules the log write as a fire-and-forget background task. The route handler returns the 302 immediately; the write happens concurrently.

#### Discarded: Loading the Entire Log File Into Memory for Analytics

Reading `open(log_path).read()` or `json.load()` for analytics processing.

**Reason discarded:** Log files accumulate gigabytes of entries over time (at 100M redirects/day, even 100 bytes per entry = 10 GB/day). Loading the entire file would cause OOM crashes on any machine with less RAM than the total log size.

**Chosen instead:** Generator-based streaming (`stream_entries` is a generator that yields one parsed dict per line). The entire file is never in memory simultaneously.

#### Discarded: Thread-Per-Write Approach

Spawning a new thread for each file write using `threading.Thread`.

**Reason discarded:** Thread creation is expensive; 11,600 redirect QPS × thread-spawn overhead would exhaust the OS thread limit rapidly. `asyncio.to_thread()` reuses the shared thread-pool executor, capping concurrency at the executor's thread limit while keeping the event loop free.

---

### AI Proposed

**TelemetryLogger (write path):**
- `record_redirect(short_code, client_ip)` is `async` — scheduled via `asyncio.create_task()` in the route handler; 302 is returned before the log write starts
- `asyncio.to_thread(self._append, entry)` offloads the blocking `open()` + `write()` to the thread-pool executor
- File opened in `"a"` (append) mode — concurrent workers never overwrite each other's entries
- All exceptions silently swallowed — telemetry loss is preferable to a failed redirect
- Entry format: `{"ts": "<ISO-8601 UTC>", "code": "...", "ip": "..."}` — UTC timestamp prevents cross-day DAU bleed when logs from multiple time zones are merged

**AnalyticsConsumer (read path — offline):**
- `stream_entries(log_path)` — generator; reads line-by-line; skips blank lines and malformed JSON silently
- `entries_for_date(log_path, target_date)` — lazy generator filter over `stream_entries`; UTC date comparison prevents cross-day bleed
- `compute_dau(entries)` — `set[str]` deduplication; O(U) space bounded by unique IP count, not click volume
- `compute_total_clicks(entries)` — `collections.Counter`; counts per short code

**`scripts/report.py`** — CLI driver: pipes `entries_for_date → compute_dau` + `compute_total_clicks` into a terminal table. Accepts `--date YYYY-MM-DD` and `TOP_N` env var.

---

### Accepted as-is

| Item | Why accepted |
|---|---|
| File-append log over Redis counters | Eliminates RAM competition with the redirect cache; exact counts; no LRU risk |
| Fire-and-forget via `asyncio.create_task()` | 302 latency is unaffected by telemetry; telemetry failure cannot cause a redirect failure |
| `asyncio.to_thread()` for file I/O | Non-blocking on the event loop; thread-pool reuse avoids thread-spawn overhead |
| Swallow all exceptions in TelemetryLogger | Availability > telemetry completeness |
| UTC timestamps | Cross-server log merging safety; deterministic DAU date boundaries |
| Generator-based streaming | O(1) memory regardless of log file size |
| Set-based DAU (`set[str]`) | O(U) space; correct deduplication semantics; exact count (not approximate) |
| Silent skip of malformed lines | A single corrupted write from disk full or race condition cannot halt an analytics run |
| 28 contract tests | Covers format, timestamp contract, async/error isolation, streaming, DAU, clicks, date filtering |

---

## Entry 5 — Docker Orchestration

**Files:** [`docker-compose.yaml`](../docker-compose.yaml), [`Dockerfile`](../Dockerfile), [`nginx/nginx.conf`](../nginx/nginx.conf)  
**Initial commits:** `a17e1a6` + `125f4a7` (PR #4)  
**Patches:** `147c0a1` (PR #8)

---

### Alternatives AI Evaluated and Discarded

#### Discarded: `internal: true` Network Bridge

Docker Compose supports an `internal: true` flag on a bridge network that blocks all outbound connectivity from containers on that network.

**Reason discarded:** FastAPI workers need outbound DNS connectivity to call `aiodns.DNSResolver.gethostbyname()` during SSRF validation at request time. An `internal: true` network would block these DNS queries, breaking the SSRF shield entirely for hostname URLs. AI explicitly documented this in the Compose file comment:

> *"The bridge flag is NOT set to `internal: true` so that FastAPI workers can make outbound aiodns calls for SSRF validation. Isolation is enforced solely by the absence of host port mappings on internal services."*

**Chosen instead:** Standard bridge with no `internal` flag. Network isolation is enforced by the absence of `ports:` declarations on internal services, not by OS-level network blocking.

#### Discarded: Standalone MongoDB (No Replica Set)

Running MongoDB as a standalone `mongod` instance is simpler and faster to start.

**Reason discarded:** Standalone MongoDB does not support `w="majority"` write concern. The Motor client is configured with `w="majority"` + `journal=True` to ensure every URL document is durably acknowledged by a majority of replica nodes before the 201 is returned — preventing data loss on primary failover. A standalone instance would silently downgrade this to `w=1`, removing the durability guarantee.

**Chosen instead:** Single-node replica set `rs0` (`mongod --replSet rs0`). A single-node replica set is operationally equivalent to standalone but supports majority write concerns. The `mongo-init` one-shot container calls `rs.initiate()` wrapped in a `try/catch` for idempotency.

#### Discarded: Inlining MongoDB Init into the `mongo` Service Command

Running `rs.initiate()` directly in the `mongo` service's `command:` field.

**Reason discarded:** `rs.initiate()` requires the mongod process to be fully started and listening before it runs. Running it in the same command as `mongod` creates a race condition. The `mongo-init` sidecar pattern — a separate container that depends on `mongo: condition: service_healthy` — guarantees the replica set is only initialised after the healthcheck confirms mongod is ready.

#### Discarded: Always-On Analytics Container

Running the analytics consumer as a persistent background service alongside the main stack.

**Reason discarded:** The analytics consumer reads from a log file — it has nothing to do when no new entries exist, and running it continuously wastes container resources. More importantly, a long-running analytics container would need to handle log rotation and file truncation, significantly complicating the implementation.

**Chosen instead:** Opt-in via `profiles: [analytics]`. The container is never started with the main stack and is invoked on-demand via `docker compose --profile analytics run --rm analytics`. It runs to completion and exits.

#### Discarded: HTTP 301 (Permanent) Redirect

HTTP 301 is the conventional redirect status code for URL shorteners and is cached aggressively by browsers.

**Reason discarded:** Browser caching breaks the telemetry pipeline. On a 301, the browser caches the destination URL and on subsequent clicks goes directly to the destination without ever contacting the URL shortener. The telemetry logger (which records redirects server-side) would see only the first click from any given browser for a given short code — all subsequent clicks are invisible. DAU and total-click counts would be severely undercounted.

**Chosen instead:** HTTP 302 (Found, temporary redirect). Browsers do not cache 302 responses; every click reaches the server and is recorded by `TelemetryLogger`.

| | HTTP 301 | HTTP 302 |
|---|---|---|
| Browser caching | Yes — first click only | No — every click reaches server |
| Telemetry accuracy | Severely undercounted | Accurate |
| SEO | Full link equity transferred | Reduced link equity |
| Redirect latency (repeat clicks) | Zero (browser cache) | Network round-trip |

For a system whose core value proposition is analytics, telemetry accuracy takes precedence over SEO and repeat-click latency.

#### Discarded: Publishing FastAPI Port on the Host

Mapping `fastapi:8000` to the host (e.g., `ports: ["8000:8000"]`) for easier local development.

**Reason discarded:** Direct FastAPI access bypasses Nginx's rate limiter and the `X-Real-IP` header injection. Telemetry would record Nginx's container IP instead of the originating client IP. Publishing the port also means production deployments (where this Compose file is authoritative) would accidentally expose the ASGI app directly to the internet.

**Chosen instead:** No `ports:` on `fastapi`. All traffic enters through Nginx. A Vite dev proxy (`/shorten → http://localhost:80`) routes frontend development traffic through the same Nginx entry point.

---

### AI Proposed

Six-service Compose stack with strict network isolation:

| Service | Image | Ports | Notes |
|---|---|---|---|
| `nginx` | `nginx:1.27-alpine` | `80:80` | Sole public ingress |
| `fastapi` | Custom (`python:3.13-slim`) | None | 4 Uvicorn workers; healthcheck: `curl -f /health` |
| `redis` | `redis:7-alpine` | None | healthcheck: `redis-cli ping` |
| `mongo` | `mongo:7` | None | `--replSet rs0`; healthcheck: `mongosh ping` |
| `mongo-init` | `mongo:7` | None | One-shot; idempotent `rs.initiate()`; `restart: "no"` |
| `analytics` | Same as fastapi | None | `profiles: [analytics]`; log volume read-only; `restart: "no"` |

Startup order enforced by `depends_on` + `condition`:  
`mongo (healthy) → mongo-init (completed_successfully) → fastapi (healthy) ←→ redis (healthy) → nginx`

Nginx rate limiting: 200 req/s per IP, burst queue 500, `nodelay` (reject rather than queue excess).  
Upstream keepalive: 64 persistent connections to fastapi workers for connection reuse at high QPS.  
Proxy headers: `X-Real-IP $remote_addr` — so TelemetryLogger records the originating client IP, not Nginx's container IP.

---

### Accepted as-is

| Item | Why accepted |
|---|---|
| No `internal: true` network flag | Required for outbound aiodns DNS calls in SSRF validation |
| Single-node replica set (`rs0`) | Only way to support `w="majority"` write concern without operational complexity |
| `mongo-init` sidecar pattern | Race-condition-free initialisation; idempotent on stack restarts |
| HTTP 302 for redirects | Ensures every click reaches the telemetry logger; 301 would cache on the browser |
| Analytics as opt-in profile | No idle container overhead; no log-rotation handling needed |
| `X-Real-IP` header passthrough | Telemetry records the client IP, not Nginx's container IP |
| `server_tokens off` in Nginx | Suppresses version disclosure in error responses |
| `curl` installed in `python:3.13-slim` Dockerfile | Required only for the Compose healthcheck probe; explicit in comments |

---

### Bugs AI Introduced

#### Bug 1: `j=True` instead of `journal=True` in Motor Client

**Introduced in:** `573dd0e` (initial commit)  
**Fixed in:** `147c0a1` (PR #8)  
**Symptom:** FastAPI crashed on startup with `ConfigurationError` from PyMongo when running inside Docker Compose, putting the service into a restart loop. Nginx had no healthy upstream and returned ECONNREFUSED to the frontend.

```python
# AI-generated (wrong):
motor.motor_asyncio.AsyncIOMotorClient(_MONGO_URI, w="majority", j=True)

# Corrected:
motor.motor_asyncio.AsyncIOMotorClient(_MONGO_URI, w="majority", journal=True)
```

Modern PyMongo (3.x+) rejects the short-form `j` option. The short-form was valid in older PyMongo versions; the API changed and AI generated stale code.

#### Bug 2: Vite Dev Proxy Pointed at FastAPI Directly

**Introduced in:** `e63cd71` (frontend commit)  
**Fixed in:** `147c0a1` (PR #8)  
**Symptom:** Running `npm run dev` with Docker Compose produced ECONNREFUSED errors on `POST /shorten` because `localhost:8000` is not published to the host.

```js
// AI-generated (wrong):
proxy: { '/shorten': { target: 'http://localhost:8000', changeOrigin: true } }

// Corrected:
proxy: { '/shorten': { target: 'http://localhost:80', changeOrigin: true } }
```

FastAPI is not published on any host port. The only entry point is Nginx on port 80. The Vite proxy must route through Nginx to match the production path.

---

## Entry 6 — Documentation Architecture

**Files:** `README.md`, `CLAUDE.md` (deleted), `architect.md` (deleted)  
**Commits:** `573dd0e` (initial spec files), `42fee0d` (consolidation + deletion), `304a6b3` + `cda7695` (alternatives section — added then reverted)

---

### AI Proposed

Initial commit `573dd0e` created two separate spec files:
- `CLAUDE.md` — project instructions and architectural constraints
- `architect.md` — a 40-line architecture specification used as the source of truth for section numbering (§1 through §6) referenced in all implementation docstrings

### Accepted Initially, Then Revised

The two-file approach was used through PRs #1–4. In commit `42fee0d`, both files were deleted and their content merged into `README.md` as the single source of truth. The section numbering (§3 through §6) was retained in docstrings but now refers to README sections rather than a separate spec file.

**Reason for consolidation:** Three separate documents (`CLAUDE.md`, `architect.md`, `README.md`) with overlapping content created a maintenance burden. Any architectural change would require updating three files. The README is the document most likely to be read by new contributors; concentrating all architecture documentation there reduces the risk of spec/implementation drift.

### Rejected — Alternatives Section in README

AI added a "Evaluated Alternatives & Trade-offs" section to `README.md` (commit `304a6b3`) documenting the three discarded ID generation approaches. This was reverted 102 seconds later (commit `cda7695`).

**Reason for rejection:** The README is the operational handbook for anyone deploying or contributing to the system. Rejected-approach analysis is process documentation that belongs in an AI traceability log (this file), not in the operational README. Keeping process and operation documentation separate prevents the README from becoming a design-archaeology document.

---

## Cross-Cutting Summary

### Proposals Accepted Without Modification

| Category | Proposal | Entry |
|---|---|---|
| ID Generation | 4-round, 42-bit Feistel cipher | 1 |
| ID Generation | Cycle-walking for domain mismatch | 1 |
| ID Generation | `asyncio.Lock` for lease serialisation | 1 |
| ID Generation | Round keys from environment variables | 1 |
| ID Generation | TDD contract-first test methodology | 1 |
| SSRF | Fail-closed on DNS error | 2 |
| SSRF | `aiodns` exclusively (no `getaddrinfo`) | 2 |
| SSRF | Literal IP fast-path (skip DNS) | 2 |
| SSRF | Scheme allowlist (not denylist) | 2 |
| SSRF | Resolver via dependency injection | 2 |
| SSRF | Validate before consuming sequence IDs | 2 |
| Persistence | Read-through cache (not write-through) | 3 |
| Persistence | No null-cache poisoning | 3 |
| Persistence | Call-log test for read ordering | 3 |
| Telemetry | File-append log (not Redis counters) | 4 |
| Telemetry | Fire-and-forget via `asyncio.create_task()` | 4 |
| Telemetry | `asyncio.to_thread()` for file I/O | 4 |
| Telemetry | Swallow all exceptions in TelemetryLogger | 4 |
| Telemetry | Generator-based streaming analytics | 4 |
| Telemetry | Set-based DAU computation | 4 |
| Docker | No `internal: true` network flag | 5 |
| Docker | Single-node replica set for `w="majority"` | 5 |
| Docker | `mongo-init` sidecar pattern | 5 |
| Docker | HTTP 302 (not 301) redirects | 5 |
| Docker | Analytics as opt-in Compose profile | 5 |
| Docker | `X-Real-IP` header passthrough via Nginx | 5 |

### Proposals Modified Before Acceptance

| Proposal | What Changed | Why | Entry |
|---|---|---|---|
| `DEFAULT_LEASE_SIZE = 1_000_000` | Reduced to 10,000 | 99% reduction in crash-leakage; DB load remains trivial | 1 |
| Vite proxy → `localhost:8000` | Changed to `localhost:80` | FastAPI is not published; Nginx is the entry point | 5 |
| `MONGO_URI` default without replica set param | Added `?replicaSet=rs0` | Motor requires the param to honour `w="majority"` in Compose | 5 |
| Two separate spec files (`CLAUDE.md`, `architect.md`) | Deleted and merged into README | Single source of truth; eliminates spec/implementation drift | 6 |

### Proposals Rejected

| Proposal | Reason | Commit |
|---|---|---|
| "Evaluated Alternatives" section in README | Process docs belong in traceability log, not operational README | `cda7695` (revert of `304a6b3`) |
| `j=True` on Motor client | Wrong parameter name; crashes FastAPI startup | Fixed in `147c0a1` |

### Alternatives AI Evaluated and Discarded (Never Implemented)

| Alternative | Reason Discarded | Entry |
|---|---|---|
| Relational DB auto-increment | Enumerable short codes; multi-master rigidity | 1 |
| MD5/SHA-256 hash truncation | Birthday collision at ~1.5M URLs; read-before-write penalty | 1 |
| UUID v4 | 36 characters; defeats URL shortener requirement | 1 |
| `socket.getaddrinfo` for DNS | Blocks the ASGI event loop | 2 |
| Fail-open on DNS error | Unknown resolution = unverifiable safety | 2 |
| Domain allowlist for SSRF | Limits utility; requires constant maintenance; doesn't prevent rebinding | 2 |
| Server-side prefetch on redirect | Secondary SSRF surface on read path; latency dependency | 2 |
| Write-through cache | Dual-write consistency risk; wastes RAM on unread URLs | 3 |
| Redis counters for telemetry | LRU eviction displaces hot redirect-cache entries | 4 |
| Synchronous file write on hot path | Blocks event loop; degrades redirect throughput | 4 |
| Thread-per-write for file I/O | Thread-spawn overhead at 11,600 QPS; exhausts OS thread limit | 4 |
| Load entire log file for analytics | OOM on multi-gigabyte log files | 4 |
| `internal: true` Docker network | Blocks outbound aiodns DNS calls; breaks SSRF validation | 5 |
| Standalone MongoDB | No `w="majority"` support | 5 |
| Inline `rs.initiate()` in mongo command | Race condition; mongod not ready when init runs | 5 |
| Always-on analytics container | Idle resource waste; log-rotation complexity | 5 |
| HTTP 301 redirect | Browser-cached; telemetry sees only first click per browser | 5 |
| Publishing FastAPI port on host | Bypasses Nginx rate limiter and `X-Real-IP` injection | 5 |
