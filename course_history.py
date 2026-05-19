"""
course_history.py — compute per-player per-venue stats from season_results.

Given a course_name and an optional player_id, aggregates that player's
performance at that course across all seasons in the DB. Designed to plug
into the scorer's PlayerInputs.

Statistical care: we treat venue history as evidence that *shifts* the
player's baseline, not as a standalone estimate. A single great finish at
Pebble doesn't make someone a Pebble specialist — but five consistent T20s
does. The shrinkage parameters below codify that.

Key concepts:

  player_venue_rate    = player's top-10 rate at THIS course
  player_overall_rate  = player's top-10 rate ACROSS ALL events
  field_baseline       = ~14% (top 10 of ~70-156 player field ≈ 7-14%)
  venue_skill_lift     = shrinkage-adjusted ratio: how much better the
                         player is at this venue vs their overall game

The output `venue_skill_lift` of 1.5 means "this player is 50% more likely
to top-10 here than at a random event." Plug it into the scorer as a
multiplicative bump on baseline probabilities.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ledger.ledger import Ledger


# ─────────────────────────────────────────────────────────────────────────────
# Tuning constants
# ─────────────────────────────────────────────────────────────────────────────

# Shrinkage strength: how many "pseudo-starts" of baseline we blend in before
# trusting the player's own venue rate. Higher = more conservative shrinkage.
# 4 is reasonable for PGA-level data — equivalent to saying "the baseline counts
# as 4 starts of evidence; the player's actual record overrides only with
# enough sample to outweigh that."
SHRINKAGE_ALPHA = 4.0

# Field-baseline top-10 rate. A 70-player field has ~14% top-10 rate by
# definition (10/70). For 156-player fields it's ~6%. We use 12% as a
# weighted average across event sizes.
BASELINE_TOP10_RATE = 0.12
BASELINE_TOP20_RATE = 0.20
BASELINE_CUT_RATE = 0.70

# Cap the venue_skill_lift to prevent extreme values from noisy samples.
# 0.5 means "no player gets more than 2x lift from venue history alone"
LIFT_CAP_LOW = 0.5
LIFT_CAP_HIGH = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Output record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CourseHistory:
    """A player's aggregated history at one course."""
    canonical_player_id: str
    course_name: str
    starts: int
    made_cuts: int
    top_10s: int
    top_20s: int
    wins: int
    best_finish: Optional[int]                  # numeric position, lower=better
    avg_finish: Optional[float]                 # mean position when made cut
    total_earnings: float
    # Derived rates (raw, before shrinkage):
    top10_rate_raw: float
    top20_rate_raw: float
    cut_rate_raw: float
    # Shrinkage-adjusted rates (what to feed the scorer):
    top10_rate_shrunk: float
    top20_rate_shrunk: float
    cut_rate_shrunk: float
    # Multiplicative skill lift vs player's overall game.
    # >1.0 = better here than average; <1.0 = worse.
    venue_skill_lift: float
    # Number of "evidence units" — informs the scorer how seriously to weight
    # this signal. Roughly = starts (capped reasonably).
    evidence_weight: float


@dataclass
class CourseHistoryUnavailable:
    """Sentinel — no history available for this player at this course."""
    canonical_player_id: str
    course_name: str
    reason: str  # 'no_starts' | 'no_course_match'


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────

def get_course_history(
    db_path: str | Path,
    canonical_player_id: str,
    course_name: str,
    *,
    exclude_event_id: Optional[str] = None,
    as_of_date: Optional[str] = None,
) -> CourseHistory | CourseHistoryUnavailable:
    """
    Compute course history for one player at one course.

    Excludes `exclude_event_id` — useful for the scorer to avoid leaking
    info about the current event's results into the prediction for that
    same event (during in-progress queries; less relevant pre-tournament).

    When `as_of_date` is set, only include events with start_date strictly
    before that date. Used by the backtester to prevent leakage of future
    venue history into past predictions.
    """
    ledger = Ledger(db_path)
    try:
        rows = ledger.conn.execute(
            """
            SELECT r.position, r.score_to_par, r.earnings, r.made_cut,
                   e.canonical_event_id, e.name, e.start_date
            FROM season_results r
            JOIN events e ON e.canonical_event_id = r.canonical_event_id
            WHERE r.canonical_player_id = ?
              AND e.course_name = ?
              AND (? IS NULL OR e.canonical_event_id != ?)
              AND (? IS NULL OR e.start_date IS NULL
                   OR substr(e.start_date, 1, 10) < ?)
            ORDER BY e.start_date
            """,
            (canonical_player_id, course_name, exclude_event_id, exclude_event_id,
             as_of_date, as_of_date[:10] if as_of_date else None),
        ).fetchall()

        if not rows:
            return CourseHistoryUnavailable(
                canonical_player_id=canonical_player_id,
                course_name=course_name,
                reason="no_starts",
            )

        # Compute raw aggregates
        starts = len(rows)
        made_cuts = sum(1 for r in rows if r["made_cut"] == 1)
        top_10s = 0
        top_20s = 0
        wins = 0
        positions: list[int] = []
        total_earnings = 0.0
        for r in rows:
            if r["earnings"]:
                total_earnings += r["earnings"]
            pos_str = (r["position"] or "").lstrip("T")
            try:
                pos = int(pos_str)
                positions.append(pos)
                if pos == 1:
                    wins += 1
                if pos <= 10:
                    top_10s += 1
                if pos <= 20:
                    top_20s += 1
            except (ValueError, TypeError):
                pass

        # Raw rates
        top10_raw = top_10s / starts if starts else 0.0
        top20_raw = top_20s / starts if starts else 0.0
        cut_raw = made_cuts / starts if starts else 0.0

        # Shrinkage toward baseline (Bayesian credibility-weighted average)
        def shrink(raw_rate: float, baseline: float, n: int) -> float:
            return (SHRINKAGE_ALPHA * baseline + n * raw_rate) / (SHRINKAGE_ALPHA + n)

        top10_shrunk = shrink(top10_raw, BASELINE_TOP10_RATE, starts)
        top20_shrunk = shrink(top20_raw, BASELINE_TOP20_RATE, starts)
        cut_shrunk = shrink(cut_raw, BASELINE_CUT_RATE, starts)

        # Compute the player's rates at OTHER courses (excluding this one) for
        # comparison. We use this to derive the skill lift.
        # CRITICAL: must exclude THIS course's rows, otherwise a venue
        # specialist's "overall" rate gets inflated by their own venue dominance
        # and the lift paradoxically becomes < 1.
        overall = _player_overall_rates(ledger, canonical_player_id,
                                         exclude_course=course_name)
        if overall["starts"] > 0 and overall["top10_rate"] > 0:
            # Lift = how much better here than at other courses
            lift = top10_shrunk / overall["top10_rate"]
        elif overall["starts"] > 0 and overall["top10_rate"] == 0:
            # Player has data at other courses but never top-10'd there.
            # If they top-10 here, that's a strong positive lift.
            lift = LIFT_CAP_HIGH if top10_shrunk > BASELINE_TOP10_RATE else 1.0
        else:
            # No data at other courses → can't compare; assume neutral
            lift = 1.0
        lift = max(LIFT_CAP_LOW, min(LIFT_CAP_HIGH, lift))

        # Evidence weight: starts, capped at 8 (diminishing returns past that)
        evidence = min(float(starts), 8.0)

        return CourseHistory(
            canonical_player_id=canonical_player_id,
            course_name=course_name,
            starts=starts,
            made_cuts=made_cuts,
            top_10s=top_10s,
            top_20s=top_20s,
            wins=wins,
            best_finish=min(positions) if positions else None,
            avg_finish=(sum(positions) / len(positions)) if positions else None,
            total_earnings=total_earnings,
            top10_rate_raw=top10_raw,
            top20_rate_raw=top20_raw,
            cut_rate_raw=cut_raw,
            top10_rate_shrunk=top10_shrunk,
            top20_rate_shrunk=top20_shrunk,
            cut_rate_shrunk=cut_shrunk,
            venue_skill_lift=lift,
            evidence_weight=evidence,
        )
    finally:
        ledger.close()


def _player_overall_rates(
    ledger: Ledger,
    canonical_player_id: str,
    *,
    exclude_course: Optional[str] = None,
) -> dict:
    """A player's top-10/top-20/cut rate across every event we have. If
    exclude_course is set, excludes events at that course (used when computing
    venue-skill lift to avoid contamination)."""
    if exclude_course:
        rows = ledger.conn.execute(
            """
            SELECT r.position, r.made_cut
            FROM season_results r
            JOIN events e ON e.canonical_event_id = r.canonical_event_id
            WHERE r.canonical_player_id = ?
              AND (e.course_name IS NULL OR e.course_name != ?)
            """,
            (canonical_player_id, exclude_course),
        ).fetchall()
    else:
        rows = ledger.conn.execute(
            """
            SELECT position, made_cut FROM season_results
            WHERE canonical_player_id = ?
            """,
            (canonical_player_id,),
        ).fetchall()
    if not rows:
        return {"starts": 0, "top10_rate": 0.0, "top20_rate": 0.0, "cut_rate": 0.0}
    starts = len(rows)
    made_cuts = sum(1 for r in rows if r["made_cut"] == 1)
    top10 = 0
    top20 = 0
    for r in rows:
        pos_str = (r["position"] or "").lstrip("T")
        try:
            pos = int(pos_str)
            if pos <= 10:
                top10 += 1
            if pos <= 20:
                top20 += 1
        except (ValueError, TypeError):
            pass
    return {
        "starts": starts,
        "top10_rate": top10 / starts,
        "top20_rate": top20 / starts,
        "cut_rate": made_cuts / starts,
    }


def get_course_history_batch(
    db_path: str | Path,
    canonical_player_ids: list[str],
    course_name: str,
    *,
    exclude_event_id: Optional[str] = None,
) -> dict[str, CourseHistory | CourseHistoryUnavailable]:
    """Batch version. Returns {player_id: CourseHistory} for every input."""
    return {
        pid: get_course_history(db_path, pid, course_name,
                                 exclude_event_id=exclude_event_id)
        for pid in canonical_player_ids
    }


# ─────────────────────────────────────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────────────────────────────────────

def _demo(db_path: Path) -> None:
    """Build mock multi-year history and verify the aggregator produces sane stats."""
    from normalize.seed import seed
    from normalize.players import PlayerCrosswalk

    print("=" * 78)
    print(" COURSE HISTORY — Offline Demo")
    print("=" * 78)

    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print("\n[1] Seeding...")
    seed(db_path)

    ledger = Ledger(db_path)
    xw = PlayerCrosswalk(db_path)

    # Create 4 years of mock data at Quail Hollow Club
    # Two player archetypes:
    #   - Strong-at-Quail player (McIlroy): wins/T5s repeatedly
    #   - Average player (some random player): middling finishes
    #   - Weak-at-Quail player: misses cuts repeatedly

    print("\n[2] Building 4 years of mock Quail Hollow events...")
    mock_history = [
        # (year, espn_id, name, players_finishes)
        # players_finishes: list of (player_name, position, score_to_par, earnings, made_cut)
        (2023, "401437", "Wells Fargo Championship", [
            ("Rory McIlroy",          "1",   -16, 3600000, True),
            ("Xander Schauffele",     "2",   -12, 2160000, True),
            ("Patrick Cantlay",       "T5",  -7,   500000, True),
            ("Tommy Fleetwood",       "T35", 2,    35000,  True),
            ("Jordan Spieth",         "CUT", 6,    0,      False),
            ("Justin Thomas",         "T15", -2,   150000, True),
            ("Collin Morikawa",       "T25", 1,    65000,  True),
        ]),
        (2024, "401580", "Wells Fargo Championship", [
            ("Rory McIlroy",          "1",   -17, 3600000, True),  # back-to-back
            ("Justin Thomas",         "T3",  -10, 920000,  True),
            ("Xander Schauffele",     "T8",  -7,  340000,  True),
            ("Tommy Fleetwood",       "T20", 0,   120000,  True),
            ("Jordan Spieth",         "T55", 4,   18000,   True),
            ("Patrick Cantlay",       "T10", -5,  290000,  True),
            ("Collin Morikawa",       "CUT", 5,   0,       False),
        ]),
        (2025, "401581", "Truist Championship", [
            ("Rory McIlroy",          "T4",  -10, 1000000, True),
            ("Xander Schauffele",     "T2",  -13, 2160000, True),
            ("Justin Thomas",         "T15", -3,  175000,  True),
            ("Tommy Fleetwood",       "T8",  -8,  340000,  True),
            ("Jordan Spieth",         "CUT", 5,   0,       False),
            ("Patrick Cantlay",       "T30", 2,   50000,   True),
        ]),
        (2026, "401582", "Truist Championship", [
            ("Ludvig Åberg",          "T8",  -10, 600000,  True),
            ("Jordan Spieth",         "CUT", 7,   0,       False),
            ("Tommy Fleetwood",       "T15", -4,  175000,  True),
        ]),
    ]

    for year, eid, name, finishes in mock_history:
        ledger.upsert_event(
            espn_event_id=eid, name=name, season=year,
            start_date=f"{year}-05-08", course_name="Quail Hollow Club",
            status="post",
        )
        canonical_event_id = f"e_{eid}"
        payload = []
        for player_name, pos, stp, earnings, made_cut in finishes:
            cid = xw.resolve(player_name, source="t").canonical_id
            if cid is None:
                continue
            payload.append({
                "canonical_player_id": cid,
                "position": pos,
                "score_to_par": stp,
                "earnings": earnings,
                "made_cut": made_cut,
                "fedex_points": None,
            })
        ledger.upsert_results(canonical_event_id, year, payload)

    # Also add some non-Quail events for the same players so overall rates aren't zero
    print("[3] Adding non-Quail events so 'overall' rates exist...")
    ledger.upsert_event(
        espn_event_id="401590", name="Masters Tournament", season=2025,
        start_date="2025-04-10", course_name="Augusta National Golf Club",
        status="post",
    )
    non_quail = [
        ("Rory McIlroy",          "T15", -3, 200000, True),
        ("Jordan Spieth",         "T5",  -8, 580000, True),
        ("Justin Thomas",         "CUT", 6, 0, False),
        ("Xander Schauffele",     "T35", 3, 35000, True),
        ("Tommy Fleetwood",       "T10", -5, 290000, True),
        ("Patrick Cantlay",       "T20", 0, 120000, True),
        ("Collin Morikawa",       "T8",  -7, 340000, True),
    ]
    payload = []
    for name, pos, stp, earn, mc in non_quail:
        cid = xw.resolve(name, source="t").canonical_id
        payload.append({"canonical_player_id": cid, "position": pos,
                         "score_to_par": stp, "earnings": earn, "made_cut": mc,
                         "fedex_points": None})
    ledger.upsert_results("e_401590", 2025, payload)

    ledger.close()

    # ── Query and verify ────────────────────────────────────────────────
    print("\n[4] Querying course history at Quail Hollow...")
    target_players = [
        "Rory McIlroy", "Jordan Spieth", "Xander Schauffele",
        "Justin Thomas", "Tommy Fleetwood", "Patrick Cantlay",
        "Collin Morikawa", "Ludvig Åberg",
    ]

    print(f"\n  {'Player':<22} {'Starts':>6} {'T10':>4} {'Cuts':>5} "
          f"{'T10 raw':>9} {'T10 adj':>9} {'Lift':>6}")
    print(f"  {'─'*22} {'─'*6} {'─'*4} {'─'*5} {'─'*9} {'─'*9} {'─'*6}")
    for name in target_players:
        cid = xw.resolve(name, source="t").canonical_id
        h = get_course_history(db_path, cid, "Quail Hollow Club")
        if isinstance(h, CourseHistoryUnavailable):
            print(f"  {name:<22}  (no history)")
            continue
        print(f"  {name:<22} {h.starts:>6} {h.top_10s:>4} {h.made_cuts:>5} "
              f"{h.top10_rate_raw:>8.1%} {h.top10_rate_shrunk:>8.1%} "
              f"{h.venue_skill_lift:>5.2f}x")

    xw.close()

    print("\n[5] Sanity assertions...")
    xw = PlayerCrosswalk(db_path)

    # McIlroy at Quail Hollow: 3 starts (2023/24/25), 2 wins + T4 — excellent
    rory_cid = xw.resolve("Rory McIlroy", source="t").canonical_id
    rory_quail = get_course_history(db_path, rory_cid, "Quail Hollow Club")
    assert isinstance(rory_quail, CourseHistory)
    assert rory_quail.starts == 3, f"Rory should have 3 Quail starts (2023, 2024, 2025), got {rory_quail.starts}"
    assert rory_quail.wins == 2, f"Rory should have 2 Quail wins, got {rory_quail.wins}"
    # His shrunken rate should be well above the 12% baseline
    assert rory_quail.top10_rate_shrunk > 0.40, \
        f"Rory's Quail top-10 rate should be high, got {rory_quail.top10_rate_shrunk}"
    assert rory_quail.venue_skill_lift > 1.0, \
        f"Rory should have positive Quail lift, got {rory_quail.venue_skill_lift}"
    print(f"  ✓ McIlroy: {rory_quail.starts} starts, {rory_quail.wins} wins, "
          f"shrunk T10 rate {rory_quail.top10_rate_shrunk:.1%}, "
          f"lift {rory_quail.venue_skill_lift:.2f}x")

    # Spieth at Quail Hollow: 4 starts, only 1 made cut. Bad venue for him.
    spieth_cid = xw.resolve("Jordan Spieth", source="t").canonical_id
    spieth_quail = get_course_history(db_path, spieth_cid, "Quail Hollow Club")
    assert isinstance(spieth_quail, CourseHistory)
    assert spieth_quail.starts == 4, \
        f"Spieth should have 4 Quail starts, got {spieth_quail.starts}"
    assert spieth_quail.made_cuts == 1, \
        f"Spieth should have 1 cut made at Quail, got {spieth_quail.made_cuts}"
    assert spieth_quail.top_10s == 0, \
        f"Spieth should have 0 T10s at Quail, got {spieth_quail.top_10s}"
    # Lift should be < 1 (he's worse here than at other courses where he T5'd Augusta)
    assert spieth_quail.venue_skill_lift < 1.0, \
        f"Spieth's Quail lift should be <1.0, got {spieth_quail.venue_skill_lift}"
    print(f"  ✓ Spieth: {spieth_quail.starts} starts, "
          f"{spieth_quail.made_cuts}/4 cuts, 0 T10s, "
          f"lift {spieth_quail.venue_skill_lift:.2f}x (bad venue for him)")

    # Åberg: 1 start (small sample) — shrinkage should dominate
    aberg_cid = xw.resolve("Ludvig Åberg", source="t").canonical_id
    aberg_quail = get_course_history(db_path, aberg_cid, "Quail Hollow Club")
    assert isinstance(aberg_quail, CourseHistory)
    assert aberg_quail.starts == 1
    # Even though his only Quail finish is T8 (top 10!), shrinkage should
    # pull his estimate strongly toward the baseline because n=1
    assert aberg_quail.top10_rate_shrunk < 0.40, \
        f"Åberg's shrunk rate should be pulled toward baseline with n=1, " \
        f"got {aberg_quail.top10_rate_shrunk}"
    print(f"  ✓ Åberg (small sample): raw T10 rate {aberg_quail.top10_rate_raw:.1%}, "
          f"shrunk to {aberg_quail.top10_rate_shrunk:.1%}")

    # No history case
    burns_cid = xw.resolve("Sam Burns", source="t").canonical_id
    burns_quail = get_course_history(db_path, burns_cid, "Quail Hollow Club")
    assert isinstance(burns_quail, CourseHistoryUnavailable)
    print(f"  ✓ Sam Burns (no Quail history): correctly returns unavailable sentinel")

    xw.close()

    print("\n" + "=" * 78)
    print(" ✓ COURSE HISTORY WORKING")
    print("=" * 78)


def _print_history(h) -> None:
    if isinstance(h, CourseHistoryUnavailable):
        print(f"No course history: {h.reason}")
        print(f"  player:  {h.canonical_player_id}")
        print(f"  course:  {h.course_name}")
        return
    print(f"  Player:           {h.canonical_player_id}")
    print(f"  Course:           {h.course_name}")
    print(f"  Starts:           {h.starts}")
    print(f"  Cuts made:        {h.made_cuts}  ({h.cut_rate_raw:.1%} raw, "
          f"{h.cut_rate_shrunk:.1%} shrunk)")
    print(f"  Top 10s:          {h.top_10s}  ({h.top10_rate_raw:.1%} raw, "
          f"{h.top10_rate_shrunk:.1%} shrunk)")
    print(f"  Top 20s:          {h.top_20s}  ({h.top20_rate_raw:.1%} raw, "
          f"{h.top20_rate_shrunk:.1%} shrunk)")
    print(f"  Wins:             {h.wins}")
    print(f"  Best finish:      {h.best_finish if h.best_finish is not None else '—'}")
    print(f"  Avg finish:       "
          f"{f'{h.avg_finish:.1f}' if h.avg_finish is not None else '—'}")
    print(f"  Total earnings:   ${h.total_earnings:,.0f}")
    print(f"  Venue skill lift: {h.venue_skill_lift:.2f}x  "
          f"(evidence weight {h.evidence_weight:.1f})")


def main() -> int:
    from normalize.players import PlayerCrosswalk, ensure_seeded
    parser = argparse.ArgumentParser(
        description="Per-player per-venue history aggregator."
    )
    parser.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    parser.add_argument("--player", help="Player name (partial, case-insensitive OK)")
    parser.add_argument("--course",
                        help="Course name (exact match against events.course_name)")
    parser.add_argument("--exclude-event-id",
                        help="Skip this event_id when aggregating (prevents leakage "
                             "when scoring an in-progress event at the same venue)")
    parser.add_argument("--demo", action="store_true",
                        help="Run offline self-test against mock multi-year data")
    args = parser.parse_args()

    if args.demo:
        _demo(Path(args.db))
        return 0

    if not args.player or not args.course:
        parser.error("--player and --course are both required (or use --demo).")

    xw = PlayerCrosswalk(args.db)
    ensure_seeded(xw)
    r = xw.resolve(args.player, source="cli")
    if r.canonical_id is None:
        print(f"Could not resolve player '{args.player}'.", file=sys.stderr)
        if r.candidates:
            print("Closest matches:", file=sys.stderr)
            for c in r.candidates[:3]:
                print(f"  • {c.display_name}  (score {c.score:.2f})", file=sys.stderr)
        xw.close()
        return 1
    xw.close()

    h = get_course_history(
        args.db, r.canonical_id, args.course,
        exclude_event_id=args.exclude_event_id,
    )
    _print_history(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
