# Scenario 2 — Brownfield: Add Per-Code Click Stats

## Requirement

Add a way to look up how many times a specific short code has been clicked.

The system was already running. The telemetry pipeline was writing click events to an append-only log. The analytics consumer could already compute aggregate counts. The question was how to expose per-code data through the API without breaking anything that existed.

---

## Codebase Impact Analysis

Before touching any file, I mapped every module in the existing system against the requirement:

| Module | Impact | What changes |
|---|---|---|
| `app/analytics.py` | **Modified** | Add `click_count_for_code(log_path, code) → int` — filters `stream_entries()` to a single code and counts matches |
| `app/app.py` | **Modified** | Add `GET /stats/{code}` route; add `StatsResponse` Pydantic model; wire `AnalyticsConsumer` into lifespan and expose via `Depends()` |
| `app/telemetry.py` | **None** | Log format unchanged — `{"ts", "code", "ip"}` already captures the code field needed for filtering |
| `app/url_repository.py` | **None** | Read path unchanged — stats are computed from the telemetry log, not MongoDB |
| `app/id_generator.py` | **None** | ID generation unaffected |
| `app/ssrf_validator.py` | **None** | Validation unaffected |
| `tests/test_telemetry.py` | **None** | Existing telemetry contracts unchanged |
| `tests/test_url_repository.py` | **None** | Existing persistence contracts unchanged |
| `docker-compose.yaml` | **None** | No new services or volumes |
| `nginx/nginx.conf` | **None** | New route served through the existing upstream |

**Pre-existing constraint respected:** `AnalyticsConsumer` already used a generator-based streaming approach. The new method had to follow the same pattern — no full file load, filter line-by-line.

---

## Risk Identified

`click_count_for_code` reads the log file on the HTTP request path. In the existing codebase, file I/O on the request path is handled via `asyncio.to_thread()` (see `TelemetryLogger._append`). The same pattern is required here to avoid blocking the event loop at read QPS.

The route handler wraps the call accordingly:

```python
total = await asyncio.to_thread(analytics.click_count_for_code, _LOG_PATH, code)
```

---

## Decomposition

1. Write contract tests for `AnalyticsConsumer.click_count_for_code` — before touching `analytics.py`
2. Write HTTP contract tests for `GET /stats/{code}` — before touching `app.py`
3. Get test suite approved
4. Implement `click_count_for_code` in `analytics.py`
5. Add `StatsResponse` model and route to `app.py`; inject `AnalyticsConsumer` via lifespan + `Depends()`
6. Run full test suite — confirm no regressions in the 146 pre-existing tests

---

## Execution

Tests written first (`tests/test_stats_analytics.py` Part A and Part C). Implementation followed after the contracts were locked.

One decision during implementation: `GET /stats/{code}` returns `{"code": "abc1234", "total_clicks": 0}` for an unknown code rather than a 404. A code with no clicks is not an error — it may have been created but not yet clicked. Returning 0 is the correct semantic; 404 would conflate "not found in log" with "does not exist."

AI implemented both the method and the route cleanly on the first attempt with no corrections required.

---

## Validation

| Check | Result |
|---|---|
| Pre-existing 146 tests | All pass — no regressions |
| New `TestClickCountForCode` (6 unit tests) | All pass |
| New `TestStatsEndpoint` (4 HTTP tests) | All pass |
| `ruff check app/ tests/` | PASS — 0 issues |
| `bandit -r app/` | PASS — unchanged from baseline |

The endpoint tests use `httpx.AsyncClient` with an `ASGITransport` fixture and a null lifespan, so they exercise the actual HTTP layer without requiring a live MongoDB or Redis connection.
