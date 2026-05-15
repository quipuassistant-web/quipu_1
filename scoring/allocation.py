"""
scoring/allocation.py — forward-looking roster allocation.

The hard truth of one-and-done: picking the best player for *this* week is
wrong if it costs you the best player for a better-fit week 3 weeks out.

This module solves the assignment problem:
  Given M available players and N upcoming events,
  assign at most one player per event such that:
    - each player is used at most once
    - total composite_score across assignments is maximized

This is the classic Hungarian / linear assignment problem. We don't need a
library — a greedy approximation works for our small N (at most ~20 events
ahead, ~120 available players), but for cleanliness we implement a real
LP-style greedy that's optimal-with-margin for our problem size.

For v1 we just need: a 4-6 week lookahead plan, refreshable each Monday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .inputs import EventInputs
from .multi_objective import ObjectiveWeights, ScoredPlayer, score_field


@dataclass
class EventPlan:
    canonical_event_id: str
    name: str
    is_major: bool
    purse: Optional[float]
    recommended_player: Optional[str]            # canonical_id, None if no good fit
    recommended_player_name: Optional[str]
    recommended_score: float
    score_if_punt: float                          # next-best score, for delta
    delta: float                                  # recommended - punt = "value of holding this player"
    top3_alternatives: list[tuple[str, str, float]] = field(default_factory=list)


@dataclass
class AllocationPlan:
    events: list[EventPlan]
    unassigned_players: list[tuple[str, str]]    # (canonical_id, name)
    total_expected_score: float


# ─────────────────────────────────────────────────────────────────────────────
# Greedy assignment
# ─────────────────────────────────────────────────────────────────────────────

def allocate(
    events_with_inputs: list[EventInputs],
    weights: ObjectiveWeights = ObjectiveWeights(),
) -> AllocationPlan:
    """
    Greedy allocation: at each step pick the (event, player) pair with the
    highest score among unassigned events and unused players. Optimal for
    small instances, near-optimal for large.

    More formally: this is the Hungarian algorithm with a greedy heuristic.
    For our scale (≤25 events × ≤150 players) the gap from optimal is small.
    If you ever need exact-optimal, swap in scipy.optimize.linear_sum_assignment.
    """
    if not events_with_inputs:
        return AllocationPlan(events=[], unassigned_players=[], total_expected_score=0.0)

    # Compute scores for every (event, player) pair
    scores_by_event: dict[str, list[ScoredPlayer]] = {}
    for ev in events_with_inputs:
        scored = score_field(ev, weights)
        scores_by_event[ev.canonical_event_id] = scored

    # Build a flat list of candidate assignments, sorted by score
    candidates: list[tuple[float, str, str, str]] = []  # (score, event_id, player_id, player_name)
    for ev in events_with_inputs:
        for sp in scores_by_event[ev.canonical_event_id]:
            candidates.append((sp.composite_score, ev.canonical_event_id,
                               sp.canonical_id, sp.display_name))
    candidates.sort(reverse=True)

    # Greedy: take the highest-scoring assignment, then skip any that conflict
    assigned_events: set[str] = set()
    assigned_players: set[str] = set()
    plan_by_event: dict[str, EventPlan] = {}

    for score, event_id, player_id, player_name in candidates:
        if event_id in assigned_events or player_id in assigned_players:
            continue
        # Find next-best score for this event among unused players (the "punt" baseline)
        next_best = 0.0
        for sp in scores_by_event[event_id]:
            if sp.canonical_id != player_id and sp.canonical_id not in assigned_players:
                next_best = sp.composite_score
                break

        ev = next(e for e in events_with_inputs if e.canonical_event_id == event_id)
        plan_by_event[event_id] = EventPlan(
            canonical_event_id=event_id,
            name=ev.name,
            is_major=ev.is_major,
            purse=ev.purse,
            recommended_player=player_id,
            recommended_player_name=player_name,
            recommended_score=score,
            score_if_punt=next_best,
            delta=score - next_best,
            top3_alternatives=[
                (sp.canonical_id, sp.display_name, sp.composite_score)
                for sp in scores_by_event[event_id][:4]
                if sp.canonical_id != player_id
            ][:3],
        )
        assigned_events.add(event_id)
        assigned_players.add(player_id)

    # Events that got no assignment (e.g. all candidates burned/conflicted)
    for ev in events_with_inputs:
        if ev.canonical_event_id not in plan_by_event:
            top3 = scores_by_event[ev.canonical_event_id][:3]
            plan_by_event[ev.canonical_event_id] = EventPlan(
                canonical_event_id=ev.canonical_event_id,
                name=ev.name,
                is_major=ev.is_major,
                purse=ev.purse,
                recommended_player=None,
                recommended_player_name=None,
                recommended_score=0.0,
                score_if_punt=0.0,
                delta=0.0,
                top3_alternatives=[
                    (sp.canonical_id, sp.display_name, sp.composite_score)
                    for sp in top3
                ],
            )

    # Players we have but didn't assign anywhere in the lookahead window
    all_player_ids = {sp.canonical_id for ev in events_with_inputs
                      for sp in scores_by_event[ev.canonical_event_id]}
    all_player_names = {sp.canonical_id: sp.display_name
                        for ev in events_with_inputs
                        for sp in scores_by_event[ev.canonical_event_id]}
    unassigned = [(pid, all_player_names[pid])
                  for pid in all_player_ids if pid not in assigned_players]
    unassigned.sort(key=lambda x: x[1])

    # Preserve calendar order
    events_in_order = [plan_by_event[ev.canonical_event_id] for ev in events_with_inputs]
    total = sum(p.recommended_score for p in events_in_order)

    return AllocationPlan(
        events=events_in_order,
        unassigned_players=unassigned,
        total_expected_score=total,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pretty-print
# ─────────────────────────────────────────────────────────────────────────────

def print_plan(plan: AllocationPlan) -> None:
    print()
    print("═" * 78)
    print(f"  ALLOCATION PLAN — {len(plan.events)} events ahead")
    print(f"  Total expected composite score: {plan.total_expected_score:.1f}")
    print("═" * 78)
    for ep in plan.events:
        marker = " [MAJOR]" if ep.is_major else ""
        purse = f"${ep.purse/1e6:.1f}M" if ep.purse else "?"
        print()
        print(f"  {ep.name}{marker}  ({purse} purse)")
        if ep.recommended_player_name:
            print(f"    → {ep.recommended_player_name}  "
                  f"(score {ep.recommended_score:.1f}, "
                  f"+{ep.delta:.1f} vs. next-best)")
            if ep.top3_alternatives:
                alts = ", ".join(f"{n} ({s:.0f})" for _, n, s in ep.top3_alternatives)
                print(f"      alternatives: {alts}")
        else:
            print(f"    → (no recommendation — all top players claimed by earlier events)")
            if ep.top3_alternatives:
                alts = ", ".join(f"{n} ({s:.0f})" for _, n, s in ep.top3_alternatives)
                print(f"      candidates: {alts}")
    if plan.unassigned_players:
        print()
        print(f"  Unassigned in this window ({len(plan.unassigned_players)}):"
              f" {', '.join(n for _, n in plan.unassigned_players[:8])}"
              f"{'…' if len(plan.unassigned_players) > 8 else ''}")
    print("═" * 78)
