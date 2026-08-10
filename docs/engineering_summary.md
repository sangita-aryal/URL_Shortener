# Engineering Summary — URL Shortener

## What Was Built

A stateless, distributed URL shortener designed for production-scale throughput: 100M URL creations per day, 1B redirects per day, sub-millisecond redirection latency. Built over 2–3 days using AI-assisted engineering with Claude Sonnet 4.6 as the execution accelerator.

The system is a four-layer stack: Nginx API gateway → stateless FastAPI workers → Redis read-through cache → MongoDB replica set. All business logic is in Python. All infrastructure is containerised and orchestrated via Docker Compose.

---

## Artifacts

| Artifact | Location | Purpose |
|---|---|---|
| Core application | `app/` | FastAPI app, ID generation, SSRF validation, persistence, telemetry, analytics |
| Test suite | `tests/` | 204 contract tests; written before implementation in every case |
| Docker stack | `docker-compose.yaml`, `Dockerfile`, `nginx/Dockerfile`, `nginx/nginx.conf` | Full runnable stack: Nginx (TLS), FastAPI, Redis, MongoDB replica set |
| React frontend | `frontend/` | Single-page app for URL shortening; Vite + Tailwind |
| Analytics report script | `scripts/report.py` | Offline CLI report: DAU, total clicks, top-N codes for a given date |
| Architecture documentation | `README.md` | Capacity planning, ADRs, setup instructions, limitations |
| AI traceability log | `docs/ai_execution_log.md` | Verbatim prompts, accepted/modified/rejected proposals, bugs introduced by AI |
| Quality gates report | `docs/quality_gates.md` | Ruff, bandit, pytest coverage results with gap analysis |
| Scenario: Greenfield | `docs/scenarios/greenfield.md` | Build from scratch — decomposition, execution, validation |
| Scenario: Brownfield | `docs/scenarios/brownfield.md` | `GET /stats/{code}` — impact analysis, existing system navigation |
| Scenario: Ambiguous | `docs/scenarios/ambiguous.md` | `GET /analytics` — requirement normalisation, decisions, execution |

---

## Plan and Rationale

The architecture was derived from capacity numbers, not from defaults. The key chain:

**1B redirects/day → Redis cache is mandatory.** At 11,600 read QPS, MongoDB alone cannot sustain sub-millisecond latency. Every redirect must be a cache hit; MongoDB is the fallback.

**100M creations/day → ID generation cannot use read-before-write.** Any strategy that checks for collisions before writing (hashing, truncated UUIDs) doubles write-path latency in the average case and introduces unbounded retries on collision. The Feistel cipher is bijective — it mathematically cannot produce collisions — so the write path is a single `insert_one` with no preliminary read.

**365B records over 10 years → MongoDB, not relational.** No single relational node holds 36.5TB of hot indexed data. MongoDB's native horizontal sharding distributes the keyspace across nodes without cross-shard joins, which our pure key-value access pattern never requires.

**Analytics must not compete with the cache.** Redis `INCR` counters for click analytics would compete with the redirect cache for RAM. Under memory pressure, Redis LRU eviction removes hot redirect entries — exactly the entries the cache exists to serve. The telemetry pipeline writes to an append-only log file instead; analytics are computed offline.

---

## Risks and Trade-offs

### Accepted trade-offs

**HTTP 302 over 301.** Every redirect hits the server. Repeat clicks produce real server load rather than resolving client-side. The trade-off is deliberate: 301 caches in the browser, making repeat visits invisible to the telemetry pipeline. Analytics accuracy requires 302.

**10,000-ID lease over 1,000,000.** A larger lease means fewer MongoDB round-trips but more IDs permanently lost when a worker crashes mid-lease. At 1M lease size, a single OOM kill leaks up to 999,999 IDs. At 10K, worst-case leakage is 9,999 — a 99% reduction — while 10,000 `find_one_and_update` calls per day is negligible at any MongoDB capacity.

**Single-node replica set.** A true multi-node replica set requires multiple physical hosts or a complex single-host Compose configuration with replicated volumes. The single-node `rs0` configuration supports `w="majority"` write concerns (the primary durability requirement) without that operational complexity. It is not truly HA — a crashed `mongo` container has no secondary to elect — but `restart: unless-stopped` limits exposure.

**Self-signed TLS, not CA-signed.** The Compose stack terminates TLS at Nginx with a self-signed RSA-2048 certificate generated at build time. Port 80 redirects permanently to HTTPS; TLSv1.0/1.1 are excluded. Plaintext on the wire is eliminated. What remains before internal deployment: replace the self-signed cert with a CA-signed certificate, configure OCSP stapling, and obtain explicit security sign-off — the cert itself carries no identity assurance and will trigger browser warnings. See README §8.10.

### Residual risks

**IP-based DAU is approximate.** Users behind NAT, corporate proxies, or VPNs are undercounted or overcounted. The system records `X-Real-IP` from the Nginx header, which is the IP of the immediate TCP peer. Exact unique-user counting requires browser fingerprinting or authentication — both out of scope.

**DNS rebinding window.** SSRF validation resolves the target hostname at creation time. There is a millisecond window between DNS resolution and the MongoDB write. A sophisticated attacker controlling DNS TTL could serve a public IP during validation and rotate to a private IP afterwards. The server is not the vector in this case — the user's browser would be — but the residual risk exists and is documented.

**Analytics endpoint reads log file on HTTP path.** `GET /analytics` and `GET /stats/{code}` call `asyncio.to_thread` to avoid blocking the event loop, but at high log volumes (10 GB/day) a single request could take several seconds. For production scale, precomputed daily aggregations in MongoDB would replace the log-file read.

---

## Assumptions

- Short codes are permanent. No expiration, no deletion. A URL created today remains resolvable indefinitely.
- The system operates in a single region. Cross-region replication and geo-distributed MongoDB sharding are out of scope.
- All clients are trusted within the Nginx perimeter. The API has no authentication layer beyond what Nginx provides (rate limiting, IP-based).
- Log volume is manageable at prototype scale. The append-only telemetry log is not rotated. At production volume (10 GB/day) a log rotation strategy (logrotate, daily rollover) would be required before `analytics_consumer` reads become too slow.
- The Feistel round keys in `docker-compose.yaml` are defaults for local development. Production deployments must rotate these keys via environment variables to prevent short-code enumeration.

---

## Limitations

See README §8 for the full list. The most operationally significant:

- TLS in place (self-signed cert); CA sign-off and cert rotation required before internal deployment (§8.10)
- Single-node replica set is not HA (§8.1)
- Redis has no persistence — cache is lost on container restart (§8.2)
- Nginx upstream does not auto-discover new FastAPI replicas (§8.3)
- Analytics endpoints read log file synchronously under `asyncio.to_thread` — acceptable at prototype scale, not at 10 GB/day (§8.8)

---

## What I Would Do Next

In priority order, if this were moving toward production:

1. Replace the self-signed TLS certificate with a CA-signed cert (internal PKI, Let's Encrypt, or ACM) and configure OCSP stapling — required before any internal deployment per §8.10
2. Replace the single-node replica set with a three-member cluster across separate hosts
3. Add Redis AOF persistence so the cache survives container restarts
4. Precompute daily analytics into MongoDB to eliminate log-file reads on the HTTP path
5. Add URL expiration support (TTL index on `expires_at` in MongoDB, matching Redis TTL)
6. Wire `frontend/dist/` into the Nginx image so the SPA and API are served from the same origin without a separate manual build step
