"""
sunday_close.py — post-tournament pipeline.

Runs Sunday evening after the tournament ends. Idempotent: safe to re-run.

What it does:
  1. Fetch the most-recently-completed PGA event from ESPN (or one by ID).
  2. Resolve every leaderboard row through the crosswalk.
  3. Upsert the event into the ledger with final venue, purse, etc.
  4. If a pick was recorded for this event, fill in finish/earnings/cut.
  5. Identify the winner; update skins pot state (rollover if no pool entry
     picked the winner).
  6. Print a clean summary for the user.

What it does NOT do:
  - Make next week's pick (that's monday_open.py).
  - Burn the player (the unique constraint on picks.canonical_player_id
    already enforces one-use-per-season; burning is just a query, not
    a state change).

Usage (run on a real network):
  python sunday_close.py                       # auto-detect most recent
  python sunday_close.py --espn-event-id 4015...
  python sunday_close.py --demo                # offline self-test with mock data
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from normalize.players import PlayerCrosswalk, ensure_seeded
from fetchers.espn import ESPNFetcher, HTTPCache, TournamentRecord, LeaderboardRow
from ledger.ledger import Ledger

logger = logging.getLogger("sunday_close")


# Pool config — would normally come from a config file or .env
@dataclass
class PoolConfig:
    season: int
    entry_count: int                  # number of entries in your pool
    weekly_skins_contribution: float  # $ per event going to the skins pot


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline result for printing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CloseResult:
    event_name: str
    event_id: str
    is_completed: bool
    is_major: bool
    leaderboard_rows: int
    rows_resolved: int
    rows_queued: int
    winner_name: Optional[str]
    winner_canonical_id: Optional[str]
    pick_existed: bool
    pick_player_name: Optional[str]
    pick_position: Optional[str]
    pick_earnings: Optional[float]
    skins_pot_size: Optional[float]
    skins_winners_in_pool: Optional[int]
    skins_payout: Optional[float]
    skins_rolled_over: bool
    espn_data_incomplete: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_sunday_close(
    *,
    db_path: str | Path,
    pool: PoolConfig,
    espn_event_id: Optional[str] = None,
    pool_winners_picked: Optional[int] = None,
    mock_tournament: Optional[TournamentRecord] = None,
) -> CloseResult:
    """
    Run the close pipeline against an event.

    Args:
      espn_event_id: which event to close. If None, uses ESPN's current scoreboard.
      pool_winners_picked: how many entries in your pool picked the winner.
        For v1 this is a manual input each week — pulled from Splash Sports'
        post-deadline pick breakdown. Pass None to skip skins update.
      mock_tournament: for offline testing; bypasses the fetcher.
    """
    cache = HTTPCache(db_path)
    xwalk = PlayerCrosswalk(db_path)
    if ensure_seeded(xwalk):
        logger.info("Seeded empty crosswalk with OWGR top 200.")
    ledger = Ledger(db_path)
    espn = ESPNFetcher(cache, xwalk)

    try:
        # ── 1. Fetch the tournament ────────────────────────────────────
        if mock_tournament is not None:
            tournament = mock_tournament
        elif espn_event_id:
            tournament = espn.event_by_id(espn_event_id)
            if tournament is None:
                raise RuntimeError(f"ESPN returned no event for id {espn_event_id}.")
        else:
            tournament = espn.current_tournament()

        if tournament is None:
            raise RuntimeError("No tournament found on ESPN scoreboard.")

        # ── 2. Verify completion + ESPN data quality ───────────────────
        espn_data_incomplete = False
        if not tournament.is_completed:
            logger.warning(
                "Event %s is status='%s', not 'post'. Will record event but "
                "skip pick resolution and skins update.",
                tournament.espn_event_id, tournament.status,
            )
        elif tournament.leaderboard and not any(r.position for r in tournament.leaderboard):
            # Status is 'post' but no row has a position string. The scoreboard
            # endpoint returns positions only via /summary; if that 502s during
            # the fetch, we get a full field with no finishes. Flag loudly
            # rather than silently emitting a fake "(no winner)" result.
            logger.warning(
                "Event %s status is 'post' but ESPN returned no position data "
                "for any of %d players. Summary endpoint likely failed — re-run "
                "later when ESPN recovers. Skipping pick + skins resolution.",
                tournament.espn_event_id, len(tournament.leaderboard),
            )
            espn_data_incomplete = True

        # ── 3. Upsert the event ────────────────────────────────────────
        canonical_event_id = ledger.upsert_event(
            espn_event_id=tournament.espn_event_id,
            name=tournament.name,
            season=pool.season,
            start_date=tournament.start_date,
            end_date=tournament.end_date,
            venue=tournament.venue_name,
            course_name=tournament.course_name,
            purse=tournament.purse,
            status=tournament.status,
            short_name=tournament.short_name,
        )

        # ── 4. Identify winner & resolve leaderboard ───────────────────
        winner_row = _find_winner(tournament.leaderboard)
        winner_canonical_id = winner_row.canonical_id if winner_row else None
        winner_name = winner_row.raw_name if winner_row else None

        if winner_canonical_id:
            ledger.upsert_event(
                espn_event_id=tournament.espn_event_id,
                name=tournament.name,
                season=pool.season,
                status=tournament.status,
                winner_canonical_id=winner_canonical_id,
            )

        rows_resolved = sum(1 for r in tournament.leaderboard if r.canonical_id)
        rows_queued = sum(1 for r in tournament.leaderboard if not r.canonical_id)

        # ── 5. Resolve user's pick if one exists ───────────────────────
        pick = ledger.get_pick_for_event(canonical_event_id)
        pick_player_name = None
        pick_position = None
        pick_earnings = None
        if pick and tournament.is_completed and not espn_data_incomplete:
            # Find this player on the leaderboard
            pick_row = next(
                (r for r in tournament.leaderboard
                 if r.canonical_id == pick["canonical_player_id"]),
                None,
            )
            if pick_row:
                ledger.resolve_pick(
                    canonical_event_id=canonical_event_id,
                    position=pick_row.position,
                    score_to_par=pick_row.score_to_par,
                    earnings=pick_row.earnings,
                    made_cut=pick_row.made_cut,
                    fedex_points=pick_row.fedex_points,
                )
                pick_player_name = pick_row.raw_name
                pick_position = pick_row.position
                pick_earnings = pick_row.earnings
            else:
                player = xwalk.get_player(pick["canonical_player_id"])
                logger.warning(
                    "Pick %s was not found on the leaderboard. WD? Did not start?",
                    player["display_name"] if player else pick["canonical_player_id"],
                )

        # ── 6. Update skins pot state ──────────────────────────────────
        skins_pot_size = None
        skins_winners = None
        skins_payout = None
        skins_rolled = False
        if tournament.is_completed and not espn_data_incomplete and pool_winners_picked is not None:
            skins_pot_size = ledger.current_skins_pot(
                pool.season,
                weekly_contribution=pool.weekly_skins_contribution,
            )
            ledger.update_skins(
                season=pool.season,
                canonical_event_id=canonical_event_id,
                pot_size=skins_pot_size,
                winners_picked_count=pool_winners_picked,
            )
            skins_winners = pool_winners_picked
            skins_rolled = (pool_winners_picked == 0)
            skins_payout = (skins_pot_size / pool_winners_picked
                            if pool_winners_picked > 0 else None)

        return CloseResult(
            event_name=tournament.name,
            event_id=tournament.espn_event_id,
            is_completed=tournament.is_completed,
            is_major=bool(ledger.get_event(canonical_event_id)["is_major"]),
            leaderboard_rows=len(tournament.leaderboard),
            rows_resolved=rows_resolved,
            rows_queued=rows_queued,
            winner_name=winner_name,
            winner_canonical_id=winner_canonical_id,
            pick_existed=pick is not None,
            pick_player_name=pick_player_name,
            pick_position=pick_position,
            pick_earnings=pick_earnings,
            skins_pot_size=skins_pot_size,
            skins_winners_in_pool=skins_winners,
            skins_payout=skins_payout,
            skins_rolled_over=skins_rolled,
            espn_data_incomplete=espn_data_incomplete,
        )
    finally:
        cache.close()
        xwalk.close()
        ledger.close()


def _find_winner(leaderboard: list[LeaderboardRow]) -> Optional[LeaderboardRow]:
    """Position '1' (no tie). If T1, no winner for skins purposes — pool rules
    may differ; adjust if your league pays out on T1."""
    ones = [r for r in leaderboard if (r.position or "").strip() in ("1", "1st")]
    if len(ones) == 1:
        return ones[0]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Pretty-print summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(r: CloseResult) -> None:
    print()
    print("─" * 64)
    print(f"  Event:        {r.event_name}")
    print(f"  Status:       {'completed' if r.is_completed else 'NOT COMPLETED'}"
          f"{'  [MAJOR]' if r.is_major else ''}")
    print(f"  ESPN id:      {r.event_id}")
    print(f"  Leaderboard:  {r.leaderboard_rows} rows  "
          f"({r.rows_resolved} resolved, {r.rows_queued} queued)")
    if r.espn_data_incomplete:
        winner_line = "(ESPN data incomplete — re-run later)"
    else:
        winner_line = r.winner_name or "(no solo winner — likely T1)"
    print(f"  Winner:       {winner_line}")
    print()
    if r.espn_data_incomplete:
        print(f"  Pick / skins resolution skipped — re-run when ESPN's summary")
        print(f"  endpoint recovers (currently returning incomplete data).")
    elif r.pick_existed:
        if r.pick_player_name:
            earn = f"${r.pick_earnings:,.0f}" if r.pick_earnings is not None else "?"
            print(f"  Your pick:    {r.pick_player_name}")
            print(f"  Finish:       {r.pick_position}    Earnings: {earn}")
        else:
            print(f"  Your pick:    recorded, but not found on leaderboard (WD / DNS?)")
    else:
        print(f"  Your pick:    (no pick recorded for this event)")
    print()
    if r.skins_pot_size is not None:
        print(f"  Skins pot:    ${r.skins_pot_size:,.0f}")
        if r.skins_rolled_over:
            print(f"  Skins result: ROLLS OVER (no pool entry picked the winner)")
        elif r.skins_winners_in_pool == 1:
            print(f"  Skins result: solo winner pays ${r.skins_payout:,.0f}")
        else:
            print(f"  Skins result: {r.skins_winners_in_pool} entries split "
                  f"${r.skins_payout:,.0f} each")
    print("─" * 64)


# ─────────────────────────────────────────────────────────────────────────────
# Offline demo — runs the full pipeline against fake ESPN data
# ─────────────────────────────────────────────────────────────────────────────

def _build_mock_sony_open() -> TournamentRecord:
    """A plausible Sony Open final result, complete with our pick + result."""
    lb = [
        LeaderboardRow(
            espn_athlete_id="9478", canonical_id=None, raw_name="Scottie Scheffler",
            position="1", score_to_par=-23, total_strokes=257, made_cut=True,
            earnings=1566000.0, fedex_points=500.0,
            crosswalk_method="", crosswalk_confidence=0.0,
        ),
        LeaderboardRow(
            espn_athlete_id="4425906", canonical_id=None, raw_name="Cameron Young",
            position="2", score_to_par=-19, total_strokes=261, made_cut=True,
            earnings=948800.0, fedex_points=300.0,
            crosswalk_method="", crosswalk_confidence=0.0,
        ),
        LeaderboardRow(
            espn_athlete_id="10505", canonical_id=None, raw_name="J.T. Poston",
            position="T3", score_to_par=-17, total_strokes=263, made_cut=True,
            earnings=458200.0, fedex_points=175.0,
            crosswalk_method="", crosswalk_confidence=0.0,
        ),
        LeaderboardRow(
            espn_athlete_id="5467", canonical_id=None, raw_name="Jordan Spieth",
            position="T18", score_to_par=-9, total_strokes=271, made_cut=True,
            earnings=89400.0, fedex_points=42.0,
            crosswalk_method="", crosswalk_confidence=0.0,
        ),
        LeaderboardRow(
            espn_athlete_id="11253", canonical_id=None, raw_name="Rasmus Højgaard",
            position="CUT", score_to_par=4, total_strokes=144, made_cut=False,
            earnings=0.0, fedex_points=0.0,
            crosswalk_method="", crosswalk_confidence=0.0,
        ),
    ]
    return TournamentRecord(
        espn_event_id="401580351",
        name="Sony Open in Hawaii",
        short_name="Sony Open",
        start_date="2026-01-15",
        end_date="2026-01-18",
        status="post",
        is_completed=True,
        venue_name="Waialae Country Club",
        venue_city="Honolulu",
        course_name="Waialae Country Club",
        purse=8700000.0,
        leaderboard=lb,
        raw_status_type="STATUS_FINAL",
    )


def _demo(db_path: Path) -> None:
    """End-to-end demo: seed → record pick → run sunday_close → inspect ledger."""
    from normalize.seed import seed, print_report

    print("=" * 64)
    print(" Sunday Close — Offline Demo")
    print("=" * 64)
    print()

    # 1. Seed crosswalk
    print("[1] Seeding crosswalk with OWGR top 200 + supplemental...")
    print_report(seed(db_path))
    print()

    # 2. Resolve the leaderboard mock players against the seeded crosswalk
    #    (in real flow, ESPN fetcher does this; here we do it manually because
    #     we built the mock with raw_name only)
    xwalk = PlayerCrosswalk(db_path)
    mock = _build_mock_sony_open()
    for row in mock.leaderboard:
        r = xwalk.resolve(row.raw_name, source="espn", source_id=row.espn_athlete_id)
        row.canonical_id = r.canonical_id
        row.crosswalk_method = r.method
        row.crosswalk_confidence = r.confidence

    # 3. Record a pick BEFORE running close — this is the Thursday-lock action
    print("[2] Recording Thursday-lock pick: J.T. Poston @ Sony Open...")
    pool = PoolConfig(season=2026, entry_count=50, weekly_skins_contribution=50.0)
    ledger = Ledger(db_path)
    # The event doesn't exist in the ledger yet — record_pick requires it.
    # Pre-create the event row (in real flow monday_open does this).
    sony_canonical_id = ledger.upsert_event(
        espn_event_id="401580351",
        name="Sony Open in Hawaii",
        season=2026,
        start_date="2026-01-15",
        end_date="2026-01-18",
        status="scheduled",
    )
    poston_canonical = xwalk.resolve("J.T. Poston", source="espn",
                                     source_id="10505").canonical_id
    ledger.record_pick(
        season=2026,
        canonical_event_id=sony_canonical_id,
        canonical_player_id=poston_canonical,
        agent_confidence="MED",
        agent_rationale="Course-horse at Waialae; soft field; mid-tier deploy",
    )
    ledger.close()
    xwalk.close()
    print(f"    Pick recorded. Player canonical id: {poston_canonical}")
    print()

    # 4. Run the close pipeline
    print("[3] Running sunday_close with pool_winners_picked=2 (split skins)...")
    result = run_sunday_close(
        db_path=db_path,
        pool=pool,
        pool_winners_picked=2,
        mock_tournament=mock,
    )
    print_summary(result)
    print()

    # 5. Inspect final ledger state
    print("[4] Final ledger state:")
    ledger = Ledger(db_path)
    summary = ledger.season_summary(2026)
    print(f"    Picks made:       {summary['picks_made']}")
    print(f"    Cuts made:        {summary['cuts_made']}")
    print(f"    Cuts missed:      {summary['cuts_missed']}")
    print(f"    Total earnings:   ${summary['total_earnings']:,.0f}")
    print(f"    Events completed: {summary['events_completed']}")

    pick = ledger.get_pick_for_event(sony_canonical_id)
    print()
    print(f"    Pick detail:")
    print(f"      Position:     {pick['position']}")
    print(f"      Score to par: {pick['score_to_par']:+d}")
    print(f"      Earnings:     ${pick['earnings']:,.0f}")
    print(f"      Made cut:     {bool(pick['made_cut'])}")
    print(f"      Confidence:   {pick['agent_confidence']}")
    print(f"      Rationale:    {pick['agent_rationale']}")

    burned = ledger.burned_player_ids(2026)
    print()
    print(f"    Burned players this season: {len(burned)} ({burned})")

    # Test: burning Poston again should fail
    print()
    print("[5] Verifying one-and-done constraint (re-picking Poston should fail)...")
    try:
        ledger.record_pick(
            season=2026,
            canonical_event_id=ledger.upsert_event(
                espn_event_id="401580352",
                name="The American Express",
                season=2026,
                status="scheduled",
            ),
            canonical_player_id=poston_canonical,
        )
        print("    ✗ FAIL: re-pick was allowed")
    except Exception as e:
        print(f"    ✓ Constraint enforced: {type(e).__name__}: {e}")
    ledger.close()

    # Test idempotency: re-run close, ledger should be unchanged
    print()
    print("[6] Re-running close (idempotency check)...")
    result2 = run_sunday_close(
        db_path=db_path,
        pool=pool,
        pool_winners_picked=2,
        mock_tournament=mock,
    )
    print(f"    Re-run completed. Pick earnings still: ${result2.pick_earnings:,.0f}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Post-tournament Sunday-close pipeline.")
    parser.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"),
                        help="SQLite database path")
    parser.add_argument("--espn-event-id", help="Specific ESPN event ID to close")
    parser.add_argument("--season", type=int,
                        default=int(os.environ.get("QUIPU_SEASON", date.today().year)))
    parser.add_argument("--entries", type=int,
                        default=int(os.environ.get("QUIPU_POOL_ENTRIES", "12")),
                        help="Number of entries in your pool")
    parser.add_argument("--weekly-skins", type=float,
                        default=float(os.environ.get("QUIPU_WEEKLY_SKINS", "372.0")),
                        help="$ contribution per event to skins pot")
    parser.add_argument("--winners-picked", type=int,
                        help="How many pool entries picked this week's winner")
    parser.add_argument("--demo", action="store_true",
                        help="Run offline end-to-end demo against mock data")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)

    if args.demo:
        # nuke any prior demo state for a clean run
        if db.exists():
            db.unlink()
        _demo(db)
        return 0

    pool = PoolConfig(
        season=args.season,
        entry_count=args.entries,
        weekly_skins_contribution=args.weekly_skins,
    )
    result = run_sunday_close(
        db_path=db,
        pool=pool,
        espn_event_id=args.espn_event_id,
        pool_winners_picked=args.winners_picked,
    )
    print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
