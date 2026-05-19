"""
scoring/multi_objective.py — score each available player against four objectives.

The league has four payout buckets (per Chad's pool rules):
  1. Season-long earnings (top 5)         → maximize expected earnings
  2. Score-to-par across the 4 majors     → only matters at majors
  3. Most cuts made                        → maximize P(make cut)
  4. Weekly skins pot, with rollover       → maximize EV of (P(win) × pot_share)

For each player we estimate a finish probability distribution, then compute
each objective score, then combine with user-set weights.

The probability estimation cascade (best to worst signal):
  1. DataGolf finish distribution (if available) — gold standard
  2. Vegas implied probabilities (de-vigged), interpolated to top-X bins
  3. ESPN-derived season form (P(cut), P(top10), P(top20) frequencies)
  4. Flat prior based on field size (uniform 1/N for win, etc.)

In Bayesian terms: each player has a prior (3 or 4), updated to a
posterior when better-signal data exists.

Output is a list of ScoredPlayer records, sortable by composite_score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .inputs import EventInputs, PlayerInputs


# ─────────────────────────────────────────────────────────────────────────────
# League weights
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ObjectiveWeights:
    """
    How much each payout bucket matters. Should sum to ~1.0. Default reflects
    a 50-entry pool where season earnings dominate, majors are critical (must
    pick in all 4), cuts is a sleeper category, and skins is per-event variance.

    Adjust based on:
      - Pool size — bigger pool → tilt to skins (higher variance needed)
      - Your standing — leading → tilt to cuts (safe), behind → skins
      - Calendar — last 4 events of season → tilt to whichever bucket you can
        still realistically win
      - Bucket eligibility — if you joined mid-season and missed a major,
        set can_win_majors_bucket=False; the majors weight redistributes to
        the other three at major events too.
    """
    season_earnings: float = 0.45
    majors_score: float = 0.15        # only counts at major events
    cuts_made: float = 0.15
    skins_ev: float = 0.25
    can_win_majors_bucket: bool = True   # set False if you missed a major

    def normalize(self) -> "ObjectiveWeights":
        s = self.season_earnings + self.majors_score + self.cuts_made + self.skins_ev
        if s <= 0:
            return self
        return ObjectiveWeights(
            season_earnings=self.season_earnings / s,
            majors_score=self.majors_score / s,
            cuts_made=self.cuts_made / s,
            skins_ev=self.skins_ev / s,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Output record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FinishProbs:
    """Estimated finish probabilities. Should sum approximately to 1."""
    win: float
    top5: float
    top10: float
    top20: float
    make_cut: float
    # The source of this estimate, for debugging:
    source: str           # 'datagolf' | 'vegas' | 'espn_form' | 'flat_prior'
    confidence: float     # 0–1, used to discount the overall score


@dataclass
class ScoredPlayer:
    canonical_id: str
    display_name: str
    finish_probs: FinishProbs
    # Per-objective scores, each on a 0-100 scale within this event's field
    score_season_earnings: float
    score_majors: float
    score_cuts: float
    score_skins_ev: float
    expected_earnings: float          # in dollars
    expected_skins_payout: float       # in dollars
    composite_score: float            # weighted combination, 0-100
    # Diagnostics for the agent's explanation:
    rationale: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Probability estimation
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_finish_probs(
    p: PlayerInputs,
    *,
    field_size: int,
) -> FinishProbs:
    """
    Estimate a finish-probability distribution for one player.

    Cascade through signal sources, taking the best available, then apply
    a course-history adjustment as a multiplicative bump scaled by evidence.
    """
    base = _base_finish_probs(p, field_size=field_size)
    return _apply_venue_adjustment(base, p)


def _base_finish_probs(
    p: PlayerInputs,
    *,
    field_size: int,
) -> FinishProbs:
    """Pre-venue baseline probability estimate, via signal cascade."""
    # 1. DataGolf — gold standard if present
    if p.datagolf_finish_distribution:
        d = p.datagolf_finish_distribution
        return FinishProbs(
            win=float(d.get("win", 0.0)),
            top5=float(d.get("t5", 0.0)),
            top10=float(d.get("t10", 0.0)),
            top20=float(d.get("t20", 0.0)),
            make_cut=float(d.get("cut", d.get("make_cut", 0.0))),
            source="datagolf",
            confidence=0.95,
        )

    # 2. Vegas — direct win prob, interpolate the rest
    if p.vegas_win_implied_pct is not None:
        win = p.vegas_win_implied_pct
        top10 = p.vegas_top10_implied_pct if p.vegas_top10_implied_pct else _interp_top10_from_win(win)
        top5 = top10 * 0.55          # empirical ratio: ~half of top10s also crack top5
        top20 = min(top10 * 1.75, 1.0)
        cut = _cut_prob_from_win(win)
        return FinishProbs(
            win=win, top5=top5, top10=top10, top20=top20, make_cut=cut,
            source="vegas", confidence=0.75,
        )

    # 3. ESPN-derived season form
    if p.season_starts >= 3:
        cut_rate = p.season_cuts_made / p.season_starts
        top10_rate = p.season_top_10s / p.season_starts
        top20_rate = p.season_top_20s / p.season_starts
        # Win frequency is too small to estimate reliably from N=3-30 starts;
        # back into it from top10 rate with a conservative ratio
        win_estimate = max(top10_rate * 0.08, 1.0 / field_size * 0.5)
        top5_estimate = top10_rate * 0.55
        return FinishProbs(
            win=win_estimate,
            top5=top5_estimate,
            top10=top10_rate,
            top20=top20_rate,
            make_cut=cut_rate,
            source="espn_form",
            confidence=0.5,
        )

    # 4. Flat prior — uniform distribution
    return FinishProbs(
        win=1.0 / max(field_size, 1),
        top5=5.0 / max(field_size, 1),
        top10=10.0 / max(field_size, 1),
        top20=20.0 / max(field_size, 1),
        make_cut=0.65,   # roughly the cut rate of a random PGA Tour entrant
        source="flat_prior",
        confidence=0.2,
    )


def _apply_venue_adjustment(probs: FinishProbs, p: PlayerInputs) -> FinishProbs:
    """
    Adjust probabilities by venue skill lift, scaled by evidence weight.

    Evidence weight is roughly the player's starts at this course (capped).
    With 0 starts, no adjustment. With 5+ starts of consistent history, full
    application of the lift. Linear interpolation in between.

    The lift is multiplicative, applied to win/top5/top10/top20 in tandem
    (these are correlated — a player who's better at this venue beats the
    field at all these thresholds). The cut probability gets a softer
    adjustment because cut-making is less venue-dependent than finishing
    high.
    """
    if p.venue_evidence_weight <= 0 or p.venue_skill_lift == 1.0:
        return probs  # no history, or perfectly neutral

    # How much of the lift to apply (0 → 1 as evidence grows from 0 to 5 starts)
    evidence_factor = min(p.venue_evidence_weight / 5.0, 1.0)
    effective_lift = 1.0 + (p.venue_skill_lift - 1.0) * evidence_factor

    # Apply to win/top5/top10/top20, with caps so we never assert >100% probs
    def bump(x: float) -> float:
        return min(x * effective_lift, 0.97)

    # For make_cut: smaller venue effect (cut-making is less venue-dependent).
    # Move the cut prob halfway toward the lift-adjusted version.
    cut_adjusted = (probs.make_cut + bump(probs.make_cut)) / 2

    return FinishProbs(
        win=bump(probs.win),
        top5=bump(probs.top5),
        top10=bump(probs.top10),
        top20=bump(probs.top20),
        make_cut=min(cut_adjusted, 0.97),
        source=probs.source + "+venue",
        # Small confidence bump for having a real course-history signal
        confidence=min(probs.confidence + 0.05 * evidence_factor, 0.99),
    )


def _interp_top10_from_win(win_pct: float) -> float:
    """
    Empirical heuristic: P(top10) ≈ 4-6x P(win) at the top of the field,
    flattening toward ~25% for low-win-prob players.
    """
    if win_pct >= 0.10:
        return min(win_pct * 4.5, 0.75)
    if win_pct >= 0.03:
        return win_pct * 5.5
    return min(win_pct * 7.0 + 0.05, 0.30)


def _cut_prob_from_win(win_pct: float) -> float:
    """Strong favorite ≈ 95% cut rate; longshot ≈ 65%. Smooth in between."""
    if win_pct >= 0.10:
        return 0.95
    if win_pct >= 0.03:
        return 0.85
    if win_pct >= 0.01:
        return 0.78
    return 0.65


# ─────────────────────────────────────────────────────────────────────────────
# Objective scoring
# ─────────────────────────────────────────────────────────────────────────────

# PGA Tour standard purse breakdown — what fraction of the purse each finish
# position pays. These are approximations; actual breakdown varies slightly
# by event. Source: PGA Tour money distribution percentages.
PURSE_DISTRIBUTION = {
    1:   0.180,
    2:   0.109,
    3:   0.069,
    4:   0.049,
    5:   0.041,
    # T5-T10 average:
    "top10_avg": 0.027,
    # T11-T20 average:
    "top20_avg": 0.0125,
    # Made-cut-but-outside-top-20 average:
    "made_cut_avg": 0.005,
}


def _expected_earnings(probs: FinishProbs, purse: Optional[float]) -> float:
    """Expected dollars from a single start at this event."""
    if not purse:
        return 0.0
    # Decompose the distribution into disjoint buckets
    p_win = probs.win
    p_top5_not_win = max(probs.top5 - probs.win, 0.0)
    p_top10_not_top5 = max(probs.top10 - probs.top5, 0.0)
    p_top20_not_top10 = max(probs.top20 - probs.top10, 0.0)
    p_made_cut_not_top20 = max(probs.make_cut - probs.top20, 0.0)

    avg_top5_share = (PURSE_DISTRIBUTION[2] + PURSE_DISTRIBUTION[3]
                      + PURSE_DISTRIBUTION[4] + PURSE_DISTRIBUTION[5]) / 4
    return purse * (
        p_win * PURSE_DISTRIBUTION[1]
        + p_top5_not_win * avg_top5_share
        + p_top10_not_top5 * PURSE_DISTRIBUTION["top10_avg"]
        + p_top20_not_top10 * PURSE_DISTRIBUTION["top20_avg"]
        + p_made_cut_not_top20 * PURSE_DISTRIBUTION["made_cut_avg"]
    )


def _expected_skins_payout(
    probs: FinishProbs,
    event: EventInputs,
    *,
    co_pickers: float,
) -> float:
    """EV of the skins payout: P(win) × (pot / co_pickers).

    co_pickers comes from estimate_pool_picks — a Vegas-weighted estimate
    of how many pool entries will pick this same player. Pre-computed once
    per event in score_field.
    """
    if event.skins_pot <= 0 or probs.win <= 0:
        return 0.0
    return probs.win * (event.skins_pot / max(co_pickers, 1.0))


def _pool_pick_distribution(
    event: EventInputs,
    estimates: list[tuple[PlayerInputs, FinishProbs]],
) -> dict[str, float]:
    """Per-player {canonical_id: share-of-pool-picks} based on Vegas-implied
    win probability. Falls back to the cascade's own win prob when Vegas
    isn't available for a player."""
    from .skins_ev import estimate_pool_picks
    odds_list = []
    for p, probs in estimates:
        implied = (p.vegas_win_implied_pct
                   if p.vegas_win_implied_pct is not None
                   else probs.win)
        odds_list.append({
            "player": p.canonical_id,
            "implied_win_pct": (implied or 0.0) * 100,
        })
    return estimate_pool_picks(odds_list, n_entries=event.pool_entries)


def _normalize_0_100(values: list[float]) -> list[float]:
    """Rescale a list of values to [0, 100]. All-equal → all 50."""
    if not values:
        return values
    lo, hi = min(values), max(values)
    if hi == lo:
        return [50.0] * len(values)
    return [(v - lo) / (hi - lo) * 100 for v in values]


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring
# ─────────────────────────────────────────────────────────────────────────────

def score_field(
    event: EventInputs,
    weights: ObjectiveWeights = ObjectiveWeights(),
) -> list[ScoredPlayer]:
    """
    Score every available player in the event's field. Returns sorted by
    composite_score descending.
    """
    w = weights.normalize()

    # Step 1: per-player probability estimates
    estimates: list[tuple[PlayerInputs, FinishProbs]] = []
    for p in event.players:
        probs = _estimate_finish_probs(p, field_size=event.field_size)
        estimates.append((p, probs))

    # Step 2: raw per-objective scores (in their natural units). Skins EV
    # uses a Vegas-weighted estimate of how many pool entries pick each
    # player — pre-computed once for the field rather than re-derived per
    # player from a flat heuristic.
    pool_shares = _pool_pick_distribution(event, estimates)
    raw_earnings = [_expected_earnings(probs, event.purse) for _, probs in estimates]
    raw_cuts = [probs.make_cut for _, probs in estimates]
    raw_skins = [
        _expected_skins_payout(
            probs, event,
            co_pickers=max(
                event.pool_entries * pool_shares.get(p.canonical_id, 1.0 / max(len(estimates), 1)),
                1.0,
            ),
        )
        for p, probs in estimates
    ]
    # Majors score = P(top20) × purse share at majors (where score-to-par matters);
    # only relevant if this is a major
    if event.is_major:
        raw_majors = [probs.top20 for _, probs in estimates]
    else:
        raw_majors = [0.0] * len(estimates)

    # Step 3: normalize each objective to [0, 100] within this field
    norm_earnings = _normalize_0_100(raw_earnings)
    norm_cuts = _normalize_0_100(raw_cuts)
    norm_skins = _normalize_0_100(raw_skins)
    norm_majors = _normalize_0_100(raw_majors)

    # Step 4: combine with weights, scale composite by source confidence
    scored: list[ScoredPlayer] = []
    # If user can't win the majors bucket (joined late, missed a major),
    # treat all events as non-majors for weighting purposes.
    treat_as_major = event.is_major and weights.can_win_majors_bucket
    for i, (p, probs) in enumerate(estimates):
        # If this isn't an effective major, redistribute majors weight to earnings
        if treat_as_major:
            we, wm, wc, ws = w.season_earnings, w.majors_score, w.cuts_made, w.skins_ev
        else:
            we, wm, wc, ws = w.season_earnings + w.majors_score, 0.0, w.cuts_made, w.skins_ev

        composite = (
            we * norm_earnings[i]
            + wm * norm_majors[i]
            + wc * norm_cuts[i]
            + ws * norm_skins[i]
        )
        # Discount by signal confidence — flat-prior estimates aren't trustworthy
        composite *= probs.confidence

        rationale = _build_rationale(p, probs, event, raw_earnings[i], raw_skins[i])
        scored.append(ScoredPlayer(
            canonical_id=p.canonical_id,
            display_name=p.display_name,
            finish_probs=probs,
            score_season_earnings=norm_earnings[i],
            score_majors=norm_majors[i],
            score_cuts=norm_cuts[i],
            score_skins_ev=norm_skins[i],
            expected_earnings=raw_earnings[i],
            expected_skins_payout=raw_skins[i],
            composite_score=composite,
            rationale=rationale,
        ))

    scored.sort(key=lambda s: s.composite_score, reverse=True)
    return scored


def _build_rationale(
    p: PlayerInputs,
    probs: FinishProbs,
    event: EventInputs,
    expected_earnings: float,
    expected_skins: float,
) -> list[str]:
    bits = []
    bits.append(f"P(win)={probs.win:.1%}, P(top10)={probs.top10:.1%}, "
                f"P(cut)={probs.make_cut:.1%}  [{probs.source}, conf={probs.confidence:.2f}]")
    if expected_earnings > 0:
        bits.append(f"Expected earnings: ${expected_earnings:,.0f}")
    if expected_skins > 0:
        bits.append(f"Expected skins payout: ${expected_skins:,.0f}")
    if p.season_starts > 0:
        bits.append(f"Season form: {p.season_cuts_made}/{p.season_starts} cuts, "
                    f"{p.season_top_10s} T10s, {p.season_wins} wins, "
                    f"${p.season_earnings:,.0f} earned")
    if p.venue_evidence_weight > 0:
        lift_pct = (p.venue_skill_lift - 1.0) * 100
        lift_str = f"+{lift_pct:.0f}%" if lift_pct >= 0 else f"{lift_pct:.0f}%"
        bits.append(f"Course history: {p.venue_top_10s}/{p.venue_starts} T10s, "
                    f"{p.venue_made_cuts} cuts, "
                    f"venue lift {lift_str} vs other courses")
    if event.is_major:
        bits.append("MAJOR — score-to-par bonus weight active")
    return bits
