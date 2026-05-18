"""
backfill.py — populate events + season_results for past tournaments.

For mid-season starts (or recovering from a fresh DB), iterate week-by-week
from a start date to today, pull each scoreboard, and write completed events'
full leaderboards into season_results. This is what powers per-player season
form once you're past week 1.

Usage:
  python backfill.py --from 2026-01-01 --to 2026-05-12 --season 2026
  python backfill.py --from 2026-01-01 --dry-run     # see what would be pulled

Networking required (talks to ESPN). The sandbox can't reach ESPN — run on
your own machine.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fetchers.espn import ESPNFetcher, HTTPCache, TournamentRecord
from ledger.ledger import Ledger
from normalize.players import PlayerCrosswalk, ensure_seeded

logger = logging.getLogger("backfill")


def _leaderboard_has_positions(leaderboard) -> bool:
    """True if the leaderboard is non-empty AND at least one row has a position.
    A completed event with rows but no positions usually means ESPN's /summary
    endpoint failed and we got only the scoreboard skeleton — writing those
    rows to season_results corrupts the form data the scorer reads."""
    return bool(leaderboard) and any(r.position for r in leaderboard)


def backfill_range(
    *,
    db_path: str | Path,
    season: int,
    start: datetime,
    end: datetime,
    step_days: int = 7,
    dry_run: bool = False,
) -> dict:
    """
    Walk the date range in week-long chunks (ESPN's natural granularity) and
    backfill every completed event encountered.

    Returns a stats dict: {events_seen, events_written, results_written, skipped_in_progress}.
    """
    cache = HTTPCache(db_path)
    xwalk = PlayerCrosswalk(db_path)
    if ensure_seeded(xwalk):
        logger.info("Seeded empty crosswalk with OWGR top 200.")
    ledger = Ledger(db_path)
    espn = ESPNFetcher(cache, xwalk)

    stats = {"events_seen": 0, "events_written": 0,
             "results_written": 0, "skipped_in_progress": 0,
             "skipped_unresolved_rows": 0}
    seen_event_ids: set[str] = set()

    try:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=step_days), end)
            logger.info("Fetching %s → %s", cursor.date(), chunk_end.date())

            try:
                events = espn.tournaments_in_range(cursor, chunk_end)
            except Exception as e:
                logger.warning("Range %s→%s failed: %s", cursor.date(), chunk_end.date(), e)
                cursor = chunk_end
                continue

            for tournament in events:
                if tournament.espn_event_id in seen_event_ids:
                    continue
                seen_event_ids.add(tournament.espn_event_id)
                stats["events_seen"] += 1

                if not tournament.is_completed:
                    stats["skipped_in_progress"] += 1
                    logger.info("  skip (in progress / scheduled): %s",
                                tournament.name)
                    continue

                # Fallback: scoreboard sometimes returns completed events with
                # empty competitor arrays OR with full rows but no position
                # data (the position field only comes via /summary). Retry via
                # summary in either case.
                if not _leaderboard_has_positions(tournament.leaderboard):
                    logger.info("  scoreboard returned no positions for %s; "
                                "retrying via summary", tournament.name)
                    detail = espn.event_by_id(tournament.espn_event_id)
                    if detail and _leaderboard_has_positions(detail.leaderboard):
                        tournament = detail
                    else:
                        logger.warning(
                            "  no usable leaderboard for %s (summary "
                            "unavailable too); skipping season_results write",
                            tournament.name,
                        )
                        stats.setdefault("skipped_no_leaderboard", 0)
                        stats["skipped_no_leaderboard"] += 1
                        continue

                logger.info("  write: %s  (%d rows)",
                            tournament.name, len(tournament.leaderboard))

                # Count unresolved rows regardless of dry-run, so the
                # pre-flight summary is honest about how many ESPN names
                # have no canonical id in the crosswalk.
                unresolved_count = sum(
                    1 for r in tournament.leaderboard if not r.canonical_id
                )
                stats["skipped_unresolved_rows"] += unresolved_count

                if dry_run:
                    continue

                # Upsert the event
                canonical_event_id = ledger.upsert_event(
                    espn_event_id=tournament.espn_event_id,
                    name=tournament.name,
                    season=season,
                    start_date=tournament.start_date,
                    end_date=tournament.end_date,
                    venue=tournament.venue_name,
                    course_name=tournament.course_name,
                    purse=tournament.purse,
                    status=tournament.status,
                    short_name=tournament.short_name,
                )
                stats["events_written"] += 1

                # Find solo winner for events table
                ones = [r for r in tournament.leaderboard
                        if (r.position or "").strip() in ("1", "1st")]
                if len(ones) == 1 and ones[0].canonical_id:
                    ledger.upsert_event(
                        espn_event_id=tournament.espn_event_id,
                        name=tournament.name,
                        season=season,
                        status=tournament.status,
                        winner_canonical_id=ones[0].canonical_id,
                    )

                # Write all leaderboard rows. Unresolved rows were already
                # counted into stats above; just filter them out here.
                payload = [
                    {
                        "canonical_player_id": r.canonical_id,
                        "position": r.position,
                        "score_to_par": r.score_to_par,
                        "earnings": r.earnings,
                        "made_cut": r.made_cut,
                        "fedex_points": r.fedex_points,
                    }
                    for r in tournament.leaderboard if r.canonical_id
                ]
                stats["results_written"] += ledger.upsert_results(
                    canonical_event_id, season, payload,
                )

            cursor = chunk_end

        return stats
    finally:
        cache.close()
        xwalk.close()
        ledger.close()


def backfill_event(
    *,
    db_path: str | Path,
    season: int,
    espn_event_id: Optional[str] = None,
    tournament: Optional[TournamentRecord] = None,
    dry_run: bool = False,
) -> dict:
    """
    Backfill a single event. Pass either an espn_event_id (fetched via the
    summary endpoint) or a pre-resolved TournamentRecord (used by the demo
    and by the range walker when retrying).
    """
    cache = HTTPCache(db_path)
    xwalk = PlayerCrosswalk(db_path)
    if ensure_seeded(xwalk):
        logger.info("Seeded empty crosswalk with OWGR top 200.")
    ledger = Ledger(db_path)
    espn = ESPNFetcher(cache, xwalk)
    stats = {"event_id": None, "name": None, "rows_written": 0,
             "unresolved": 0, "status": "ok"}
    try:
        if tournament is None:
            if not espn_event_id:
                stats["status"] = "no_event_specified"
                return stats
            tournament = espn.event_by_id(espn_event_id)
            if tournament is None:
                stats["status"] = "event_not_found"
                stats["event_id"] = espn_event_id
                return stats

        stats["event_id"] = tournament.espn_event_id
        stats["name"] = tournament.name

        if not tournament.is_completed:
            stats["status"] = "not_completed"
            return stats

        if not _leaderboard_has_positions(tournament.leaderboard):
            # Scoreboard alone often returns rows without position data;
            # the /summary endpoint is the authoritative source.
            detail = espn.event_by_id(tournament.espn_event_id)
            if detail and _leaderboard_has_positions(detail.leaderboard):
                tournament = detail
            else:
                stats["status"] = "no_leaderboard"
                return stats

        unresolved = sum(1 for r in tournament.leaderboard if not r.canonical_id)
        stats["unresolved"] = unresolved

        if dry_run:
            stats["rows_written"] = sum(1 for r in tournament.leaderboard if r.canonical_id)
            stats["status"] = "dry_run"
            return stats

        canonical_event_id = ledger.upsert_event(
            espn_event_id=tournament.espn_event_id,
            name=tournament.name,
            season=season,
            start_date=tournament.start_date,
            end_date=tournament.end_date,
            venue=tournament.venue_name,
            course_name=tournament.course_name,
            purse=tournament.purse,
            status=tournament.status,
            short_name=tournament.short_name,
        )

        # Solo winner attribution
        ones = [r for r in tournament.leaderboard
                if (r.position or "").strip() in ("1", "1st")]
        if len(ones) == 1 and ones[0].canonical_id:
            ledger.upsert_event(
                espn_event_id=tournament.espn_event_id,
                name=tournament.name, season=season, status=tournament.status,
                winner_canonical_id=ones[0].canonical_id,
            )

        payload = [
            {
                "canonical_player_id": r.canonical_id,
                "position": r.position,
                "score_to_par": r.score_to_par,
                "earnings": r.earnings,
                "made_cut": r.made_cut,
                "fedex_points": r.fedex_points,
            }
            for r in tournament.leaderboard if r.canonical_id
        ]
        stats["rows_written"] = ledger.upsert_results(canonical_event_id, season, payload)
        return stats
    finally:
        cache.close()
        xwalk.close()
        ledger.close()


def _demo(db_path: Path) -> None:
    """Offline demo — seeds, backfills three mock past events, validates form."""
    from normalize.seed import seed
    from fetchers.espn import LeaderboardRow

    print("=" * 72)
    print(" BACKFILL — Offline Demo")
    print("=" * 72)

    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n[1] Seeding crosswalk...")
    seed(db_path)

    def mock_completed(eid, name, start, end, course, purse, rows):
        lb = [
            LeaderboardRow(
                espn_athlete_id=r[0], canonical_id=None, raw_name=r[1],
                position=r[2], score_to_par=r[3], total_strokes=None,
                made_cut=(r[2] not in ("CUT", "WD", "DQ")),
                earnings=r[4], fedex_points=None,
                crosswalk_method="", crosswalk_confidence=0.0,
            )
            for r in rows
        ]
        return TournamentRecord(
            espn_event_id=eid, name=name, short_name=None,
            start_date=start, end_date=end, status="post",
            is_completed=True, venue_name=course, venue_city=None,
            course_name=course, purse=purse, leaderboard=lb,
            raw_status_type="STATUS_FINAL",
        )

    print("\n[2] Building 3 mock completed events (Sony / AmEx / Pebble)...")
    sony = mock_completed(
        "401580351", "Sony Open in Hawaii", "2026-01-15", "2026-01-18",
        "Waialae Country Club", 8_700_000,
        [
            ("9478", "Scottie Scheffler", "1", -23, 1566000),
            ("4425906", "Cameron Young", "2", -19, 948800),
            ("10505", "J.T. Poston", "T3", -17, 458200),
            ("5467", "Jordan Spieth", "T18", -9, 89400),
            ("11253", "Rasmus Højgaard", "CUT", 4, 0),
        ],
    )
    amex = mock_completed(
        "401580352", "The American Express", "2026-01-22", "2026-01-25",
        "PGA West", 8_800_000,
        [
            ("11099", "Joaquín Niemann", "1", -26, 1584000),
            ("9938", "Sam Burns", "2", -23, 959200),
            ("4425906", "Cameron Young", "T3", -21, 463100),
            ("9478", "Scottie Scheffler", "T3", -21, 463100),
            ("5467", "Jordan Spieth", "CUT", 3, 0),
        ],
    )
    pebble = mock_completed(
        "401580354", "AT&T Pebble Beach Pro-Am", "2026-02-05", "2026-02-08",
        "Pebble Beach Golf Links", 20_000_000,
        [
            ("9478", "Scottie Scheffler", "1", -18, 3600000),
            ("5539", "Tommy Fleetwood", "2", -16, 2160000),
            ("10592", "Collin Morikawa", "3", -14, 1360000),
            ("3470", "Rory McIlroy", "T4", -12, 880000),
            ("5860", "Hideki Matsuyama", "T4", -12, 880000),
            ("5467", "Jordan Spieth", "T18", -4, 156000),
            ("11099", "Joaquín Niemann", "T35", 1, 38000),
        ],
    )

    # Resolve through crosswalk (real ESPN fetcher does this automatically)
    xw = PlayerCrosswalk(db_path)
    for t in (sony, amex, pebble):
        for row in t.leaderboard:
            r = xw.resolve(row.raw_name, source="espn", source_id=row.espn_athlete_id)
            row.canonical_id = r.canonical_id
            row.crosswalk_method = r.method
            row.crosswalk_confidence = r.confidence
    xw.close()

    print("\n[3] Backfilling each event...")
    for t in (sony, amex, pebble):
        stats = backfill_event(db_path=db_path, season=2026, tournament=t)
        print(f"  {t.name:<32}  {stats['rows_written']:>3} rows, "
              f"{stats['unresolved']} unresolved  [{stats['status']}]")

    print("\n[4] Verifying season form is now populated...")
    ledger = Ledger(db_path)
    for name in ("Scottie Scheffler", "Jordan Spieth", "Joaquín Niemann"):
        xw = PlayerCrosswalk(db_path)
        cid = xw.resolve(name, source="t").canonical_id
        xw.close()
        rows = ledger.player_season_results(cid, 2026)
        print(f"\n  {name}: {len(rows)} events on record")
        for r in rows:
            mc = "made" if r["made_cut"] == 1 else "miss" if r["made_cut"] == 0 else "?"
            earn = f"${r['earnings']:,.0f}" if r["earnings"] else "$0"
            stp = r["score_to_par"]
            stp_str = f"{stp:+d}" if stp is not None else "?"
            print(f"    {r['event_name']:<32} {r['position'] or '?':<5} "
                  f"{stp_str:<5} {earn:>11}  ({mc})")

    print("\n[5] Sanity assertions...")
    xw = PlayerCrosswalk(db_path)
    scheff = xw.resolve("Scottie Scheffler", source="t").canonical_id
    spieth = xw.resolve("Jordan Spieth", source="t").canonical_id
    niemann = xw.resolve("Joaquín Niemann", source="t").canonical_id
    xw.close()

    scheff_rows = ledger.player_season_results(scheff, 2026)
    spieth_rows = ledger.player_season_results(spieth, 2026)
    niemann_rows = ledger.player_season_results(niemann, 2026)

    assert len(scheff_rows) == 3, f"Scheffler should have 3 events, got {len(scheff_rows)}"
    print(f"  ✓ Scheffler has 3 events in season_results")
    scheff_wins = sum(1 for r in scheff_rows if r["position"] == "1")
    assert scheff_wins == 2, f"Scheffler should have 2 wins, got {scheff_wins}"
    print(f"  ✓ Scheffler has 2 wins (Sony, Pebble)")
    spieth_cuts = sum(1 for r in spieth_rows if r["made_cut"] == 0)
    assert spieth_cuts == 1, f"Spieth should have 1 missed cut, got {spieth_cuts}"
    print(f"  ✓ Spieth has 1 missed cut (AmEx)")
    assert any(r["position"] == "1" for r in niemann_rows)
    print(f"  ✓ Niemann's AmEx win recorded")

    print("\n[6] Verifying scorer reads the new form data...")
    from scoring.inputs import PlayerInputs, _hydrate_season_form
    inp = PlayerInputs(canonical_id=scheff, display_name="Scottie Scheffler")
    _hydrate_season_form(ledger, inp, 2026)
    assert inp.season_starts == 3
    assert inp.season_wins == 2
    assert inp.season_top_10s == 3  # Sony 1st, AmEx T3, Pebble 1st — all top 10
    print(f"  ✓ Scorer sees {inp.season_starts} starts, {inp.season_wins} wins, "
          f"{inp.season_top_10s} T10s, ${inp.season_earnings:,.0f} earned")

    print("\n[7] Idempotency: re-running backfill produces same row count...")
    for t in (sony, amex, pebble):
        backfill_event(db_path=db_path, season=2026, tournament=t)
    scheff_rows2 = ledger.player_season_results(scheff, 2026)
    assert len(scheff_rows2) == 3, "Re-run should not duplicate"
    print(f"  ✓ Re-run produced same count: {len(scheff_rows2)}")

    ledger.close()

    print("\n" + "=" * 72)
    print(" ✓ BACKFILL + SEASON_RESULTS PIPELINE WORKING")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill past PGA events from ESPN.")
    parser.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    parser.add_argument("--from", dest="from_date",
                        help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date",
                        default=datetime.now().strftime("%Y-%m-%d"),
                        help="End date (YYYY-MM-DD), default today")
    parser.add_argument("--season", type=int,
                        default=int(os.environ.get("QUIPU_SEASON", date.today().year)))
    parser.add_argument("--step-days", type=int, default=7,
                        help="Date range chunk size (default 7)")
    parser.add_argument("--espn-event-id",
                        help="Backfill a single event by ESPN ID")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="Run offline self-test with mock events")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    db = Path(args.db)

    if args.demo:
        _demo(db)
        return 0

    if args.espn_event_id:
        stats = backfill_event(
            db_path=db, season=args.season,
            espn_event_id=args.espn_event_id, dry_run=args.dry_run,
        )
        print(f"{stats.get('name') or stats.get('event_id')}: "
              f"{stats['rows_written']} rows, "
              f"{stats['unresolved']} unresolved  [{stats['status']}]")
        return 0

    if not args.from_date:
        print("ERROR: provide --from YYYY-MM-DD, or --espn-event-id, or --demo",
              file=sys.stderr)
        return 1

    start = datetime.strptime(args.from_date, "%Y-%m-%d")
    end = datetime.strptime(args.to_date, "%Y-%m-%d")
    if start >= end:
        print("ERROR: --from must precede --to", file=sys.stderr)
        return 1

    print(f"Backfilling {args.from_date} → {args.to_date} into {args.db}")
    if args.dry_run:
        print("(DRY RUN — no writes)")
    print()

    stats = backfill_range(
        db_path=db, season=args.season, start=start, end=end,
        step_days=args.step_days, dry_run=args.dry_run,
    )

    print()
    print("Backfill complete:")
    for k, v in stats.items():
        print(f"  {k:30s}  {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
