"""
Offline analytics consumer.

Per architect.md §6, this script acts as the analytics consumer for the
append-only log files written by TelemetryLogger.  It operates entirely
offline — no Redis or MongoDB connections — and is designed to be run as
a scheduled job or piped into a reporting tool.

Design constraints enforced here:
  - Stream processing: stream_entries() is a generator that reads the log
    file line-by-line.  The entire file is never loaded into memory, which
    is essential for log files that accumulate gigabytes of entries.
  - Resilient parsing: malformed JSON lines and blank lines (e.g., from
    log rotation) are skipped silently so a single bad write cannot halt
    an analytics run.
  - O(U) space for DAU: compute_dau() uses a Python set bounded by unique
    user count, not total click volume.
  - Date isolation: entries_for_date() filters by the UTC date embedded in
    each entry's `ts` field, preventing cross-day DAU bleed.
"""
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import date, datetime


class AnalyticsConsumer:

    def stream_entries(self, log_path: str) -> Iterator[dict]:
        """Yield parsed log entries one at a time; skip blank and malformed lines."""
        try:
            f = open(log_path)  # noqa: SIM115
        except FileNotFoundError:
            return
        with f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue

    def entries_for_date(self, log_path: str, target_date: date) -> Iterator[dict]:
        """Yield only entries whose UTC timestamp falls on target_date."""
        for entry in self.stream_entries(log_path):
            try:
                entry_date = datetime.fromisoformat(entry["ts"]).date()
            except (KeyError, ValueError):
                continue
            if entry_date == target_date:
                yield entry

    def compute_dau(self, entries: Iterable[dict]) -> int:
        """Count distinct client IPs. O(U) space bounded by unique user count."""
        seen: set[str] = set()
        for entry in entries:
            ip = entry.get("ip")
            if ip:
                seen.add(ip)
        return len(seen)

    def compute_total_clicks(self, entries: Iterable[dict]) -> dict[str, int]:
        """Count redirects per short code across the provided entries."""
        counts: Counter[str] = Counter()
        for entry in entries:
            code = entry.get("code")
            if code:
                counts[code] += 1
        return dict(counts)

    def click_count_for_code(self, log_path: str, code: str) -> int:
        """All-time redirect count for a single short code."""
        return sum(1 for e in self.stream_entries(log_path) if e.get("code") == code)

    def summary_for_date(self, log_path: str, target_date: date) -> dict:
        """DAU, total clicks, and top-5 codes for target_date in a single log pass."""
        seen_ips: set[str] = set()
        click_counts: Counter[str] = Counter()
        for entry in self.entries_for_date(log_path, target_date):
            if ip := entry.get("ip"):
                seen_ips.add(ip)
            if code := entry.get("code"):
                click_counts[code] += 1
        top = sorted(click_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        return {
            "date": target_date.isoformat(),
            "dau": len(seen_ips),
            "total_clicks": sum(click_counts.values()),
            "top_codes": [{"code": c, "clicks": n} for c, n in top],
        }
