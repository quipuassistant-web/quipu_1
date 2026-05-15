"""
test_pga_championship.py — full pipeline integration on a realistic PGA scenario.

Simulates Chad's actual situation: mid-May 2026, starting fresh, the PGA
Championship is this week. We:

  1. Seed crosswalk (210 players).
  2. Backfill ~3 mock prior events to populate season_results (so the scorer
     has real form, not flat priors).
  3. Open the PGA Championship event with its field.
  4. Add Vegas odds.
  5. Score the field and produce a recommendation.
  6. Confirm the rationale references real season form for top picks.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from normalize.players import PlayerCrosswalk
from ledger.ledger import Ledger
from fetchers.espn import LeaderboardRow, TournamentRecord
from normalize.seed import seed
from backfill import backfill_event
from pipeline.monday_open import run_monday_open
from pipeline.sunday_close import PoolConfig
from add_odds import parse_block, store_odds, load_odds_for_event
from scoring.inputs import build_event_inputs
from scoring.multi_objective import score_field, ObjectiveWeights


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


def main() -> int:
    db = Path(tempfile.mkdtemp(prefix="quipu_pga_")) / "golf.db"

    pool = PoolConfig(season=2026, entry_count=50, weekly_skins_contribution=50.0)

    print("=" * 78)
    print(" PGA CHAMPIONSHIP — END-TO-END TEST")
    print(" Realistic mid-season scenario: backfill + odds + form + scorer")
    print("=" * 78)

    # ── 1. Seed ─────────────────────────────────────────────────────────
    print("\n[1] Seeding crosswalk...")
    seed(db)

    # ── 2. Backfill 3 prior 2026 events ─────────────────────────────────
    print("\n[2] Backfilling prior events (Sony, AmEx, Pebble) to populate form...")
    sony = mock_completed(
        "401580351", "Sony Open in Hawaii", "2026-01-15", "2026-01-18",
        "Waialae Country Club", 8_700_000,
        [
            ("9478", "Scottie Scheffler", "1", -23, 1566000),
            ("3470", "Rory McIlroy", "T4", -16, 510000),
            ("10592", "Collin Morikawa", "T8", -13, 290000),
            ("4848", "Justin Thomas", "T20", -8, 89000),
            ("9938", "Sam Burns", "T35", -3, 38000),
            ("4425906", "Cameron Young", "CUT", 4, 0),
        ],
    )
    masters = mock_completed(
        "401580360", "Masters Tournament", "2026-04-09", "2026-04-12",
        "Augusta National Golf Club", 21_000_000,
        [
            ("3470", "Rory McIlroy", "1", -13, 4200000),    # Rory wins Masters
            ("9478", "Scottie Scheffler", "2", -10, 2268000),
            ("10046", "Bryson DeChambeau", "T3", -8, 1196000),
            ("10592", "Collin Morikawa", "T8", -4, 460000),
            ("10140", "Xander Schauffele", "T15", -1, 200000),
            ("4848", "Justin Thomas", "T35", 4, 40000),
            ("9938", "Sam Burns", "CUT", 7, 0),
        ],
    )
    pebble = mock_completed(
        "401580354", "AT&T Pebble Beach Pro-Am", "2026-02-05", "2026-02-08",
        "Pebble Beach Golf Links", 20_000_000,
        [
            ("9478", "Scottie Scheffler", "1", -18, 3600000),
            ("5539", "Tommy Fleetwood", "T2", -16, 1960000),
            ("10140", "Xander Schauffele", "T2", -16, 1960000),
            ("3470", "Rory McIlroy", "T4", -13, 880000),
            ("10592", "Collin Morikawa", "T10", -9, 360000),
            ("9938", "Sam Burns", "T20", -5, 138000),
        ],
    )

    xw = PlayerCrosswalk(db)
    for t in (sony, masters, pebble):
        for row in t.leaderboard:
            r = xw.resolve(row.raw_name, source="espn", source_id=row.espn_athlete_id)
            row.canonical_id = r.canonical_id
    xw.close()

    for t in (sony, masters, pebble):
        stats = backfill_event(db_path=db, season=2026, tournament=t)
        print(f"  {t.name:<35}  {stats['rows_written']} rows")

    # ── 3. Open the PGA Championship ────────────────────────────────────
    print("\n[3] Opening the PGA Championship at Quail Hollow...")
    pga = TournamentRecord(
        espn_event_id="401580370", name="PGA Championship",
        short_name="PGA Champ", start_date="2026-05-14", end_date="2026-05-17",
        status="scheduled", is_completed=False,
        venue_name="Quail Hollow Club", venue_city="Charlotte",
        course_name="Quail Hollow Club", purse=18_500_000,
        leaderboard=[
            LeaderboardRow(
                espn_athlete_id=eid, canonical_id=None, raw_name=name,
                position=None, score_to_par=None, total_strokes=None,
                made_cut=None, earnings=None, fedex_points=None,
                crosswalk_method="", crosswalk_confidence=0.0,
            )
            for eid, name in [
                ("9478", "Scottie Scheffler"),
                ("3470", "Rory McIlroy"),
                ("10140", "Xander Schauffele"),
                ("9780", "Jon Rahm"),
                ("10046", "Bryson DeChambeau"),
                ("10592", "Collin Morikawa"),
                ("4848", "Justin Thomas"),
                ("4364873", "Viktor Hovland"),
                ("5860", "Hideki Matsuyama"),
                ("11099", "Joaquín Niemann"),
                ("9938", "Sam Burns"),
                ("5539", "Tommy Fleetwood"),
                ("6007", "Patrick Cantlay"),
                ("10980", "Sahith Theegala"),
                ("4425906", "Cameron Young"),
                ("10166", "J.J. Spaun"),
            ]
        ],
        raw_status_type="STATUS_SCHEDULED",
    )
    xw = PlayerCrosswalk(db)
    for row in pga.leaderboard:
        r = xw.resolve(row.raw_name, source="espn", source_id=row.espn_athlete_id)
        row.canonical_id = r.canonical_id
        row.crosswalk_method = r.method
        row.crosswalk_confidence = r.confidence
    xw.close()
    open_result = run_monday_open(db_path=db, pool=pool, mock_tournament=pga)
    print(f"  Field: {len(open_result.available_in_field)} available, "
          f"{open_result.field_size} total")
    print(f"  Major flag: {open_result.is_major}")
    assert open_result.is_major, "PGA Championship should be flagged as a major"

    # ── 4. Add Vegas odds ───────────────────────────────────────────────
    print("\n[4] Adding DraftKings odds...")
    odds_text = """
Scottie Scheffler     +450
Rory McIlroy          +700
Xander Schauffele     +1400
Jon Rahm              +1600
Bryson DeChambeau     +1800
Collin Morikawa       +2200
Justin Thomas         +2500
Viktor Hovland        +2800
Hideki Matsuyama      +3000
Joaquín Niemann       +3500
Sam Burns             +4000
Tommy Fleetwood       +4500
Patrick Cantlay       +5000
Sahith Theegala       +6000
Cameron Young         +6500
J.J. Spaun            +8000
"""
    parsed = parse_block(odds_text)
    stats = store_odds(db, "e_401580370", "draftkings", parsed)
    print(f"  Stored {stats['rows_written']} odds rows "
          f"({stats['rows_queued']} unresolved)")

    # ── 5. Score the field ──────────────────────────────────────────────
    print("\n[5] Building inputs and scoring...")
    vegas_odds = load_odds_for_event(db, "e_401580370")
    inputs = build_event_inputs(
        db, "e_401580370", season=2026,
        skins_pot=50.0, expected_winner_pickers=4.0,
        vegas_odds=vegas_odds,
    )
    scored = score_field(inputs, ObjectiveWeights())

    print(f"\n  Top 6 picks for the PGA Championship:")
    print(f"  {'Rank':<5} {'Player':<24} {'Score':<7} {'Fair Win':<9} "
          f"{'Form (W-T10-Cut)':<18} {'E[$]':>10}")
    print(f"  {'─'*5} {'─'*24} {'─'*7} {'─'*9} {'─'*18} {'─'*10}")
    for i, sp in enumerate(scored[:6], 1):
        p = next(pl for pl in inputs.players if pl.canonical_id == sp.canonical_id)
        form = f"{p.season_wins}W-{p.season_top_10s}T10-{p.season_cuts_made}/{p.season_starts}"
        print(f"  {i:<5} {sp.display_name:<24} {sp.composite_score:<7.1f} "
              f"{sp.finish_probs.win:<9.1%} {form:<18} "
              f"${sp.expected_earnings:>9,.0f}")

    print(f"\n  Top pick rationale ({scored[0].display_name}):")
    for line in scored[0].rationale:
        print(f"    • {line}")

    # ── 6. Validate ─────────────────────────────────────────────────────
    print("\n[6] Sanity checks...")

    # Vegas should be the probability source for top players (we provided it)
    top_probs = scored[0].finish_probs
    assert top_probs.source == "vegas", \
        f"Top pick should use vegas odds, got {top_probs.source}"
    print(f"  ✓ Top pick uses vegas as probability source")

    # Scheffler and McIlroy should be the top 2 (highest fair win pct)
    top2_names = {sp.display_name for sp in scored[:2]}
    assert "Scottie Scheffler" in top2_names and "Rory McIlroy" in top2_names, \
        f"Top 2 should include Scheffler and McIlroy, got {top2_names}"
    print(f"  ✓ Top 2 are Scheffler and McIlroy")

    # Verify season form actually flowed through
    scheff = next(p for p in inputs.players if "Scheffler" in p.display_name)
    assert scheff.season_starts >= 3, \
        f"Scheffler should have 3+ starts on record, got {scheff.season_starts}"
    assert scheff.season_wins >= 2, \
        f"Scheffler should have 2+ wins (Sony, Pebble), got {scheff.season_wins}"
    print(f"  ✓ Scheffler's backfilled form is visible: "
          f"{scheff.season_starts} starts, {scheff.season_wins} wins, "
          f"${scheff.season_earnings:,.0f}")

    # McIlroy should have his Masters win recorded
    rory = next(p for p in inputs.players if "McIlroy" in p.display_name)
    assert rory.season_wins >= 1, f"McIlroy should have his Masters win on record"
    print(f"  ✓ McIlroy's Masters win is in season_results: {rory.season_wins} wins")

    # Majors weight should be active (event is a major)
    rory_score = next(sp for sp in scored if "McIlroy" in sp.display_name)
    assert rory_score.score_majors > 0, \
        "Majors score should be non-zero for a major event"
    print(f"  ✓ Majors weight is active: McIlroy majors score = "
          f"{rory_score.score_majors:.1f}")

    print("\n" + "=" * 78)
    print(" ✓ FULL PIPELINE WORKING — BACKFILL + ODDS + SCORER")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
