#!/usr/bin/env python3
"""
Daily analytics report — offline consumer for TelemetryLogger output.

Run via Docker Compose (reads the shared telemetry_logs volume):

    docker compose --profile analytics run --rm analytics

Run directly against a local log file:

    LOG_PATH=/path/to/analytics.log python3 scripts/report.py
    LOG_PATH=/path/to/analytics.log python3 scripts/report.py --date 2026-08-09
"""
import os
import sys
from datetime import UTC, date, datetime
from app.analytics import AnalyticsConsumer

LOG_PATH = os.environ.get("LOG_PATH", "/var/log/url_shortener_analytics.log")
TOP_N    = int(os.environ.get("TOP_N", "20"))


def _parse_date(arg: str) -> date:
    try:
        return date.fromisoformat(arg)
    except ValueError:
        print(f"Invalid date '{arg}'. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    target = (
        _parse_date(sys.argv[sys.argv.index("--date") + 1])
        if "--date" in sys.argv
        else datetime.now(UTC).date()
    )

    consumer = AnalyticsConsumer()

    try:
        entries = list(consumer.entries_for_date(LOG_PATH, target))
    except FileNotFoundError:
        print(f"Log file not found: {LOG_PATH}", file=sys.stderr)
        sys.exit(1)

    dau    = consumer.compute_dau(iter(entries))
    clicks = consumer.compute_total_clicks(iter(entries))
    total  = sum(clicks.values())

    print(f"╔══════════════════════════════════════╗")
    print(f"║      URL Shortener Analytics         ║")
    print(f"╠══════════════════════════════════════╣")
    print(f"  Date          : {target}")
    print(f"  Daily Active Users (DAU) : {dau:>8,}")
    print(f"  Total redirects          : {total:>8,}")
    print(f"  Unique short codes       : {len(clicks):>8,}")
    if clicks:
        print(f"\n  Top {TOP_N} short codes by click volume:")
        print(f"  {'Code':<12}  {'Clicks':>10}")
        print(f"  {'-'*12}  {'-'*10}")
        for code, count in sorted(clicks.items(), key=lambda x: -x[1])[:TOP_N]:
            print(f"  {code:<12}  {count:>10,}")
    print(f"╚══════════════════════════════════════╝")


if __name__ == "__main__":
    main()
