"""
test_scorer.py — end-to-end demo of inputs → scoring → allocation.

Sets up a realistic 3-event lookahead (Sony just closed, AmEx + Farmers +
Pebble Beach ahead) with hand-built mock fields and Vegas-derived win odds
for ~30 favorites. Runs scoring and prints both the weekly pick recommendation
and the 3-event allocation plan.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from normalize.players import PlayerCrosswalk
from ledger.ledger import Ledger
from fetchers.espn import LeaderboardRow, TournamentRecord
from pipeline.monday_open import run_monday_open
from pipeline.sunday_close import (
    PoolConfig, run_sunday_close, _build_mock_sony_open,
)
from normalize.seed import seed
from scoring.inputs import build_event_inputs
from scoring.multi_objective import score_field, ObjectiveWeights
from scoring.allocation import allocate, print_plan


# Mock Vegas win probabilities (de-vigged, illustrative) keyed by ESPN id.
# In real use, scrape DraftKings outright market each Monday/Tuesday.
VEGAS_WIN_AMEX = {
    "9478":     0.085,   # Scheffler
    "11099":    0.060,   # Joaquín Niemann
    "9938":     0.055,   # Sam Burns
    "4425906":  0.050,   # Cameron Young
    "11119":    0.045,   # Wyndham Clark
    "5467":     0.040,   # Spieth
    "9843":     0.035,   # Knapp
    "11250":    0.030,   # Nicolai Højgaard
    "9530":     0.028,   # McNealy
    "4419142":  0.025,   # Bhatia
    "4585548":  0.020,   # Välimäki
    "4348470":  0.018,   # Reitan
    "9143":     0.015,   # Hubbard
}

VEGAS_WIN_FARMERS = {
    "9478":     0.10,
    "10592":    0.075,   # Morikawa
    "10140":    0.065,   # Schauffele
    "10046":    0.055,   # DeChambeau
    "5860":     0.045,   # Matsuyama
    "9938":     0.040,   # Burns
    "9530":     0.030,   # McNealy
    "11119":    0.028,   # Clark
    "5467":     0.025,   # Spieth
    "4419142":  0.020,
    "9037":     0.020,   # Fitzpatrick
}

VEGAS_WIN_PEBBLE = {
    "9478":     0.11,
    "3470":     0.08,    # McIlroy
    "5539":     0.06,    # Fleetwood
    "10140":    0.055,
    "10592":    0.050,
    "11378":    0.040,   # MacIntyre
    "5860":     0.040,
    "5467":     0.035,   # Spieth — historic Pebble lover
    "4364873":  0.035,   # Hovland
    "10046":    0.025,
    "4513":     0.020,   # Bradley
}


def _hydrate(tournament: TournamentRecord, db_path: Path) -> None:
    xw = PlayerCrosswalk(db_path)
    for row in tournament.leaderboard:
        r = xw.resolve(row.raw_name, source="espn", source_id=row.espn_athlete_id)
        row.canonical_id = r.canonical_id
        row.crosswalk_method = r.method
        row.crosswalk_confidence = r.confidence
    xw.close()


def _mock_event(espn_id, name, start_date, venue, course, purse,
                roster: list[tuple[str, str]]) -> TournamentRecord:
    """Build a scheduled-status mock event with the given (espn_id, name) roster."""
    lb = [LeaderboardRow(
        espn_athlete_id=eid, canonical_id=None, raw_name=name,
        position=None, score_to_par=None, total_strokes=None, made_cut=None,
        earnings=None, fedex_points=None, crosswalk_method="", crosswalk_confidence=0.0,
    ) for eid, name in roster]
    return TournamentRecord(
        espn_event_id=espn_id, name=name, short_name=None,
        start_date=start_date, end_date=None, status="scheduled",
        is_completed=False, venue_name=venue, venue_city=None,
        course_name=course, purse=purse, leaderboard=lb,
        raw_status_type="STATUS_SCHEDULED",
    )


def _amex() -> TournamentRecord:
    return _mock_event(
        "401580352", "The American Express", "2026-01-22",
        "PGA West", "Pete Dye Stadium Course", 8_800_000,
        [
            ("9478", "Scottie Scheffler"),
            ("11099", "Joaquín Niemann"),
            ("9938", "Sam Burns"),
            ("4425906", "Cameron Young"),
            ("11119", "Wyndham Clark"),
            ("5467", "Jordan Spieth"),
            ("9843", "Jake Knapp"),
            ("11250", "Nicolai Højgaard"),
            ("9530", "Maverick McNealy"),
            ("4419142", "Akshay Bhatia"),
            ("4585548", "Sami Välimäki"),
            ("4348470", "Kristoffer Reitan"),
            ("9143", "Mark Hubbard"),
            ("10505", "J.T. Poston"),    # BURNED from Sony
        ],
    )


def _farmers() -> TournamentRecord:
    return _mock_event(
        "401580353", "Farmers Insurance Open", "2026-01-29",
        "Torrey Pines", "South Course", 9_500_000,
        [
            ("9478", "Scottie Scheffler"),
            ("10592", "Collin Morikawa"),
            ("10140", "Xander Schauffele"),
            ("10046", "Bryson DeChambeau"),
            ("5860", "Hideki Matsuyama"),
            ("9938", "Sam Burns"),
            ("9530", "Maverick McNealy"),
            ("11119", "Wyndham Clark"),
            ("5467", "Jordan Spieth"),
            ("4419142", "Akshay Bhatia"),
            ("9037", "Matt Fitzpatrick"),
            ("4425906", "Cameron Young"),
        ],
    )


def _pebble() -> TournamentRecord:
    return _mock_event(
        "401580354", "AT&T Pebble Beach Pro-Am", "2026-02-05",
        "Pebble Beach Golf Links", "Pebble Beach", 20_000_000,
        [
            ("9478", "Scottie Scheffler"),
            ("3470", "Rory McIlroy"),
            ("5539", "Tommy Fleetwood"),
            ("10140", "Xander Schauffele"),
            ("10592", "Collin Morikawa"),
            ("11378", "Robert MacIntyre"),
            ("5860", "Hideki Matsuyama"),
            ("5467", "Jordan Spieth"),
            ("4364873", "Viktor Hovland"),
            ("10046", "Bryson DeChambeau"),
            ("4513", "Keegan Bradley"),
        ],
    )


def main() -> int:
    db = Path(tempfile.mkdtemp(prefix="quipu_scorer_")) / "golf.db"

    pool = PoolConfig(season=2026, entry_count=50, weekly_skins_contribution=50.0)

    print("=" * 78)
    print(" SCORER + ALLOCATOR END-TO-END")
    print("=" * 78)

    # ── Setup: seed and burn Poston ─────────────────────────────────────
    print("\n[1] Seeding crosswalk...")
    seed(db)

    # Run a Sony close to burn Poston so AmEx scoring excludes him
    print("[2] Closing Sony Open (Poston pick → burned)...")
    sony = _build_mock_sony_open()
    _hydrate(sony, db)
    ledger = Ledger(db)
    sony_id = ledger.upsert_event(
        espn_event_id="401580351", name="Sony Open in Hawaii",
        season=2026, status="scheduled",
    )
    xw = PlayerCrosswalk(db)
    poston_id = xw.resolve("J.T. Poston", source="espn", source_id="10505").canonical_id
    ledger.record_pick(season=2026, canonical_event_id=sony_id,
                       canonical_player_id=poston_id, agent_confidence="MED")
    xw.close()
    ledger.close()
    run_sunday_close(db_path=db, pool=pool, pool_winners_picked=2, mock_tournament=sony)

    # ── Open all three upcoming events ──────────────────────────────────
    print("\n[3] Opening AmEx, Farmers, and Pebble Beach...")
    for tournament in (_amex(), _farmers(), _pebble()):
        _hydrate(tournament, db)
        result = run_monday_open(db_path=db, pool=pool, mock_tournament=tournament)
        print(f"    {result.event_name:<30s}  "
              f"avail={len(result.available_in_field):2d}  "
              f"burned={len(result.burned_in_field):2d}")

    # ── Score AmEx in isolation ─────────────────────────────────────────
    print("\n[4] Scoring AmEx in isolation (weekly view)...")
    xw = PlayerCrosswalk(db)
    vegas_amex = {xw.resolve(_lookup_name(eid), source="t", source_id=eid).canonical_id: pct
                  for eid, pct in VEGAS_WIN_AMEX.items()}
    xw.close()
    amex_inputs = build_event_inputs(
        db, "e_401580352",
        season=2026, skins_pot=50.0,
        expected_winner_pickers=4.0,
        vegas_odds=vegas_amex,
    )
    scored = score_field(amex_inputs, ObjectiveWeights())
    print(f"\n  Top 5 picks for {amex_inputs.name}:")
    print(f"  {'Rank':<5} {'Player':<26} {'Score':<7} {'P(win)':<8} "
          f"{'E[$]':<10} {'E[skins]':<9}")
    print(f"  {'─'*5} {'─'*26} {'─'*7} {'─'*8} {'─'*10} {'─'*9}")
    for i, sp in enumerate(scored[:5], 1):
        print(f"  {i:<5} {sp.display_name:<26} {sp.composite_score:<7.1f} "
              f"{sp.finish_probs.win:<8.1%} "
              f"${sp.expected_earnings:<9,.0f} ${sp.expected_skins_payout:<8.2f}")
    print(f"\n  Top pick rationale ({scored[0].display_name}):")
    for line in scored[0].rationale:
        print(f"    • {line}")

    # ── Verify burned exclusion ─────────────────────────────────────────
    poston_in_scored = any(sp.canonical_id == poston_id for sp in scored)
    assert not poston_in_scored, "Burned Poston should not appear in scored field"
    print(f"\n  ✓ Poston (burned) correctly absent from scored field")

    # ── Run 3-event allocation ──────────────────────────────────────────
    print("\n[5] Running 3-event forward allocation...")
    xw = PlayerCrosswalk(db)
    vegas_farmers = {xw.resolve(_lookup_name(eid), source="t", source_id=eid).canonical_id: pct
                     for eid, pct in VEGAS_WIN_FARMERS.items()}
    vegas_pebble = {xw.resolve(_lookup_name(eid), source="t", source_id=eid).canonical_id: pct
                    for eid, pct in VEGAS_WIN_PEBBLE.items()}
    xw.close()

    all_inputs = [
        build_event_inputs(db, "e_401580352", season=2026, skins_pot=50.0,
                           vegas_odds=vegas_amex, expected_winner_pickers=4.0),
        build_event_inputs(db, "e_401580353", season=2026, skins_pot=50.0,
                           vegas_odds=vegas_farmers, expected_winner_pickers=4.0),
        build_event_inputs(db, "e_401580354", season=2026, skins_pot=50.0,
                           vegas_odds=vegas_pebble, expected_winner_pickers=4.0),
    ]
    plan = allocate(all_inputs, ObjectiveWeights())
    print_plan(plan)

    # ── Verify allocation respects one-and-done ─────────────────────────
    assigned = [ep.recommended_player for ep in plan.events if ep.recommended_player]
    assert len(assigned) == len(set(assigned)), \
        f"Allocation assigned same player twice: {assigned}"
    print("\n  ✓ Allocation respects one-and-done (no player used twice)")

    # The agent SHOULD prefer to save Scheffler for the higher-purse Pebble Beach
    # over the lower-purse AmEx, since both events score him similarly.
    pebble_plan = next(e for e in plan.events if "Pebble" in e.name)
    print(f"\n  Pebble Beach (highest-purse week) → {pebble_plan.recommended_player_name}")
    print(f"  AmEx (lower-purse week)            → "
          f"{next(e for e in plan.events if 'AmEx' in e.name or 'American Express' in e.name).recommended_player_name}")

    print("\n" + "=" * 78)
    print(" ✓ SCORING + ALLOCATION WORKING")
    print("=" * 78)
    return 0


_NAME_BY_ESPN_ID = {
    "9478": "Scottie Scheffler", "3470": "Rory McIlroy",
    "5539": "Tommy Fleetwood", "10592": "Collin Morikawa",
    "10140": "Xander Schauffele", "10046": "Bryson DeChambeau",
    "5860": "Hideki Matsuyama", "9938": "Sam Burns",
    "9530": "Maverick McNealy", "11099": "Joaquín Niemann",
    "4425906": "Cameron Young", "11119": "Wyndham Clark",
    "5467": "Jordan Spieth", "9843": "Jake Knapp",
    "11250": "Nicolai Højgaard", "4419142": "Akshay Bhatia",
    "4585548": "Sami Välimäki", "4348470": "Kristoffer Reitan",
    "9143": "Mark Hubbard", "10505": "J.T. Poston",
    "9037": "Matt Fitzpatrick", "11378": "Robert MacIntyre",
    "4364873": "Viktor Hovland", "4513": "Keegan Bradley",
}


def _lookup_name(espn_id: str) -> str:
    return _NAME_BY_ESPN_ID.get(espn_id, "")


if __name__ == "__main__":
    sys.exit(main())
