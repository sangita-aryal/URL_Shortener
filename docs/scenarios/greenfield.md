# Scenario 1 — Greenfield: URL Shortener from Scratch

## Requirement

Build a URL shortener service with core APIs, analytics, and reliability features.

That was the starting point. No existing system, no legacy schema, no prior API to conform to. The brief was intentionally open-ended, which meant the first real task was turning it into something I could actually build against.

---

## Ambiguity Identified

Before writing a line of code I had to make decisions the requirement didn't make for me:

| Open question | Decision made | Reason |
|---|---|---|
| How short is "short"? | 7-character Base-62 | Derived from capacity: 62^7 = 3.5T unique codes covers a 10-year horizon at 100M creations/day |
| What does "analytics" mean? | DAU (unique IPs) + click count per short code | Simplest useful metrics; no user accounts needed; matches what the telemetry log can produce offline |
| What does "reliability" mean? | Durability (majority write concern) + availability (stateless workers, health-checked containers) | Durability prevents data loss on primary failover; stateless workers allow horizontal scaling without coordination |
| What ID generation strategy? | Feistel cipher over a MongoDB sequence lease | Sequential IDs are enumerable; hashing collides at scale; UUIDs are too long — ruled all three out before writing tests |
| 301 or 302 redirects? | 302 | 301 caches in the browser; every repeat click becomes invisible to telemetry. Analytics requirement overrides the latency benefit of caching |

---

## Decomposition

Broke the requirement into five independent layers, ordered by dependency:

1. **ID generation** — `FeistelCipher` + `SequenceLeaseManager` — no external dependencies; pure logic; testable in isolation
2. **SSRF validation** — `validate_url` — depends on `aiodns`; must run before any ID is consumed
3. **Persistence** — `URLRepository` — depends on Redis and MongoDB; read-through cache pattern
4. **Telemetry** — `TelemetryLogger` + `AnalyticsConsumer` — append-only log; fully decoupled from the cache layer
5. **Orchestration** — FastAPI app, Nginx, Docker Compose — wires everything together; built last

Each layer had its tests written and approved before implementation. The sequence was: write contracts → get approval → write implementation → confirm all tests pass → move to the next layer.

---

## Execution

Built entirely with AI assistance (Claude Sonnet 4.6) under a strict TDD discipline. Full prompt-by-prompt record is in [`docs/ai_execution_log.md`](../ai_execution_log.md).

The short version of what happened:

- **ID generation**: AI's first attempt produced a Snowflake ID generator — timestamp-based, enumerable, clock-sensitive. I rejected it, attached the architecture spec, and required a Feistel cipher + segment lease approach. Three more turns before the test suite was approved.
- **SSRF validator**: AI's first attempt used `socket.getaddrinfo` — a blocking syscall that stalls the ASGI event loop. Named the problem explicitly, required `aiodns` by name.
- **Persistence**: Clean first attempt. Read-through cache, no write-through, no null-cache poisoning.
- **Telemetry**: Clean first attempt. Fire-and-forget via `asyncio.create_task`, file I/O via `asyncio.to_thread`.
- **Orchestration**: Two bugs caught after running the stack — `j=True` crashed Motor on startup, Vite proxy pointed at the wrong port. Both diagnosed by me, fixed in PR #8.

---

## Validation

| Gate | Result |
|---|---|
| `ruff check app/ tests/` | PASS — 0 issues |
| `bandit -r app/` | PASS — 1 Low (intentional, documented) |
| `pytest tests/` | PASS — 172/172 |
| Coverage | 87% overall; 95–100% on all business logic modules |

Full gate output in [`docs/quality_gates.md`](../quality_gates.md).

The single bandit finding (`B110` — bare `except: pass` in `telemetry.py`) is intentional. Swallowing telemetry write errors is the documented contract: a failed log write must never propagate to the route handler and cause a redirect failure.
