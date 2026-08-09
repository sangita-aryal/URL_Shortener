# Scenario 3 — Ambiguous: "Add Analytics"

## Requirement as Stated

> "Add an analytics endpoint so we can see how the service is performing."

That's it. No metrics specified. No time range. No format. No authentication requirement. No indication of whether this should be real-time or batch. Every implementation decision was mine to make.

---

## Ambiguity Identified

I wrote out every open question before writing a test:

| Question | Why it's ambiguous | Decision |
|---|---|---|
| Real-time or batch? | Real-time implies a streaming consumer or live DB aggregation. Batch implies reading the existing log file. | **Batch** — the telemetry log already exists; adding a streaming consumer would introduce Kafka or Redis Streams and significantly expand the infrastructure footprint without a stated requirement for real-time data |
| What metrics? | "How the service is performing" could mean redirect latency, error rate, DAU, revenue, geographic distribution, or click counts | **DAU + total clicks + top-5 codes by click volume** — these are directly computable from the existing log format without schema changes; they answer the most natural interpretation of "performing" for a URL shortener |
| What time range? | All-time, rolling 24h, calendar day, configurable | **Calendar day via optional `?date=YYYY-MM-DD` parameter, defaulting to today UTC** — consistent with the existing `entries_for_date` filter already in `AnalyticsConsumer`; UTC prevents cross-timezone ambiguity |
| Authentication? | No auth specified | **No auth** — every other endpoint in the service is unauthenticated; the API surface lives on the internal network behind Nginx; adding auth would require a key management system not in scope |
| Response format? | JSON, HTML dashboard, CSV | **JSON** — consistent with every other endpoint; a frontend can render it |
| Top-N codes: how many? | Unspecified | **5** — enough to identify the most-clicked content without an unbounded response; doesn't require pagination |

---

## Normalised Requirement

`GET /analytics?date=YYYY-MM-DD` returns:

```json
{
  "date": "2026-08-09",
  "dau": 3142,
  "total_clicks": 18904,
  "top_codes": [
    {"code": "aB3cD4e", "clicks": 412},
    {"code": "xY9zW1q", "clicks": 388}
  ]
}
```

Date parameter is optional; omitting it returns today UTC. Invalid date strings return 422 (FastAPI query parameter validation, no custom code required).

---

## Codebase Impact Analysis

| Module | Impact | What changes |
|---|---|---|
| `app/analytics.py` | **Modified** | Add `summary_for_date(log_path, target_date) → dict` — single-pass over `entries_for_date`; computes DAU, total clicks, and top-5 in one iteration |
| `app/app.py` | **Modified** | Add `GET /analytics` route with optional `date` query param; add `AnalyticsSummaryResponse` and `TopCode` Pydantic models |
| Everything else | **None** | Log format, telemetry pipeline, cache, ID generation — all unchanged |

---

## Key Implementation Decision: Single Pass

`summary_for_date` had to compute three different aggregations (DAU, total clicks, top codes) over the same filtered log stream. The naive approach — three separate passes over `entries_for_date` — would read the log file three times and fail if the generator was exhausted after the first pass.

The implementation collects all three in a single iteration:

```python
seen_ips: set[str] = set()
click_counts: Counter[str] = Counter()
for entry in self.entries_for_date(log_path, target_date):
    if ip := entry.get("ip"):
        seen_ips.add(ip)
    if code := entry.get("code"):
        click_counts[code] += 1
```

This was verified by `test_single_pass_no_double_read` — a test that catches double-read by checking that counts are correct when the underlying iterator can only be consumed once.

---

## Decomposition

1. Write contract tests for `AnalyticsConsumer.summary_for_date` — covering empty log, date filtering, DAU deduplication, top-5 capping, single-pass constraint
2. Write HTTP contract tests for `GET /analytics` — covering 200 response, default date, explicit date, invalid date (422), shape, data correctness
3. Get test suite approved
4. Implement `summary_for_date` in `analytics.py`
5. Add response models and route to `app.py`
6. Run full test suite — confirm no regressions

---

## Execution

Tests written first (Part B and Part D in `tests/test_stats_analytics.py`). The ambiguity decisions are documented in the test module docstring so they're visible to anyone reading the tests, not just whoever reads this file.

The `GET /analytics` route docstring in `app.py` also restates the decisions inline:

```python
"""
Batch analytics for a calendar day.

Ambiguous requirement interpreted as:
  - Batch (reads log file); no new streaming infra needed.
  - Defaults to today UTC when date param is omitted.
  - Metrics: DAU (unique IPs), total clicks, top-5 codes by clicks.
  - No auth: internal network only, consistent with the rest of the API.
"""
```

AI implemented both cleanly on the first attempt. No corrections required.

---

## Validation

| Check | Result |
|---|---|
| Pre-existing 146 tests | All pass — no regressions |
| New `TestSummaryForDate` (8 unit tests) | All pass |
| New `TestAnalyticsEndpoint` (8 HTTP tests) | All pass |
| `ruff check app/ tests/` | PASS — 0 issues |
| `bandit -r app/` | PASS — unchanged |
| Coverage | `app/analytics.py` 96%; `app/app.py` 66% (up from 0%) |

---

## What I Would Do Differently at Production Scale

The current implementation reads the full log file on every request. At 10 GB/day of log volume, a single `GET /analytics` call could take seconds. For production use, I would precompute daily aggregations into MongoDB (a background job running at midnight UTC) and serve the endpoint from a cached document. The log-file approach is correct for a prototype where log volume is small and real-time precomputation infrastructure is out of scope.

This is documented in README §8.8 as a known limitation.
