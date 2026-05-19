"""
Walk-forward backtester.

For each completed event in `season` (chronological):
  1. Build EventInputs *as of* that event's start_date — season_results
     and course_history are filtered to events strictly before that date,
     so the scorer can't peek at the future.
  2. Apply a strategy → pick one canonical_player_id (respecting the
     simulated burned set: no player can be re-picked).
  3. Look up the player's actual result from season_results.
  4. Update bookkeeping: burned set, skins pot rollover, running totals.

At the end, report 4-bucket performance matching Chad's pool:
  - Season earnings (sum of pick earnings)
  - Majors score-to-par (sum across events flagged is_major)
  - Cuts made (count of picks with made_cut=1)
  - Skins payouts (analytical: pot/co_pickers when pick wins,
                   rolls over otherwise)

`compare_strategies` runs multiple strategies against the same season
and prints a side-by-side table.

LIMITATIONS
  - OWGR snapshot leaks future skill data into past events (we use the
    current rank map, not the as-of-event rank). Address later by
    filtering owgr_rankings.week_ending.
  - Skins simulation models only "if Chad wins, pot pays"; it does not
    simulate other pool entries. Real-world skins resets when ANY entry
    picks the winner — we conservatively roll over instead. Tends to
    over-credit the strategy that lands single wins late.
  - vegas_odds for historical events typically aren't present in
    event_odds; that strategy falls back to a None pick when no Vegas
    line is found and the event is skipped from the strategy's totals.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from ledger.ledger import Ledger
from scoring.inputs import EventInputs, build_event_inputs
from scoring.multi_objective import ObjectiveWeights, score_field

logger = logging.getLogger("backtester")


# ─────────────────────────────────────────────────────────────────────────────
# Records
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PickRecord:
    canonical_event_id: str
    event_name: str
    start_date: str
    is_major: bool
    picked_canonical_id: Optional[str]
    picked_player_name: Optional[str]
    position: Optional[str]
    score_to_par: Optional[int]
    earnings: float
    made_cut: Optional[bool]
    pot_at_event: float
    skins_payout: float
    pick_won: bool                 # True if picked player finished 1st
    note: Optional[str] = None     # 'no_pick' if strategy returned None


@dataclass
class BacktestResult:
    season: int
    strategy_name: str
    picks: list[PickRecord] = field(default_factory=list)
    # Totals
    total_earnings: float = 0.0
    majors_score_to_par: int = 0      # lower is better
    cuts_made: int = 0
    cuts_missed: int = 0
    wins: int = 0
    top_10s: int = 0
    skins_won: float = 0.0
    events_with_no_pick: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Strategies — `event` is a fully-built EventInputs; return canonical_id or None
# ─────────────────────────────────────────────────────────────────────────────

Strategy = Callable[[EventInputs, ObjectiveWeights], Optional[str]]


def agent_top_composite(event: EventInputs, weights: ObjectiveWeights) -> Optional[str]:
    scored = score_field(event, weights)
    return scored[0].canonical_id if scored else None


def vegas_favorite(event: EventInputs, weights: ObjectiveWeights) -> Optional[str]:
    has_vegas = [p for p in event.players if p.vegas_win_implied_pct]
    if not has_vegas:
        return None
    return max(has_vegas, key=lambda p: p.vegas_win_implied_pct or 0.0).canonical_id


def owgr_top(event: EventInputs, weights: ObjectiveWeights) -> Optional[str]:
    ranked = [p for p in event.players if p.owgr_rank]
    if not ranked:
        return None
    return min(ranked, key=lambda p: p.owgr_rank).canonical_id


def random_pick(event: EventInputs, weights: ObjectiveWeights) -> Optional[str]:
    if not event.players:
        return None
    return random.choice(event.players).canonical_id


STRATEGIES: dict[str, Strategy] = {
    "agent": agent_top_composite,
    "vegas": vegas_favorite,
    "owgr": owgr_top,
    "random": random_pick,
}


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward simulation
# ─────────────────────────────────────────────────────────────────────────────

def _position_to_int(pos: Optional[str]) -> Optional[int]:
    if not pos:
        return None
    try:
        return int(pos.lstrip("T"))
    except ValueError:
        return None


def walk_forward_backtest(
    db_path: str | Path,
    season: int,
    strategy: Strategy,
    *,
    strategy_name: str = "agent",
    weights: Optional[ObjectiveWeights] = None,
    weekly_skins_contribution: float = 372.0,
    pool_entries: int = 12,
) -> BacktestResult:
    """Walk the season chronologically and run `strategy` per event."""
    weights = weights or ObjectiveWeights()
    result = BacktestResult(season=season, strategy_name=strategy_name)

    ledger = Ledger(db_path)
    try:
        events = ledger.conn.execute(
            """
            SELECT canonical_event_id, name, start_date, is_major
            FROM events
            WHERE season = ? AND status = 'post'
              AND start_date IS NOT NULL
            ORDER BY start_date ASC, canonical_event_id ASC
            """,
            (season,),
        ).fetchall()
    finally:
        ledger.close()

    burned: set[str] = set()
    pot = weekly_skins_contribution  # baseline; rolls each event

    for ev in events:
        ev_id = ev["canonical_event_id"]
        start = (ev["start_date"] or "")[:10]
        is_major = bool(ev["is_major"])

        # Build inputs with as-of-date leakage guard + simulated burned set
        try:
            inputs = build_event_inputs(
                db_path, ev_id,
                season=season, skins_pot=pot,
                pool_entries=pool_entries,
                as_of_date=start,
                burned_override=burned,
                include_course_history=True,
            )
        except Exception as e:
            logger.warning("skipping %s (%s): build_event_inputs failed: %s",
                           ev["name"], ev_id, e)
            continue

        # Strategy returns the chosen canonical_id
        try:
            pick_cid = strategy(inputs, weights)
        except Exception as e:
            logger.warning("strategy raised on %s: %s", ev["name"], e)
            pick_cid = None

        # Honor the simulated burn list — strategy might return a burned player
        # if its logic doesn't filter (e.g., random_pick won't); skip those.
        valid_field = {p.canonical_id for p in inputs.players}
        if pick_cid is not None and pick_cid in burned:
            pick_cid = None
        if pick_cid is not None and pick_cid not in valid_field:
            pick_cid = None

        if pick_cid is None:
            result.events_with_no_pick += 1
            pot += weekly_skins_contribution  # nobody picked → roll over
            result.picks.append(PickRecord(
                canonical_event_id=ev_id, event_name=ev["name"],
                start_date=start, is_major=is_major,
                picked_canonical_id=None, picked_player_name=None,
                position=None, score_to_par=None, earnings=0.0,
                made_cut=None, pot_at_event=pot, skins_payout=0.0,
                pick_won=False, note="no_pick",
            ))
            continue

        # Look up the actual result for that player at that event
        l2 = Ledger(db_path)
        try:
            actual = l2.conn.execute(
                """
                SELECT r.position, r.score_to_par, r.earnings, r.made_cut,
                       pl.display_name
                FROM season_results r
                LEFT JOIN players pl ON pl.canonical_id = r.canonical_player_id
                WHERE r.canonical_event_id = ? AND r.canonical_player_id = ?
                """,
                (ev_id, pick_cid),
            ).fetchone()
        finally:
            l2.close()

        if actual is None:
            # Player was in the field per event_field but not in season_results
            # (e.g. WD before tee time). Treat as $0 / no finish.
            burned.add(pick_cid)
            result.picks.append(PickRecord(
                canonical_event_id=ev_id, event_name=ev["name"],
                start_date=start, is_major=is_major,
                picked_canonical_id=pick_cid,
                picked_player_name=None,
                position=None, score_to_par=None, earnings=0.0,
                made_cut=None, pot_at_event=pot, skins_payout=0.0,
                pick_won=False, note="result_missing",
            ))
            pot += weekly_skins_contribution
            continue

        # Update totals
        earnings = float(actual["earnings"] or 0.0)
        made_cut = bool(actual["made_cut"]) if actual["made_cut"] is not None else None
        rank = _position_to_int(actual["position"])
        pick_won = (rank == 1)
        score_to_par = actual["score_to_par"]

        # Skins payout: if pick won, pot pays out to (estimated) co_pickers.
        # See file docstring for the simplification.
        skins_payout = 0.0
        if pick_won:
            from scoring.multi_objective import _pool_pick_distribution
            from scoring.multi_objective import _estimate_finish_probs
            estimates = [(p, _estimate_finish_probs(p, field_size=inputs.field_size))
                         for p in inputs.players]
            shares = _pool_pick_distribution(inputs, estimates)
            my_share = shares.get(pick_cid, 1.0 / max(len(inputs.players), 1))
            co_pickers = max(pool_entries * my_share, 1.0)
            skins_payout = pot / co_pickers

        # Update aggregates
        result.total_earnings += earnings
        if made_cut is True:
            result.cuts_made += 1
        elif made_cut is False:
            result.cuts_missed += 1
        if rank is not None and rank <= 10:
            result.top_10s += 1
        if pick_won:
            result.wins += 1
        if is_major and score_to_par is not None:
            result.majors_score_to_par += int(score_to_par)
        result.skins_won += skins_payout

        # Pot rollover: reset to baseline if Chad's pick won; otherwise roll
        if pick_won:
            pot = weekly_skins_contribution
        else:
            pot += weekly_skins_contribution

        burned.add(pick_cid)
        result.picks.append(PickRecord(
            canonical_event_id=ev_id, event_name=ev["name"],
            start_date=start, is_major=is_major,
            picked_canonical_id=pick_cid,
            picked_player_name=actual["display_name"] or pick_cid,
            position=actual["position"], score_to_par=score_to_par,
            earnings=earnings, made_cut=made_cut,
            pot_at_event=pot, skins_payout=skins_payout,
            pick_won=pick_won,
        ))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Comparison + pretty-print
# ─────────────────────────────────────────────────────────────────────────────

def compare_strategies(
    db_path: str | Path,
    season: int,
    strategy_names: list[str],
    *,
    weights: Optional[ObjectiveWeights] = None,
    weekly_skins_contribution: float = 372.0,
    pool_entries: int = 12,
    seed: Optional[int] = None,
) -> dict[str, BacktestResult]:
    """Run several strategies against the same season. Returns
    {strategy_name: BacktestResult}."""
    if seed is not None:
        random.seed(seed)
    out = {}
    for name in strategy_names:
        if name not in STRATEGIES:
            raise ValueError(f"Unknown strategy {name!r}. "
                             f"Choices: {sorted(STRATEGIES)}")
        out[name] = walk_forward_backtest(
            db_path, season, STRATEGIES[name],
            strategy_name=name, weights=weights,
            weekly_skins_contribution=weekly_skins_contribution,
            pool_entries=pool_entries,
        )
    return out


def print_comparison(results: dict[str, BacktestResult]) -> None:
    names = list(results.keys())
    print()
    print("─" * 78)
    print(f"  {'Bucket':<28} " + "  ".join(f"{n:>10}" for n in names))
    print("─" * 78)
    rows = [
        ("Season earnings",       lambda r: f"${r.total_earnings:>9,.0f}"),
        ("Majors score-to-par",   lambda r: f"{r.majors_score_to_par:>+10d}"),
        ("Cuts made",             lambda r: f"{r.cuts_made:>10d}"),
        ("Cuts missed",           lambda r: f"{r.cuts_missed:>10d}"),
        ("Wins",                  lambda r: f"{r.wins:>10d}"),
        ("Top 10s",               lambda r: f"{r.top_10s:>10d}"),
        ("Skins payouts",         lambda r: f"${r.skins_won:>9,.2f}"),
        ("Events with no pick",   lambda r: f"{r.events_with_no_pick:>10d}"),
    ]
    for label, fn in rows:
        print(f"  {label:<28} " + "  ".join(fn(results[n]) for n in names))
    print("─" * 78)


def print_picks_log(result: BacktestResult, *, show_n: int = 20) -> None:
    print()
    print(f"  {result.strategy_name.upper()} — per-event picks "
          f"(first {min(show_n, len(result.picks))}):")
    print()
    print(f"  {'Date':12}{'Event':30}{'Player':22}{'Pos':>6}{'Earnings':>11}  Note")
    print("  " + "─" * 92)
    for p in result.picks[:show_n]:
        name = (p.picked_player_name or "(no pick)")[:22]
        pos = p.position or "—"
        marker = " *MAJOR*" if p.is_major else ""
        note = p.note or ""
        print(f"  {p.start_date[:10]:12}{p.event_name[:30]:30}{name:22}"
              f"{pos:>6}${p.earnings:>10,.0f}  {note}{marker}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Walk-forward backtest the scorer.")
    p.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    p.add_argument("--season", type=int,
                   default=int(os.environ.get("QUIPU_SEASON", date.today().year)))
    p.add_argument("--strategies", default="agent,vegas,owgr,random",
                   help="Comma-separated strategies to compare. "
                        f"Choices: {','.join(STRATEGIES)}.")
    p.add_argument("--weekly-skins", type=float,
                   default=float(os.environ.get("QUIPU_WEEKLY_SKINS", "372.0")))
    p.add_argument("--entries", type=int,
                   default=int(os.environ.get("QUIPU_POOL_ENTRIES", "12")))
    p.add_argument("--show-picks", action="store_true",
                   help="Print per-event picks log for each strategy.")
    p.add_argument("--seed", type=int, default=42,
                   help="Seed for the random strategy (reproducibility).")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    results = compare_strategies(
        args.db, args.season, names,
        weekly_skins_contribution=args.weekly_skins,
        pool_entries=args.entries,
        seed=args.seed,
    )

    n_events = len(next(iter(results.values())).picks)
    print(f"\nSeason {args.season}: {n_events} completed events backtested.")
    print_comparison(results)

    if args.show_picks:
        for name in names:
            print_picks_log(results[name])

    return 0


if __name__ == "__main__":
    sys.exit(main())
