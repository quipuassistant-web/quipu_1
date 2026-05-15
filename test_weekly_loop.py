"""
test_weekly_loop.py — integration test for the full Sun→Mon→Thu→Sun cycle.

Walks through two consecutive weeks (Sony Open → American Express) and
verifies that picks, results, burns, skins state, and one-and-done all
behave correctly across the cycle.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from normalize.players import PlayerCrosswalk
from ledger.ledger import Ledger
from pipeline.sunday_close import (
    run_sunday_close, PoolConfig, _build_mock_sony_open,
)
from pipeline.monday_open import run_monday_open, _build_mock_amex_field
from normalize.seed import seed


def run_cli(*args: str) -> tuple[int, str, str]:
    """Run record_pick.py and capture output."""
    proc = subprocess.run(
        [sys.executable, "record_pick.py", *args],
        capture_output=True, text=True, cwd="/home/claude",
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    db = Path("/home/claude/data/golf.db")
    if db.exists():
        db.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)

    pool = PoolConfig(season=2026, entry_count=50, weekly_skins_contribution=50.0)

    print("=" * 72)
    print(" WEEKLY LOOP INTEGRATION TEST")
    print("=" * 72)

    # ── Setup: seed ──────────────────────────────────────────────────────
    print("\n[setup] Seeding crosswalk...")
    seed(db)

    # Helper: resolve a mock leaderboard through the seeded crosswalk
    # (mimics what the real ESPN fetcher does)
    def hydrate(tournament):
        xw = PlayerCrosswalk(db)
        for row in tournament.leaderboard:
            r = xw.resolve(row.raw_name, source="espn",
                           source_id=row.espn_athlete_id)
            row.canonical_id = r.canonical_id
            row.crosswalk_method = r.method
            row.crosswalk_confidence = r.confidence
        xw.close()

    # ── Week 1 — MONDAY: Sony Open opens ────────────────────────────────
    print("\n[Week 1 / Monday] Opening Sony Open...")
    sony = _build_mock_sony_open()
    # Sony mock is "post" status; make a fresh "scheduled" version for monday_open
    from copy import deepcopy
    sony_scheduled = deepcopy(sony)
    sony_scheduled.status = "scheduled"
    sony_scheduled.is_completed = False
    hydrate(sony_scheduled)
    result = run_monday_open(db_path=db, pool=pool,
                             mock_tournament=sony_scheduled)
    assert result.event_name == "Sony Open in Hawaii"
    assert len(result.burned_in_field) == 0, "Nobody burned yet, it's week 1"
    print(f"  ✓ {len(result.available_in_field)} available, "
          f"{len(result.unresolved_in_field)} unresolved")

    # ── Week 1 — THURSDAY: lock pick via CLI ────────────────────────────
    print("\n[Week 1 / Thursday] Locking pick: J.T. Poston...")
    rc, out, err = run_cli("--player", "Poston", "--confidence", "MED",
                            "--rationale", "Course-horse at Waialae",
                            "--yes")
    assert rc == 0, f"record_pick failed: rc={rc}\nstdout:\n{out}\nstderr:\n{err}"
    assert "✓ Pick recorded" in out
    print(f"  ✓ Pick locked via CLI")

    # ── Week 1 — SUNDAY: close the tournament ───────────────────────────
    print("\n[Week 1 / Sunday] Closing Sony Open (Scheffler wins, 2 in pool picked him)...")
    hydrate(sony)  # the "post" version with final scores
    close = run_sunday_close(db_path=db, pool=pool,
                             pool_winners_picked=2, mock_tournament=sony)
    assert close.pick_existed
    assert close.pick_player_name == "J.T. Poston"
    assert close.pick_position == "T3"
    assert close.pick_earnings == 458200
    assert close.skins_payout == 25.0  # $50 / 2
    assert not close.skins_rolled_over
    print(f"  ✓ Poston T3, earned $458,200")
    print(f"  ✓ Skins: $50 pot, 2 winners, $25 each")

    # ── Week 2 — MONDAY: AmEx opens, Poston should be burned ────────────
    print("\n[Week 2 / Monday] Opening American Express...")
    amex = _build_mock_amex_field()
    hydrate(amex)
    open2 = run_monday_open(db_path=db, pool=pool, mock_tournament=amex)
    burned_names = {fp.raw_name for fp in open2.burned_in_field}
    assert "J.T. Poston" in burned_names, \
        f"Poston should be burned; burned set was {burned_names}"
    print(f"  ✓ Poston correctly flagged as burned: {burned_names}")
    print(f"  ✓ {len(open2.available_in_field)} still available in field")
    print(f"  ✓ {len(open2.unresolved_in_field)} sponsor invites queued for review")

    # ── Week 2 — THURSDAY: try to re-pick Poston (should fail) ──────────
    print("\n[Week 2 / Thursday] Attempting to re-pick Poston (should fail)...")
    rc, out, err = run_cli("--player", "Poston", "--yes")
    assert rc == 2, f"Expected rc=2 (burned), got rc={rc}\nstdout:\n{out}\nstderr:\n{err}"
    assert "already used this season" in err
    print(f"  ✓ CLI refused: 'already used this season at Sony Open'")

    # ── Week 2 — THURSDAY: pick a fresh player ──────────────────────────
    print("\n[Week 2 / Thursday] Locking fresh pick: Jordan Spieth...")
    rc, out, err = run_cli("--player", "spieth", "--confidence", "HIGH",
                            "--rationale", "Pete Dye fit, good recent form",
                            "--yes")
    assert rc == 0, f"record_pick failed: rc={rc}\nstdout:\n{out}\nstderr:\n{err}"
    print(f"  ✓ Spieth pick locked")

    # ── Week 2 — THURSDAY: try a 2nd pick same event (should fail) ──────
    print("\n[Week 2 / Thursday] Attempting a second pick for same event...")
    rc, out, err = run_cli("--player", "Scheffler", "--yes")
    assert rc == 3, f"Expected rc=3 (duplicate event pick), got rc={rc}"
    assert "pick already exists" in err
    print(f"  ✓ CLI refused: 'a pick already exists for The American Express'")

    # ── Week 2 — THURSDAY: replace works ────────────────────────────────
    print("\n[Week 2 / Thursday] --replace should allow swap...")
    rc, out, err = run_cli("--player", "Scheffler", "--replace", "--yes")
    assert rc == 0, f"Expected rc=0 with --replace, got rc={rc}\nstderr:{err}"
    print(f"  ✓ Replace succeeded; pick is now Scheffler")

    # ── Ambiguous lookup should fail cleanly ────────────────────────────
    print("\n[smoke] Ambiguous lookup ('C. Young') should be rejected...")
    rc, out, err = run_cli("--player", "C. Young", "--yes")
    assert rc == 1, f"Expected rc=1 for ambiguous, got rc={rc}"
    assert "Couldn't resolve" in out or "Closest matches" in out
    print(f"  ✓ Ambiguous input rejected with candidate list")

    # ── Final ledger sanity ─────────────────────────────────────────────
    print("\n[final] Inspecting ledger state...")
    ledger = Ledger(db)
    summary = ledger.season_summary(2026)
    burned = ledger.burned_player_ids(2026)
    print(f"  Season earnings:   ${summary['total_earnings']:,.0f}")
    print(f"  Picks made:        {summary['picks_made']}")
    print(f"  Cuts made:         {summary['cuts_made']}")
    print(f"  Burned players:    {len(burned)}")
    print(f"  Events completed:  {summary['events_completed']}")
    assert summary["picks_made"] == 2  # Sony (Poston) + AmEx (Scheffler)
    assert summary["events_completed"] == 1  # only Sony has been closed
    assert summary["total_earnings"] == 458200.0
    assert len(burned) == 2  # Poston + Scheffler
    ledger.close()

    print()
    print("=" * 72)
    print(" ✓ ALL CHECKS PASSED")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
