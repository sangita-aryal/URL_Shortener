"""
Asynchronous telemetry logger.

Per architect.md §6, buffering analytics in Redis exhausts RAM and forces
LRU evictions of hot redirection mappings.  Instead, each FastAPI worker
appends structured JSON log entries to a local file.  An offline consumer
(app/analytics.py) processes the file independently of the hot path.

Design constraints enforced here:
  - Non-blocking: asyncio.to_thread() offloads the file write to the
    thread-pool executor so the event loop is never stalled.
  - Fire-and-forget: the route handler schedules this coroutine with
    asyncio.create_task() and returns the 302 immediately.
  - Fail-safe: any I/O error (disk full, permission denied, missing
    directory) is swallowed.  Telemetry loss is preferable to a failed
    redirect.
  - Append-only: opens the file in "a" mode so concurrent workers and
    successive calls never overwrite earlier entries.
"""
import asyncio
import json
from datetime import datetime, timezone


class TelemetryLogger:
    def __init__(self, log_path: str) -> None:
        self._log_path = log_path

    async def record_redirect(self, short_code: str, client_ip: str) -> None:
        entry = json.dumps({
            "ts":   datetime.now(timezone.utc).isoformat(),
            "code": short_code,
            "ip":   client_ip,
        })
        try:
            await asyncio.to_thread(self._append, entry)
        except Exception:
            pass

    def _append(self, line: str) -> None:
        with open(self._log_path, "a") as f:
            f.write(line + "\n")
