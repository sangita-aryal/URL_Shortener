"""
Contract tests for GET /stats/{code} and GET /analytics.

Written test-first (TDD). Two new features:

  BROWNFIELD — GET /stats/{code}
    Returns all-time click count for one short code. Touches app.py,
    analytics.py (new method). Requires reasoning about the existing log
    data model before writing a line.

  AMBIGUOUS — GET /analytics?date=YYYY-MM-DD
    Requirement as stated: "add analytics so we can see how the service
    is performing."  Ambiguous as-is; decisions made explicit here:

      Real-time vs batch?   Batch. Reads the existing append-only log
                            file. No new storage or streaming required.
      Date parameter?       Optional; defaults to today UTC. Omitting is
                            the common case for a "how is today going" dashboard.
      Metrics?              DAU (unique client IPs), total redirects,
                            top-5 codes by click count (enough for a
                            basic dashboard without schema changes).
      Auth?                 None. Endpoint lives on the private internal
                            network behind Nginx, consistent with the rest
                            of the API surface. Auth is out of scope per README.
      Response format?      JSON, same as every other endpoint.
"""
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.analytics import AnalyticsConsumer

# RFC 5737 TEST-NET addresses — safe for tests
_IP1 = "203.0.113.1"
_IP2 = "203.0.113.2"
_IP3 = "203.0.113.3"

_TODAY = datetime.now(UTC).date()
_TS_TODAY = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 12, 0, 0, tzinfo=UTC).isoformat()


def _write(path: str, entries: list[dict]) -> None:
    with open(path, "w") as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)


def _entry(code: str, ip: str, ts: str = _TS_TODAY) -> dict:
    return {"ts": ts, "code": code, "ip": ip}


# ══════════════════════════════════════════════════════════════════════════════
# Part A — AnalyticsConsumer.click_count_for_code  (brownfield unit)
# ══════════════════════════════════════════════════════════════════════════════

class TestClickCountForCode:
    """All-time click count for a single short code."""

    def test_empty_log_returns_zero(self, tmp_path):
        log = str(tmp_path / "t.log")
        open(log, "w").close()
        assert AnalyticsConsumer().click_count_for_code(log, "abc1234") == 0

    def test_single_matching_entry_returns_one(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [_entry("abc1234", _IP1)])
        assert AnalyticsConsumer().click_count_for_code(log, "abc1234") == 1

    def test_multiple_matching_entries_counted(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [
            _entry("abc1234", _IP1),
            _entry("abc1234", _IP2),
            _entry("abc1234", _IP1),
        ])
        assert AnalyticsConsumer().click_count_for_code(log, "abc1234") == 3

    def test_non_matching_entries_excluded(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [
            _entry("abc1234", _IP1),
            _entry("xyz5678", _IP2),
            _entry("xyz5678", _IP3),
        ])
        assert AnalyticsConsumer().click_count_for_code(log, "abc1234") == 1

    def test_unknown_code_returns_zero(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [_entry("abc1234", _IP1)])
        assert AnalyticsConsumer().click_count_for_code(log, "notexist") == 0

    def test_returns_int(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [_entry("abc1234", _IP1)])
        result = AnalyticsConsumer().click_count_for_code(log, "abc1234")
        assert isinstance(result, int)


# ══════════════════════════════════════════════════════════════════════════════
# Part B — AnalyticsConsumer.summary_for_date  (ambiguous unit)
# ══════════════════════════════════════════════════════════════════════════════

_YESTER = date(_TODAY.year, _TODAY.month, _TODAY.day - 1)
_TS_YESTER = datetime(
    _YESTER.year, _YESTER.month, _YESTER.day, 12, 0, 0, tzinfo=UTC
).isoformat()


class TestSummaryForDate:
    """Per-day DAU + total clicks + top-5 codes in a single log pass."""

    def test_empty_log_returns_zero_metrics(self, tmp_path):
        log = str(tmp_path / "t.log")
        open(log, "w").close()
        result = AnalyticsConsumer().summary_for_date(log, _TODAY)
        assert result["dau"] == 0
        assert result["total_clicks"] == 0
        assert result["top_codes"] == []

    def test_date_field_matches_target(self, tmp_path):
        log = str(tmp_path / "t.log")
        open(log, "w").close()
        result = AnalyticsConsumer().summary_for_date(log, _TODAY)
        assert result["date"] == _TODAY.isoformat()

    def test_dau_deduplicates_ips(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [
            _entry("a", _IP1),
            _entry("b", _IP1),   # same IP, different code
            _entry("a", _IP2),
        ])
        assert AnalyticsConsumer().summary_for_date(log, _TODAY)["dau"] == 2

    def test_total_clicks_counts_all_entries(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [_entry("a", _IP1), _entry("b", _IP2), _entry("a", _IP3)])
        assert AnalyticsConsumer().summary_for_date(log, _TODAY)["total_clicks"] == 3

    def test_top_codes_sorted_descending(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [
            _entry("low", _IP1),
            _entry("high", _IP1),
            _entry("high", _IP2),
            _entry("high", _IP3),
        ])
        top = AnalyticsConsumer().summary_for_date(log, _TODAY)["top_codes"]
        assert top[0] == {"code": "high", "clicks": 3}
        assert top[1] == {"code": "low",  "clicks": 1}

    def test_top_codes_capped_at_five(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [_entry(f"code{i}", _IP1) for i in range(10)])
        top = AnalyticsConsumer().summary_for_date(log, _TODAY)["top_codes"]
        assert len(top) <= 5

    def test_cross_date_entries_excluded(self, tmp_path):
        log = str(tmp_path / "t.log")
        _write(log, [
            _entry("today", _IP1, _TS_TODAY),
            _entry("yester", _IP2, _TS_YESTER),
        ])
        result = AnalyticsConsumer().summary_for_date(log, _TODAY)
        assert result["total_clicks"] == 1
        assert result["dau"] == 1
        assert result["top_codes"][0]["code"] == "today"

    def test_single_pass_no_double_read(self, tmp_path):
        """summary_for_date must not materialise all entries into RAM."""
        log = str(tmp_path / "t.log")
        _write(log, [_entry("a", _IP1), _entry("b", _IP1), _entry("a", _IP2)])
        result = AnalyticsConsumer().summary_for_date(log, _TODAY)
        # If double-read, iterator exhaustion would produce wrong numbers.
        assert result["dau"] == 2
        assert result["total_clicks"] == 3


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint test fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    """
    AsyncClient wired to the real FastAPI app with:
      - MongoDB/Redis lifespan replaced by a no-op so no live connections.
      - analytics dependency overridden with a real AnalyticsConsumer.
      - _LOG_PATH patched to a temp file so tests control log contents.
    """
    import app.app as app_module
    from app.app import app, get_analytics_consumer

    log_file = str(tmp_path / "test.log")
    consumer = AnalyticsConsumer()

    monkeypatch.setattr(app_module, "_LOG_PATH", log_file)
    app.dependency_overrides[get_analytics_consumer] = lambda: consumer

    @asynccontextmanager
    async def null_lifespan(_):
        yield

    original = app.router.lifespan_context
    app.router.lifespan_context = null_lifespan

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, log_file

    app.router.lifespan_context = original
    app.dependency_overrides.pop(get_analytics_consumer, None)


# ══════════════════════════════════════════════════════════════════════════════
# Part C — GET /stats/{code}  (brownfield endpoint)
# ══════════════════════════════════════════════════════════════════════════════

class TestStatsEndpoint:

    async def test_returns_200(self, api_client):
        client, log_file = api_client
        _write(log_file, [_entry("abc1234", _IP1)])
        resp = await client.get("/stats/abc1234")
        assert resp.status_code == 200

    async def test_returns_correct_click_count(self, api_client):
        client, log_file = api_client
        _write(log_file, [
            _entry("abc1234", _IP1),
            _entry("abc1234", _IP2),
            _entry("other", _IP1),
        ])
        data = (await client.get("/stats/abc1234")).json()
        assert data["code"] == "abc1234"
        assert data["total_clicks"] == 2

    async def test_unknown_code_returns_zero_not_404(self, api_client):
        """A code with no log entries is not an error — it just has 0 clicks."""
        client, log_file = api_client
        open(log_file, "w").close()
        data = (await client.get("/stats/notexist")).json()
        assert data["total_clicks"] == 0

    async def test_response_shape(self, api_client):
        client, log_file = api_client
        open(log_file, "w").close()
        data = (await client.get("/stats/abc1234")).json()
        assert set(data.keys()) == {"code", "total_clicks"}


# ══════════════════════════════════════════════════════════════════════════════
# Part D — GET /analytics?date=YYYY-MM-DD  (ambiguous endpoint)
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyticsEndpoint:

    async def test_returns_200(self, api_client):
        client, log_file = api_client
        open(log_file, "w").close()
        resp = await client.get("/analytics")
        assert resp.status_code == 200

    async def test_response_shape(self, api_client):
        client, log_file = api_client
        open(log_file, "w").close()
        data = (await client.get("/analytics")).json()
        assert set(data.keys()) == {"date", "dau", "total_clicks", "top_codes"}

    async def test_default_date_is_today_utc(self, api_client):
        client, _ = api_client
        data = (await client.get("/analytics")).json()
        assert data["date"] == _TODAY.isoformat()

    async def test_explicit_date_parameter(self, api_client):
        client, log_file = api_client
        yester_str = _YESTER.isoformat()
        _write(log_file, [_entry("code", _IP1, _TS_YESTER)])
        data = (await client.get(f"/analytics?date={yester_str}")).json()
        assert data["date"] == yester_str
        assert data["total_clicks"] == 1

    async def test_invalid_date_returns_422(self, api_client):
        client, _ = api_client
        resp = await client.get("/analytics?date=not-a-date")
        assert resp.status_code == 422

    async def test_dau_and_total_clicks_correct(self, api_client):
        client, log_file = api_client
        _write(log_file, [
            _entry("a", _IP1),
            _entry("b", _IP1),   # same IP → still 1 DAU
            _entry("a", _IP2),
        ])
        data = (await client.get(f"/analytics?date={_TODAY.isoformat()}")).json()
        assert data["dau"] == 2
        assert data["total_clicks"] == 3

    async def test_top_codes_sorted_descending(self, api_client):
        client, log_file = api_client
        _write(log_file, [
            _entry("popular", _IP1),
            _entry("popular", _IP2),
            _entry("rare", _IP3),
        ])
        top = (await client.get(f"/analytics?date={_TODAY.isoformat()}")).json()["top_codes"]
        assert top[0]["code"] == "popular"
        assert top[0]["clicks"] == 2

    async def test_date_only_returns_that_days_data(self, api_client):
        client, log_file = api_client
        _write(log_file, [
            _entry("today", _IP1, _TS_TODAY),
            _entry("yester", _IP2, _TS_YESTER),
        ])
        data = (await client.get(f"/analytics?date={_TODAY.isoformat()}")).json()
        codes = [c["code"] for c in data["top_codes"]]
        assert "today" in codes
        assert "yester" not in codes
