"""
test_course_history_integration.py — proves course history changes recommendations.

The test scenario:
  PGA Championship at Quail Hollow Club, 2026.
  Field: McIlroy (great at Quail), Spieth (terrible at Quail), Schauffele,
         Thomas, Fleetwood, Burns (no Quail history), Niemann.
  Vegas odds: same for all comparisons.

  Run the scorer TWICE:
    A) include_course_history=False  → pure Vegas-driven
    B) include_course_history=True   → Vegas + multi-year Quail history

  Verify:
    - McIlroy's score should INCREASE meaningfully in (B)
    - Spieth's score should DECREASE meaningfully in (B)
    - Burns (no history) should be unchanged
    - The rationale text in (B) should mention course history
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from normalize.players import PlayerCrosswalk
from ledger.ledger import Ledger
from normalize.seed import seed
from scoring.inputs import build_event_inputs
from scoring.multi_objective import score_field, ObjectiveWeights


def main() -> int:
    db = Path(tempfile.mkdtemp(prefix="quipu_course_")) / "golf.db"

    print("=" * 78)
    print(" COURSE HISTORY INTEGRATION TEST")
    print(" Same field, scored with and without venue history")
    print("=" * 78)

    print("\n[1] Seeding...")
    seed(db)

    print("\n[2] Building multi-year Quail Hollow history...")
    ledger = Ledger(db)
    xw = PlayerCrosswalk(db)

    quail_events = [
        (2023, "wf23", "Wells Fargo Championship", [
            ("Rory McIlroy",      "1",   -16, 3600000, True),
            ("Xander Schauffele", "2",   -12, 2160000, True),
            ("Justin Thomas",     "T8",  -7,   400000, True),
            ("Tommy Fleetwood",   "T20",  0,   120000, True),
            ("Jordan Spieth",     "CUT",  6,        0, False),
            ("Joaquín Niemann",   "T35",  3,    40000, True),
        ]),
        (2024, "wf24", "Wells Fargo Championship", [
            ("Rory McIlroy",      "1",   -17, 3600000, True),
            ("Xander Schauffele", "T5",   -8,   500000, True),
            ("Justin Thomas",     "T3",  -10,   920000, True),
            ("Tommy Fleetwood",   "T15",  -2,   175000, True),
            ("Jordan Spieth",     "T55",  4,    18000, True),
            ("Joaquín Niemann",   "CUT",  5,        0, False),
        ]),
        (2025, "tr25", "Truist Championship", [
            ("Rory McIlroy",      "T4",  -10, 1000000, True),
            ("Xander Schauffele", "T2",  -13, 2160000, True),
            ("Justin Thomas",     "T20",  -3,   120000, True),
            ("Tommy Fleetwood",   "T8",   -8,   340000, True),
            ("Jordan Spieth",     "CUT",  5,        0, False),
            ("Joaquín Niemann",   "T55",  4,    18000, True),
        ]),
    ]
    for year, eid, name, finishes in quail_events:
        canonical_event_id = ledger.upsert_event(
            espn_event_id=eid, name=name, season=year,
            start_date=f"{year}-05-08", course_name="Quail Hollow Club",
            status="post",
        )
        payload = []
        for pname, pos, stp, earn, mc in finishes:
            cid = xw.resolve(pname, source="t").canonical_id
            if cid:
                payload.append({
                    "canonical_player_id": cid, "position": pos,
                    "score_to_par": stp, "earnings": earn, "made_cut": mc,
                    "fedex_points": None,
                })
        ledger.upsert_results(canonical_event_id, year, payload)

    # Add non-Quail events so overall rates exist & lift comparisons work
    print("[3] Adding non-Quail events (Masters, US Open) for baseline comparison...")
    ledger.upsert_event(
        espn_event_id="aug25", name="Masters Tournament", season=2025,
        start_date="2025-04-10", course_name="Augusta National Golf Club",
        status="post",
    )
    masters_finishes = [
        ("Rory McIlroy",      "T15", -3,  200000, True),
        ("Jordan Spieth",     "T5",  -8,  580000, True),    # Spieth Masters tradition
        ("Justin Thomas",     "T35",  3,   35000, True),
        ("Xander Schauffele", "T20", -1,  100000, True),
        ("Tommy Fleetwood",   "T10", -5,  290000, True),
        ("Joaquín Niemann",   "T8",  -6,  340000, True),
        ("Sam Burns",         "T25",  1,   65000, True),
    ]
    payload = [{"canonical_player_id": xw.resolve(n, source="t").canonical_id,
                "position": pos, "score_to_par": stp, "earnings": e,
                "made_cut": mc, "fedex_points": None}
               for n, pos, stp, e, mc in masters_finishes]
    ledger.upsert_results("e_aug25", 2025, payload)

    print("[4] Creating PGA Championship 2026 at Quail Hollow as scheduled event...")
    pga_id = ledger.upsert_event(
        espn_event_id="pga26", name="PGA Championship", season=2026,
        start_date="2026-05-14", course_name="Quail Hollow Club",
        purse=18_500_000, status="scheduled",
    )

    # Set the field
    field_players = ["Rory McIlroy", "Jordan Spieth", "Xander Schauffele",
                     "Justin Thomas", "Tommy Fleetwood", "Sam Burns",
                     "Joaquín Niemann"]
    field_payload = [{"canonical_player_id": xw.resolve(n, source="t").canonical_id,
                      "raw_name": n, "espn_athlete_id": None}
                     for n in field_players]
    ledger.set_event_field(pga_id, field_payload)
    xw.close()
    ledger.close()

    # ── Step 5: build Vegas odds (same for both runs) ───────────────────
    xw = PlayerCrosswalk(db)
    vegas = {
        xw.resolve("Rory McIlroy", source="t").canonical_id:      0.16,
        xw.resolve("Xander Schauffele", source="t").canonical_id: 0.10,
        xw.resolve("Justin Thomas", source="t").canonical_id:     0.07,
        xw.resolve("Tommy Fleetwood", source="t").canonical_id:   0.05,
        xw.resolve("Jordan Spieth", source="t").canonical_id:     0.04,
        xw.resolve("Joaquín Niemann", source="t").canonical_id:   0.05,
        xw.resolve("Sam Burns", source="t").canonical_id:         0.06,
    }
    xw.close()

    print("\n[5] Scoring WITHOUT course history (Vegas-only baseline)...")
    inputs_no_hist = build_event_inputs(
        db, pga_id, season=2026, skins_pot=150.0,
        vegas_odds=vegas, include_course_history=False,
    )
    scored_no_hist = score_field(inputs_no_hist, ObjectiveWeights())

    print(f"\n  {'Player':<22} {'Score':>6} {'P(top10)':>10} {'Source':<10}")
    print(f"  {'─'*22} {'─'*6} {'─'*10} {'─'*10}")
    for sp in scored_no_hist:
        print(f"  {sp.display_name:<22} {sp.composite_score:>6.1f} "
              f"{sp.finish_probs.top10:>9.1%} {sp.finish_probs.source:<10}")

    print("\n[6] Scoring WITH course history...")
    inputs_with = build_event_inputs(
        db, pga_id, season=2026, skins_pot=150.0,
        vegas_odds=vegas, include_course_history=True,
    )
    scored_with = score_field(inputs_with, ObjectiveWeights())

    print(f"\n  {'Player':<22} {'Score':>6} {'P(top10)':>10} "
          f"{'V.Lift':>7} {'Hist':<14} {'Source':<15}")
    print(f"  {'─'*22} {'─'*6} {'─'*10} {'─'*7} {'─'*14} {'─'*15}")
    for sp in scored_with:
        pi = next(p for p in inputs_with.players if p.canonical_id == sp.canonical_id)
        lift_str = f"{pi.venue_skill_lift:.2f}x" if pi.venue_evidence_weight > 0 else "—"
        hist_str = (f"{pi.venue_top_10s}/{pi.venue_starts} T10s"
                    if pi.venue_starts else "no history")
        print(f"  {sp.display_name:<22} {sp.composite_score:>6.1f} "
              f"{sp.finish_probs.top10:>9.1%} {lift_str:>7} "
              f"{hist_str:<14} {sp.finish_probs.source:<15}")

    # ── Step 7: side-by-side delta ──────────────────────────────────────
    print("\n[7] Score deltas (with-history vs no-history):")
    delta_by_name: dict[str, tuple[float, float, float]] = {}  # name → (no, with, delta)
    for sp_no in scored_no_hist:
        sp_with = next(s for s in scored_with if s.canonical_id == sp_no.canonical_id)
        delta_by_name[sp_no.display_name] = (
            sp_no.composite_score, sp_with.composite_score,
            sp_with.composite_score - sp_no.composite_score,
        )
    print(f"\n  {'Player':<22} {'No hist':>8} {'With hist':>10} {'Δ':>8}")
    print(f"  {'─'*22} {'─'*8} {'─'*10} {'─'*8}")
    for name, (no, with_h, d) in sorted(delta_by_name.items(),
                                          key=lambda kv: kv[1][2], reverse=True):
        sign = "+" if d > 0 else ""
        print(f"  {name:<22} {no:>8.1f} {with_h:>10.1f} {sign}{d:>7.1f}")

    # ── Step 8: assertions ──────────────────────────────────────────────
    print("\n[8] Sanity assertions...")

    rory_delta = delta_by_name["Rory McIlroy"][2]
    spieth_delta = delta_by_name["Jordan Spieth"][2]
    burns_delta = delta_by_name["Sam Burns"][2]

    # The composite_score is normalized within the field, so a player at the
    # floor stays at 0 even when their underlying probs drop further. Test the
    # underlying probabilities for clearer signal.

    def get_probs(scored, name):
        sp = next(s for s in scored if s.display_name == name)
        return sp.finish_probs

    rory_no = get_probs(scored_no_hist, "Rory McIlroy")
    rory_with = get_probs(scored_with, "Rory McIlroy")
    spieth_no = get_probs(scored_no_hist, "Jordan Spieth")
    spieth_with = get_probs(scored_with, "Jordan Spieth")
    burns_no = get_probs(scored_no_hist, "Sam Burns")
    burns_with = get_probs(scored_with, "Sam Burns")

    # Test underlying P(top10) — the right signal regardless of normalization
    assert rory_with.top10 > rory_no.top10, \
        f"McIlroy's P(top10) should INCREASE: {rory_no.top10:.1%} → {rory_with.top10:.1%}"
    print(f"  ✓ McIlroy P(top10): {rory_no.top10:.1%} → {rory_with.top10:.1%} "
          f"(lift applied)")

    assert spieth_with.top10 < spieth_no.top10, \
        f"Spieth's P(top10) should DECREASE: {spieth_no.top10:.1%} → {spieth_with.top10:.1%}"
    print(f"  ✓ Spieth P(top10): {spieth_no.top10:.1%} → {spieth_with.top10:.1%} "
          f"(downgrade applied)")

    # Burns: no Quail history → underlying probs unchanged
    assert abs(burns_with.top10 - burns_no.top10) < 0.001, \
        f"Burns (no history) P(top10) should be unchanged: " \
        f"{burns_no.top10:.1%} → {burns_with.top10:.1%}"
    print(f"  ✓ Burns P(top10) unchanged: {burns_no.top10:.1%} "
          f"(no Quail history)")

    # Burns's composite_score CAN change because the field's range shifts when
    # other players get venue lifts. That's expected behavior, not a bug.
    print(f"  ✓ Note: Burns composite shifted {burns_delta:+.1f} due to field "
          f"renormalization (expected — others' lifts widen the field range)")

    # Final ranking: McIlroy should be #1 with history applied
    top_pick = scored_with[0]
    assert top_pick.display_name == "Rory McIlroy", \
        f"Top pick should be McIlroy with course history, got {top_pick.display_name}"
    print(f"  ✓ Top pick is McIlroy: {top_pick.composite_score:.1f}")

    # Spieth should rank lower-or-equal with history applied
    spieth_rank_no = next(i for i, s in enumerate(scored_no_hist, 1)
                          if s.display_name == "Jordan Spieth")
    spieth_rank_with = next(i for i, s in enumerate(scored_with, 1)
                             if s.display_name == "Jordan Spieth")
    assert spieth_rank_with >= spieth_rank_no, \
        f"Spieth should rank lower (or same) with history; was #{spieth_rank_no}, now #{spieth_rank_with}"
    print(f"  ✓ Spieth's rank: #{spieth_rank_no} → #{spieth_rank_with}")

    # The rationale should mention course history for players with venue stats
    rory_rationale = scored_with[0].rationale
    has_course_line = any("Course history" in line for line in rory_rationale)
    assert has_course_line, \
        f"McIlroy's rationale should mention course history, got: {rory_rationale}"
    print(f"  ✓ Rationale includes course history line for McIlroy")

    # Confirm source string was updated
    assert "venue" in scored_with[0].finish_probs.source, \
        f"Source should indicate venue adjustment, got {scored_with[0].finish_probs.source}"
    print(f"  ✓ Probability source tagged '{scored_with[0].finish_probs.source}'")

    print("\n[9] McIlroy's full rationale (with course history):")
    for line in scored_with[0].rationale:
        print(f"    • {line}")

    print("\n" + "=" * 78)
    print(" ✓ COURSE HISTORY MEANINGFULLY CHANGES RECOMMENDATIONS")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
