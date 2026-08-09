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
from datetime import date, datetime
from typing import Iterable, Iterator


class AnalyticsConsumer:

    def stream_entries(self, log_path: str) -> Iterator[dict]:
        """Yield parsed log entries one at a time; skip blank and malformed lines."""
        with open(log_path) as f:
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
