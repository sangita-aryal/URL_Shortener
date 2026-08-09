"""
Contract tests for the Telemetry and Analytics Pipeline.

Per architect.md §6:

  Buffering click analytics directly in Redis memory is an anti-pattern
  that exhausts RAM and forces LRU evictions of hot redirection mappings.

  Instead, FastAPI nodes track Daily Active Users (DAU) and total clicks
  by writing telemetry asynchronously to structured, append-only local
  log files.  An offline Python script acts as the analytics consumer:
  it streams logs line-by-line via a generator, uses a deduplicated Python
  Set for DAU (O(U) space, bounded by unique users), and counts total
  clicks per short code.

  The write path (TelemetryLogger) must be fire-and-forget: the HTTP 302
  response is returned before the log entry hits disk.  A failed write
  must never propagate an exception to the route handler.

  The read path (AnalyticsConsumer) operates entirely offline and never
  touches Redis or MongoDB.

API contracts under test (not yet implemented):

  class TelemetryLogger:          # app/telemetry.py
      def __init__(self, log_path: str) -> None: ...
      async def record_redirect(self, short_code: str, client_ip: str) -> None: ...

  class AnalyticsConsumer:        # app/analytics.py
      def stream_entries(self, log_path: str) -> Iterator[dict]: ...
      def entries_for_date(self, log_path: str, target_date: date) -> Iterator[dict]: ...
      def compute_dau(self, entries: Iterable[dict]) -> int: ...
      def compute_total_clicks(self, entries: Iterable[dict]) -> dict[str, int]: ...
"""
import asyncio
import inspect
import json
from datetime import UTC, date, datetime

from app.telemetry import TelemetryLogger

# RFC 5737 TEST-NET addresses — safe for documentation and tests
_CODE  = "aB3cD4e"
_CODE2 = "xY9zW1q"
_IP    = "203.0.113.42"   # TEST-NET-3
_IP2   = "198.51.100.7"   # TEST-NET-2


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_log_lines(path: str, lines: list[str]) -> None:
    """Write raw log lines to a file for consumer-side tests."""
    with open(path, "w") as f:
        f.writelines(line + "\n" for line in lines)


def _make_entry(code: str, ip: str, dt: datetime | None = None) -> str:
    """Build a valid JSON log line matching TelemetryLogger's output format."""
    ts = (dt or datetime.now(UTC)).isoformat()
    return json.dumps({"ts": ts, "code": code, "ip": ip})


# ══════════════════════════════════════════════════════════════════════════════
# TelemetryLogger — log entry format
# ══════════════════════════════════════════════════════════════════════════════

class TestLogEntryFormat:
    """
    record_redirect() must append exactly one valid JSON line per call.
    The line must contain the three mandatory fields: ts, code, ip.
    Repeated calls must append without overwriting earlier entries.
    """

    async def test_record_redirect_writes_one_line(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        assert len(lines) == 1

    async def test_log_line_is_valid_json(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            line = f.read().strip()
        entry = json.loads(line)   # raises json.JSONDecodeError on malformed output
        assert isinstance(entry, dict)

    async def test_log_entry_contains_short_code(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            entry = json.loads(f.read().strip())
        assert entry["code"] == _CODE

    async def test_log_entry_contains_client_ip(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            entry = json.loads(f.read().strip())
        assert entry["ip"] == _IP

    async def test_log_entry_contains_timestamp_field(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            entry = json.loads(f.read().strip())
        assert "ts" in entry

    async def test_two_calls_append_two_lines(self, telemetry_logger, log_path):
        """Second call must append, not overwrite, the first entry."""
        await telemetry_logger.record_redirect(_CODE,  _IP)
        await telemetry_logger.record_redirect(_CODE2, _IP2)
        with open(log_path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        assert len(lines) == 2


# ══════════════════════════════════════════════════════════════════════════════
# TelemetryLogger — timestamp contract
# ══════════════════════════════════════════════════════════════════════════════

class TestLogEntryTimestamp:
    """
    The `ts` field must be a valid ISO-8601 string carrying UTC timezone
    information.  A timezone-naive timestamp would produce incorrect DAU
    aggregation when logs from servers in different time zones are merged.
    """

    async def test_timestamp_is_parseable_as_iso_datetime(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            entry = json.loads(f.read().strip())
        dt = datetime.fromisoformat(entry["ts"])   # raises on invalid ISO format
        assert isinstance(dt, datetime)

    async def test_timestamp_carries_utc_timezone_info(self, telemetry_logger, log_path):
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            entry = json.loads(f.read().strip())
        dt = datetime.fromisoformat(entry["ts"])
        assert dt.tzinfo is not None, (
            "timestamp must carry UTC timezone info for cross-server log merging"
        )

    async def test_timestamp_reflects_current_utc_date(self, telemetry_logger, log_path):
        today = datetime.now(UTC).date()
        await telemetry_logger.record_redirect(_CODE, _IP)
        with open(log_path) as f:
            entry = json.loads(f.read().strip())
        dt = datetime.fromisoformat(entry["ts"])
        assert dt.date() == today


# ══════════════════════════════════════════════════════════════════════════════
# TelemetryLogger — async and error-isolation contract
# ══════════════════════════════════════════════════════════════════════════════

class TestAsyncAndErrorIsolation:
    """
    record_redirect() must be a coroutine so the route handler can schedule
    it as asyncio.create_task() and return the 302 immediately.

    A failed write (e.g., disk full, missing directory) must never propagate
    an exception.  Telemetry is best-effort; availability of the redirect
    service takes precedence.
    """

    def test_record_redirect_is_a_coroutine_function(self):
        assert asyncio.iscoroutinefunction(TelemetryLogger.record_redirect)

    def test_calling_record_redirect_returns_an_awaitable(self, telemetry_logger):
        """
        Calling (not awaiting) record_redirect() must return a coroutine
        object that can be passed to asyncio.create_task().
        """
        coro = telemetry_logger.record_redirect(_CODE, _IP)
        assert inspect.isawaitable(coro)
        coro.close()   # prevent "coroutine never awaited" ResourceWarning

    async def test_write_failure_does_not_propagate_exception(self, tmp_path):
        """
        A log_path whose parent directory does not exist causes an OSError
        on open().  The exception must be caught internally — not re-raised.
        """
        bad_logger = TelemetryLogger(
            log_path=str(tmp_path / "nonexistent_dir" / "analytics.log")
        )
        await bad_logger.record_redirect(_CODE, _IP)   # must not raise

    async def test_healthy_logger_succeeds_after_failed_logger(self, tmp_path, log_path):
        """
        A failure in one logger instance must not affect another instance
        pointing at a valid path.  State must not bleed between instances.
        """
        bad_logger = TelemetryLogger(
            log_path=str(tmp_path / "nonexistent_dir" / "analytics.log")
        )
        await bad_logger.record_redirect(_CODE, _IP)   # silent failure

        good_logger = TelemetryLogger(log_path=log_path)
        await good_logger.record_redirect(_CODE, _IP)  # must succeed

        with open(log_path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        assert len(lines) == 1


# ══════════════════════════════════════════════════════════════════════════════
# AnalyticsConsumer — stream_entries generator
# ══════════════════════════════════════════════════════════════════════════════

class TestStreamEntries:
    """
    stream_entries() must be a generator that yields one parsed dict per
    valid log line.  It must never load the entire file into memory —
    this is critical for log files that accumulate gigabytes of entries.

    Malformed lines (bad JSON, blank lines) must be skipped silently so
    a single corrupted write does not halt the entire analytics run.
    """

    def test_stream_entries_returns_a_generator(self, analytics_consumer, log_path):
        _write_log_lines(log_path, [_make_entry(_CODE, _IP)])
        result = analytics_consumer.stream_entries(log_path)
        assert inspect.isgenerator(result)

    def test_stream_entries_yields_dicts(self, analytics_consumer, log_path):
        _write_log_lines(log_path, [_make_entry(_CODE, _IP)])
        entries = list(analytics_consumer.stream_entries(log_path))
        assert len(entries) == 1
        assert isinstance(entries[0], dict)

    def test_stream_entries_empty_file_yields_nothing(self, analytics_consumer, log_path):
        _write_log_lines(log_path, [])
        assert list(analytics_consumer.stream_entries(log_path)) == []

    def test_stream_entries_skips_invalid_json_lines(self, analytics_consumer, log_path):
        _write_log_lines(log_path, [
            _make_entry(_CODE, _IP),
            "not valid json {{{",
            _make_entry(_CODE2, _IP2),
        ])
        entries = list(analytics_consumer.stream_entries(log_path))
        assert len(entries) == 2

    def test_stream_entries_skips_blank_lines(self, analytics_consumer, log_path):
        """Blank lines from log rotation artefacts must not crash the consumer."""
        _write_log_lines(log_path, [
            _make_entry(_CODE, _IP),
            "",
            "   ",
            _make_entry(_CODE2, _IP2),
        ])
        entries = list(analytics_consumer.stream_entries(log_path))
        assert len(entries) == 2

    def test_stream_entries_preserves_all_fields(self, analytics_consumer, log_path):
        """Each yielded dict must contain the ts, code, and ip fields."""
        _write_log_lines(log_path, [_make_entry(_CODE, _IP)])
        entry = next(analytics_consumer.stream_entries(log_path))
        assert "ts"   in entry
        assert "code" in entry
        assert "ip"   in entry


# ══════════════════════════════════════════════════════════════════════════════
# AnalyticsConsumer — DAU computation
# ══════════════════════════════════════════════════════════════════════════════

class TestDAUComputation:
    """
    compute_dau() counts distinct client IPs using a Python Set, giving
    O(U) space complexity bounded by unique users — not total click volume.

    The same IP appearing multiple times (e.g., a user clicking 100 links)
    must count as exactly one daily active user.
    """

    def test_single_entry_gives_dau_of_one(self, analytics_consumer):
        entries = [{"ts": "2026-08-09T10:00:00+00:00", "code": _CODE, "ip": _IP}]
        assert analytics_consumer.compute_dau(entries) == 1

    def test_two_entries_same_ip_gives_dau_of_one(self, analytics_consumer):
        entries = [
            {"ts": "2026-08-09T10:00:00+00:00", "code": _CODE,  "ip": _IP},
            {"ts": "2026-08-09T11:00:00+00:00", "code": _CODE2, "ip": _IP},
        ]
        assert analytics_consumer.compute_dau(entries) == 1

    def test_two_entries_different_ips_gives_dau_of_two(self, analytics_consumer):
        entries = [
            {"ts": "2026-08-09T10:00:00+00:00", "code": _CODE,  "ip": _IP},
            {"ts": "2026-08-09T11:00:00+00:00", "code": _CODE2, "ip": _IP2},
        ]
        assert analytics_consumer.compute_dau(entries) == 2

    def test_empty_entries_gives_dau_of_zero(self, analytics_consumer):
        assert analytics_consumer.compute_dau([]) == 0

    def test_large_input_deduplicates_correctly(self, analytics_consumer):
        """
        500 unique IPs each appearing twice must give DAU == 500.
        This validates set-based deduplication at a meaningful scale.
        """
        entries = [
            {
                "ts": "2026-08-09T10:00:00+00:00",
                "code": _CODE,
                "ip": f"203.0.113.{i % 500}",
            }
            for i in range(1_000)
        ]
        assert analytics_consumer.compute_dau(entries) == 500

    def test_compute_dau_returns_int(self, analytics_consumer):
        entries = [{"ts": "2026-08-09T10:00:00+00:00", "code": _CODE, "ip": _IP}]
        assert isinstance(analytics_consumer.compute_dau(entries), int)


# ══════════════════════════════════════════════════════════════════════════════
# AnalyticsConsumer — click computation
# ══════════════════════════════════════════════════════════════════════════════

class TestClickComputation:
    """
    compute_total_clicks() counts how many times each short code appears
    in the provided entries, returning a dict mapping short_code → count.
    Different short codes are counted independently.
    """

    def test_single_click_returns_count_of_one(self, analytics_consumer):
        entries = [{"ts": "2026-08-09T10:00:00+00:00", "code": _CODE, "ip": _IP}]
        result = analytics_consumer.compute_total_clicks(entries)
        assert result[_CODE] == 1

    def test_two_clicks_same_code_returns_count_of_two(self, analytics_consumer):
        entries = [
            {"ts": "2026-08-09T10:00:00+00:00", "code": _CODE, "ip": _IP},
            {"ts": "2026-08-09T11:00:00+00:00", "code": _CODE, "ip": _IP2},
        ]
        assert analytics_consumer.compute_total_clicks(entries)[_CODE] == 2

    def test_different_codes_counted_independently(self, analytics_consumer):
        entries = [
            {"ts": "2026-08-09T10:00:00+00:00", "code": _CODE,  "ip": _IP},
            {"ts": "2026-08-09T11:00:00+00:00", "code": _CODE2, "ip": _IP},
        ]
        result = analytics_consumer.compute_total_clicks(entries)
        assert result[_CODE]  == 1
        assert result[_CODE2] == 1

    def test_empty_entries_returns_empty_dict(self, analytics_consumer):
        assert analytics_consumer.compute_total_clicks([]) == {}

    def test_click_count_values_are_ints(self, analytics_consumer):
        entries = [{"ts": "2026-08-09T10:00:00+00:00", "code": _CODE, "ip": _IP}]
        result = analytics_consumer.compute_total_clicks(entries)
        assert isinstance(result[_CODE], int)


# ══════════════════════════════════════════════════════════════════════════════
# AnalyticsConsumer — date-based entry filtering (DAU isolation)
# ══════════════════════════════════════════════════════════════════════════════

class TestDateIsolation:
    """
    entries_for_date() must filter stream_entries to only those whose `ts`
    timestamp falls on the given UTC date.

    This is the boundary between today's DAU and yesterday's — incorrectly
    including yesterday's traffic would overcount DAU and corrupt any
    dashboard or SLA report built on top of it.
    """

    def test_entries_for_today_excludes_yesterday(self, analytics_consumer, log_path):
        today = date(2026, 8, 9)
        _write_log_lines(log_path, [
            _make_entry(_CODE,  _IP,  datetime(2026, 8, 9, 10, 0, tzinfo=UTC)),
            _make_entry(_CODE2, _IP2, datetime(2026, 8, 8, 10, 0, tzinfo=UTC)),
        ])
        entries = list(analytics_consumer.entries_for_date(log_path, today))
        assert len(entries) == 1
        assert entries[0]["code"] == _CODE

    def test_entries_for_yesterday_excludes_today(self, analytics_consumer, log_path):
        yesterday = date(2026, 8, 8)
        _write_log_lines(log_path, [
            _make_entry(_CODE,  _IP,  datetime(2026, 8, 9, 10, 0, tzinfo=UTC)),
            _make_entry(_CODE2, _IP2, datetime(2026, 8, 8, 10, 0, tzinfo=UTC)),
        ])
        entries = list(analytics_consumer.entries_for_date(log_path, yesterday))
        assert len(entries) == 1
        assert entries[0]["code"] == _CODE2

    def test_entries_for_date_returns_a_generator(self, analytics_consumer, log_path):
        """The filter itself must be lazy — no bulk reads into memory."""
        _write_log_lines(log_path, [
            _make_entry(_CODE, _IP, datetime(2026, 8, 9, 10, 0, tzinfo=UTC)),
        ])
        result = analytics_consumer.entries_for_date(log_path, date(2026, 8, 9))
        assert inspect.isgenerator(result)

    def test_dau_via_date_filter_excludes_prior_day_ips(self, analytics_consumer, log_path):
        """
        Full pipeline: entries_for_date → compute_dau.
        Yesterday's unique IP must not inflate today's count.
        Today has two entries for the same IP → DAU must be 1.
        """
        today = date(2026, 8, 9)
        _write_log_lines(log_path, [
            _make_entry(_CODE, _IP,  datetime(2026, 8, 9, 10, 0, tzinfo=UTC)),
            _make_entry(_CODE, _IP,  datetime(2026, 8, 9, 14, 0, tzinfo=UTC)),
            _make_entry(_CODE, _IP2, datetime(2026, 8, 8, 10, 0, tzinfo=UTC)),
        ])
        filtered = analytics_consumer.entries_for_date(log_path, today)
        assert analytics_consumer.compute_dau(filtered) == 1
