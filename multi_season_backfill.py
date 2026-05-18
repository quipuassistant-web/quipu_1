"""
multi_season_backfill.py — orchestrate backfill across multiple PGA Tour seasons.

The PGA Tour calendar runs roughly January through August now (post-2023 schedule
changes; the wraparound fall season was eliminated). For course history we want
3-4 years of data: enough to see real venue patterns, not so much that ancient
data dilutes recent form.

Usage:
  # Standard course-history backfill — last 4 seasons
  python multi_season_backfill.py --seasons 2023,2024,2025,2026

  # Just one season (for testing)
  python multi_season_backfill.py --seasons 2024

  # Verbose with per-event progress
  python multi_season_backfill.py --seasons 2023,2024,2025 --verbose

Network heavy: each season is ~30 events, each event's leaderboard is ~70-156
rows. Expect 5-15 minutes for a 4-season backfill against a cold cache.
Re-runs are fast (cached).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from backfill import backfill_range

logger = logging.getLogger("multi_season")


# Explicit season windows for years where the default (Jan 1 → Dec 31) is
# wider than the actual schedule — e.g. the 2023-pre era when the PGA Tour
# season ended in August. New seasons fall through to the default below.
SEASON_WINDOWS = {
    2023: ("2023-01-01", "2023-08-31"),  # 2022-23 calendar (Aug-end)
    2024: ("2024-01-01", "2024-08-31"),
    2025: ("2025-01-01", "2025-08-31"),
}


def _window_for(year: int) -> tuple[str, str]:
    """Look up an explicit window or default to the full calendar year."""
    return SEASON_WINDOWS.get(year, (f"{year}-01-01", f"{year}-12-31"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill multiple PGA seasons.")
    parser.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    parser.add_argument("--seasons", required=True,
                        help="Comma-separated list, e.g. 2023,2024,2025,2026")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--step-days", type=int, default=14,
                        help="Date range chunk size; larger reduces request count")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    seasons = [int(s.strip()) for s in args.seasons.split(",") if s.strip()]

    if not seasons:
        print("ERROR: no seasons specified", file=sys.stderr)
        return 1

    print(f"Backfilling {len(seasons)} seasons → {args.db}")
    if args.dry_run:
        print("(DRY RUN — no writes)")
    print()

    grand_totals = {
        "events_seen": 0, "events_written": 0,
        "results_written": 0, "skipped_in_progress": 0,
        "skipped_unresolved_rows": 0, "skipped_no_leaderboard": 0,
    }

    for season in seasons:
        start_str, end_str = _window_for(season)
        # Cap end at today if the season is current/future
        end_dt = min(
            datetime.strptime(end_str, "%Y-%m-%d"),
            datetime.now(),
        )
        start_dt = datetime.strptime(start_str, "%Y-%m-%d")

        print(f"━━━━ Season {season}: {start_dt.date()} → {end_dt.date()} ━━━━")
        stats = backfill_range(
            db_path=args.db, season=season,
            start=start_dt, end=end_dt,
            step_days=args.step_days, dry_run=args.dry_run,
        )
        for k, v in stats.items():
            grand_totals[k] = grand_totals.get(k, 0) + v
            print(f"  {k:30s}  {v}")
        print()

    print("━━━━ GRAND TOTAL ━━━━")
    for k, v in grand_totals.items():
        print(f"  {k:30s}  {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
