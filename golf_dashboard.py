# golf_dashboard.py

from flask import Flask, render_template_string, jsonify, request
import json
import os
import sqlite3
from datetime import date, datetime
from typing import Optional

from normalize.events import CANONICAL_EVENTS, get_upcoming
from normalize.players import get_all_available, _owgr_rank_map, _get_crosswalk
from ledger.ledger import Ledger
from scoring.inputs import build_event_inputs
from scoring.multi_objective import score_field

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config — env-overridable
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("QUIPU_DB", "data/golf.db")
SEASON = int(os.environ.get("QUIPU_SEASON", date.today().year))
# Pool config — defaults match Chad's 12-entry pool ($31/wk skins × 12 = $372)
POOL_ENTRIES = int(os.environ.get("QUIPU_POOL_ENTRIES", "12"))
WEEKLY_SKINS_CONTRIBUTION = float(os.environ.get("QUIPU_WEEKLY_SKINS", "372.0"))
TOTAL_EVENTS_IN_SEASON = int(os.environ.get("QUIPU_TOTAL_EVENTS", "31"))

# CXL brand palette + a few derived tones for cards/borders/muted text
CSS_COLORS = {
    "bg": "#222126",
    "bg_card": "#2d2c33",
    "text": "#FFFEF8",
    "text_muted": "#9d9c95",
    "accent": "#E6FD76",
    "green": "#24483A",
    "blue": "#01455B",
    "border": "#3a3940",
}
CSS_FONT = "'Work Sans', Arial, sans-serif"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _confidence_label(c: float) -> str:
    if c >= 0.8:
        return "HIGH"
    if c >= 0.5:
        return "MED"
    return "LOW"


def _current_ledger_event(ledger: Ledger) -> dict | None:
    """The most recent 'scheduled' event in the ledger — what monday_open
    last populated. Returns None if nothing is scheduled."""
    row = ledger.conn.execute(
        """
        SELECT canonical_event_id, name, course_name, purse, is_major, start_date
        FROM events
        WHERE season = ? AND status = 'scheduled'
          AND (start_date IS NULL OR start_date >= date('now'))
        ORDER BY (start_date IS NULL), start_date ASC
        LIMIT 1
        """,
        (SEASON,),
    ).fetchone()
    return dict(row) if row else None


def _odds_for_event(ledger: Ledger, canonical_event_id: str) -> dict[str, dict]:
    """{canonical_player_id: {american, raw_implied, fair_implied}} from event_odds.
    Returns empty dict if the table doesn't exist yet (no add_odds runs)."""
    try:
        rows = ledger.conn.execute(
            """
            SELECT canonical_player_id, american_odds, raw_implied, fair_implied
            FROM event_odds
            WHERE canonical_event_id = ?
            """,
            (canonical_event_id,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return {}
        raise
    return {
        r["canonical_player_id"]: {
            "american": r["american_odds"],
            "raw_implied": r["raw_implied"],
            "fair_implied": r["fair_implied"],
        }
        for r in rows
    }


def _american_str(american: int | None) -> str:
    if american is None:
        return "—"
    return f"+{american}" if american > 0 else str(american)


# ---------------------------------------------------------------------------
# Data functions
# ---------------------------------------------------------------------------

def get_available_players() -> list[dict]:
    """All unburned active players, enriched with odds for the current event."""
    players = get_all_available(season=SEASON)

    odds_by_cid: dict[str, dict] = {}
    ledger = Ledger(DB_PATH)
    try:
        ev = _current_ledger_event(ledger)
        if ev:
            odds_by_cid = _odds_for_event(ledger, ev["canonical_event_id"])
    finally:
        ledger.close()

    # Players come back keyed by readable id; we need canonical_id for odds lookup.
    # The compat shim's `id` is the slug, not the canonical p_NNNNN id, so we need
    # a second pass through the crosswalk. For perf, build a name→canonical map.
    from normalize.players import _get_crosswalk
    xw = _get_crosswalk()
    cid_by_name = {
        r["display_name"]: r["canonical_id"]
        for r in xw.conn.execute("SELECT canonical_id, display_name FROM players").fetchall()
    }

    out = []
    for p in players:
        cid = cid_by_name.get(p["display_name"])
        o = odds_by_cid.get(cid, {}) if cid else {}
        implied = (o.get("fair_implied") or 0.0) * 100  # → percentage
        out.append({
            "name": p["display_name"],
            "tier": p["tier"],
            "owgr": p["owgr"] if p["owgr"] else "—",
            "vegas_odds": _american_str(o.get("american")),
            "implied_win_pct": implied,
            "recent_form": "→",  # TODO: wire to season_results history
            "burned": p["burned"],
        })

    tier_order = {"elite": 0, "star": 1, "mid": 2}
    out.sort(key=lambda x: (
        tier_order.get(x["tier"], 3),
        x["owgr"] if isinstance(x["owgr"], int) else 9999,
    ))
    return out


def get_recommendations(top_n: int = 5) -> list[dict]:
    """Top-N picks by composite score for the current scheduled event.
    Returns [] if no event is scheduled in the ledger yet (run monday_open first)."""
    ledger = Ledger(DB_PATH)
    try:
        ev = _current_ledger_event(ledger)
        if ev is None:
            return []
        canonical_event_id = ev["canonical_event_id"]
        skins_pot = ledger.current_skins_pot(
            SEASON, weekly_contribution=WEEKLY_SKINS_CONTRIBUTION,
        )
    finally:
        ledger.close()

    try:
        inputs = build_event_inputs(
            DB_PATH, canonical_event_id,
            season=SEASON, skins_pot=skins_pot,
            expected_winner_pickers=max(POOL_ENTRIES / 4.0, 1.0),
        )
    except Exception as e:
        app.logger.warning("build_event_inputs failed: %s", e)
        return []

    if not inputs.players:
        return []

    odds_by_cid: dict[str, dict] = {}
    ledger = Ledger(DB_PATH)
    try:
        odds_by_cid = _odds_for_event(ledger, canonical_event_id)
    finally:
        ledger.close()

    scored = score_field(inputs)[:top_n]
    out = []
    for r in scored:
        o = odds_by_cid.get(r.canonical_id, {})
        out.append({
            "canonical_id": r.canonical_id,
            "name": r.display_name,
            "composite_score": round(r.composite_score, 1),
            "score_breakdown": [
                round(r.score_season_earnings),
                round(r.score_majors),
                round(r.score_cuts),
                round(r.score_skins_ev),
            ],
            "vegas_odds": _american_str(o.get("american")),
            "implied_win_pct": (o.get("fair_implied") or r.finish_probs.win) * 100,
            "course_fit_reasoning": " · ".join(r.rationale),
            "opportunity_cost": "",  # not yet derived
            "skins_value_probability": r.expected_skins_payout,
            "confidence_level": _confidence_label(r.finish_probs.confidence),
        })
    return out


def get_current_event_info() -> Optional[dict]:
    """The current scheduled event (used for the dashboard's "Lock In Pick" flow).
    Returns None if no event is scheduled."""
    ledger = Ledger(DB_PATH)
    try:
        return _current_ledger_event(ledger)
    finally:
        ledger.close()


def get_season_summary() -> dict:
    """Bankroll, events-completed progress, and current skins pot."""
    ledger = Ledger(DB_PATH)
    try:
        s = ledger.season_summary(SEASON)
        pot = ledger.current_skins_pot(
            SEASON, weekly_contribution=WEEKLY_SKINS_CONTRIBUTION,
        )
    finally:
        ledger.close()
    return {
        "total_winnings": s["total_earnings"],
        "events_completed": s["events_completed"],
        "total_events": TOTAL_EVENTS_IN_SEASON,
        "picks_made": s["picks_made"],
        "pool_entries": POOL_ENTRIES,
        "skins_pot": pot,
        "skins_pot_size": pot,
    }


def get_major_tracker() -> list[dict]:
    """Per-major status for this season. Pulls picked majors from the ledger
    and inserts placeholder rows for majors not yet played."""
    ledger = Ledger(DB_PATH)
    try:
        picked_rows = ledger.majors_picked(SEASON)
        picked_by_name = {row["name"]: row for row in picked_rows}
    finally:
        ledger.close()

    majors = [
        e for e in CANONICAL_EVENTS.values()
        if e.get("type") == "major"
    ]
    majors.sort(key=lambda e: e["start_date"])

    out = []
    for m in majors:
        pick_row = picked_by_name.get(m["display_name"])
        if pick_row:
            out.append({
                "name": m["display_name"],
                "pick_made": True,
                "finish_position": pick_row["position"],
                "winnings": None,  # majors_picked doesn't return earnings
                "score_to_par": pick_row["score_to_par"],
            })
        else:
            out.append({
                "name": m["display_name"],
                "pick_made": False,
                "finish_position": None,
                "winnings": None,
                "score_to_par": None,
            })
    return result


def _player_display_name(canonical_id: str) -> str:
    """Resolve canonical_id → display name using the normalize crosswalk."""
    xw = _get_crosswalk()
    row = xw.get_player(canonical_id)
    if row:
        return row["display_name"]
    return canonical_id


def get_pick_history() -> list[dict]:
    """Pick history for the current season with live OWGR ranks."""
    ledger = Ledger(DB_PATH)
    try:
        picks = ledger.conn.execute(
            """
            SELECT p.picked_at, p.canonical_event_id, p.canonical_player_id,
                   p.position, p.score_to_par, p.earnings, p.made_cut,
                   e.name AS event_name
            FROM picks p
            JOIN events e ON e.canonical_event_id = p.canonical_event_id
            WHERE p.season = ? AND p.voided = 0
            ORDER BY p.picked_at DESC
            """,
            (SEASON,)
        ).fetchall()
    finally:
        ledger.close()


    rank_map = _owgr_rank_map()
    result = []
    for p in picks:
        cid = p["canonical_player_id"]
        event_name = p["event_name"] or "Unknown Event"
        picked_at = p["picked_at"] or ""
        try:
            dt = datetime.strptime(picked_at, "%Y-%m-%d %H:%M:%S")
            date_picked = dt.strftime("%b %-d, %Y")
        except Exception:
            date_picked = picked_at
        player_name = _player_display_name(cid)
        owgr_rank = rank_map.get(cid, "N/A")
        position = p["position"]
        made_cut = p["made_cut"]
        if position is not None:
            if made_cut == 1:
                result_emoji = f"T{position}"
            elif made_cut == 0:
                result_emoji = "❌ Missed Cut"
            else:
                result_emoji = f"T{position}"
        else:
            result_emoji = "— Pending"
        result.append({
            "event": event_name,
            "date_picked": date_picked,
            "player": player_name,
            "owgr": owgr_rank,
            "result": result_emoji,
        })
    return result


def get_upcoming_events() -> list[dict]:
    """Next 6 upcoming events from the canonical schedule (display only)."""
    events = get_upcoming(n=6)
    return [
        {
            "name": e["display_name"],
            "dates": e["dates"],
            "venue": e["venue"],
            "purse": f"${e['purse']:,}",
            "type": (e["type"] or "regular").upper(),
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Golf Dashboard – Chad's Pool</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Work+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: {{ bg }};
  --bg-card: {{ bg_card }};
  --text: {{ text }};
  --text-muted: {{ text_muted }};
  --accent: {{ accent }};
  --green: {{ green }};
  --blue: {{ blue }};
  --border: {{ border }};
}
* { box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: {{ font }};
  margin: 0;
  font-size: 14px;
  line-height: 1.5;
}
code { background: rgba(255,255,255,0.06); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.shell { max-width: 1280px; margin: 0 auto; padding: 24px 32px 48px; }
h1, h2, h3 { margin: 0; font-weight: 600; }
h1 { font-size: 22px; color: var(--accent); letter-spacing: 0.4px; }
h2 { font-size: 12px; color: var(--accent); text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 12px; }
h2 .sub { color: var(--text-muted); text-transform: none; letter-spacing: normal; font-weight: 400; margin-left: 6px; }
h3 { font-size: 15px; font-weight: 600; }
header.top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 28px;
}
.stats { display: flex; gap: 28px; }
.stat { display: flex; flex-direction: column; align-items: flex-end; }
.stat .label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.9px; }
.stat .value { font-size: 18px; font-weight: 600; color: var(--accent); font-variant-numeric: tabular-nums; }
.section { margin-bottom: 32px; }
.grid { display: grid; gap: 16px; }
.grid.two { grid-template-columns: 2fr 1fr; }
.grid.recs { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.grid.events { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
}
.card.flush { padding: 0; }
.event-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 4px solid var(--blue);
  border-radius: 6px;
  padding: 12px 14px;
}
.event-card.major { border-left-color: gold; }
.event-card .meta { color: var(--text-muted); font-size: 12px; margin-top: 6px; }
.event-card .meta .purse { color: var(--accent); font-weight: 500; }
.rec {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rec .head { display: flex; justify-content: space-between; align-items: center; }
.rec .score { color: var(--accent); font-weight: 600; font-size: 20px; font-variant-numeric: tabular-nums; }
.rec .stats { color: var(--text-muted); font-size: 12px; display: flex; gap: 8px; align-items: center; }
.rec .rationale { font-size: 12px; color: var(--text); line-height: 1.55; opacity: 0.88; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.6px;
}
.badge.high { background: #2196F3; color: white; }
.badge.med  { background: var(--accent); color: black; }
.badge.low  { background: #f44336; color: white; }
button.lock {
  background: var(--accent);
  color: black;
  border: none;
  padding: 8px 12px;
  border-radius: 4px;
  font-family: inherit;
  font-weight: 600;
  font-size: 12px;
  cursor: pointer;
  letter-spacing: 0.4px;
  margin-top: 2px;
}
button.lock:hover { filter: brightness(0.92); }
button.lock:disabled { background: var(--text-muted); cursor: not-allowed; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 14px; text-align: left; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
th { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.7px; font-weight: 600; }
tbody tr:hover { background: rgba(230, 253, 118, 0.04); }
tr.elite td:first-child { color: var(--accent); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.major-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.major-row:last-child { border-bottom: none; }
.major-row .name { font-weight: 500; }
.major-row.tbd .name { color: var(--text-muted); }
.major-status { font-size: 12px; color: var(--text-muted); }
.major-status.made { color: var(--accent); }
.empty {
  color: var(--text-muted);
  font-style: italic;
  padding: 20px;
  text-align: center;
}
@media (max-width: 900px) {
  .grid.two { grid-template-columns: 1fr; }
  .stats { gap: 18px; }
  .stat .value { font-size: 16px; }
}
</style>
</head>
<body>
<div class="shell">

  <header class="top">
    <h1>Golf Dashboard <span style="color: var(--text-muted); font-weight: 400; font-size: 14px; letter-spacing: 0;">· Chad's Pool · {{ season_summary.pool_entries }} entries</span></h1>
    <div class="stats">
      <div class="stat">
        <span class="label">Earned</span>
        <span class="value">${{ "{:,.0f}".format(season_summary.total_winnings) }}</span>
      </div>
      <div class="stat">
        <span class="label">Events</span>
        <span class="value">{{ season_summary.events_completed }}/{{ season_summary.total_events }}</span>
      </div>
      <div class="stat">
        <span class="label">Picks</span>
        <span class="value">{{ season_summary.picks_made }}</span>
      </div>
      <div class="stat">
        <span class="label">Skins Pot</span>
        <span class="value">${{ "{:,.0f}".format(season_summary.skins_pot_size) }}</span>
      </div>
    </div>
  </header>

  <section class="section">
    <h2>Recommendations{% if current_event %}<span class="sub">— {{ current_event.name }}</span>{% endif %}</h2>
    {% if recommendations %}
      <div class="grid recs">
        {% for rec in recommendations %}
          <div class="rec">
            <div class="head">
              <h3>{{ rec.name }}</h3>
              <span class="score">{{ "%.0f"|format(rec.composite_score) }}</span>
            </div>
            <div class="stats">
              <span>{{ rec.vegas_odds }}</span>
              <span>·</span>
              <span>{{ "%.1f"|format(rec.implied_win_pct) }}% win</span>
              <span class="badge {{ rec.confidence_level|lower }}">{{ rec.confidence_level }}</span>
            </div>
            {% if rec.course_fit_reasoning %}
              <div class="rationale">{{ rec.course_fit_reasoning }}</div>
            {% endif %}
            {% if current_event %}
              <button class="lock"
                      data-name="{{ rec.name|e }}"
                      data-cid="{{ rec.canonical_id }}"
                      data-eid="{{ current_event.canonical_event_id }}">
                Lock In Pick
              </button>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    {% else %}
      <div class="card empty">
        No scheduled event in the ledger yet —
        run <code>python -m pipeline.monday_open</code> to populate one.
      </div>
    {% endif %}
  </section>

  <section class="section grid two">
    <div>
      <h2>Upcoming</h2>
      <div class="grid events">
        {% for event in upcoming_events %}
          <div class="event-card{% if event.type == 'MAJOR' %} major{% endif %}">
            <h3>{{ event.name }}</h3>
            <div class="meta">
              {{ event.dates }}<br>
              {{ event.venue }}<br>
              <span class="purse">{{ event.purse }}</span> · {{ event.type|title }}
            </div>
          </div>
        {% endfor %}
      </div>
    </div>
    <div>
      <h2>Majors</h2>
      <div class="card">
        {% for major in major_tracker %}
          <div class="major-row{% if not major.pick_made %} tbd{% endif %}">
            <span class="name">{{ major.name }}</span>
            <span class="major-status{% if major.pick_made %} made{% endif %}">
              {% if major.pick_made %}
                {% if major.finish_position %}
                  finish {{ major.finish_position }}{% if major.score_to_par is not none %}, {% if major.score_to_par >= 0 %}+{% endif %}{{ major.score_to_par }}{% endif %}
                {% else %}picked, awaiting result{% endif %}
              {% else %}pick TBD{% endif %}
            </span>
          </div>
        {% endfor %}
      </div>
    </div>
  </section>

  <section class="section">
    <h2>Available Players<span class="sub">— top {{ available_players|length }} of {{ available_total }} by OWGR</span></h2>
    <div class="card flush">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Tier</th>
            <th class="num">OWGR</th>
            <th class="num">Vegas</th>
            <th class="num">Implied</th>
          </tr>
        </thead>
        <tbody>
          {% for player in available_players %}
            <tr class="{{ player.tier }}">
              <td>{{ player.name }}</td>
              <td>{{ player.tier|title }}</td>
              <td class="num">{{ player.owgr if player.owgr else '—' }}</td>
              <td class="num">{{ player.vegas_odds }}</td>
              <td class="num">{% if player.implied_win_pct %}{{ "%.1f"|format(player.implied_win_pct) }}%{% else %}—{% endif %}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </section>

  <section class="section">
    <h2>Pick History <span class="sub">— current season</span></h2>
    {% if pick_history %}
      <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Event</th>
            <th>Date Picked</th>
            <th>Player</th>
            <th class="num">OWGR</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {% for pick in pick_history %}
            <tr class="{% if '✅' in pick.result %}win{% endif %}">
              <td>{{ pick.event }}</td>
              <td>{{ pick.date_picked }}</td>
              <td>{{ pick.player }}</td>
              <td class="num">{{ pick.owgr if pick.owgr != 'N/A' else '—' }}</td>
              <td>{{ pick.result }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>
    {% else %}
      <p style="color:var(--text-muted);">No picks recorded yet this season.</p>
    {% endif %}
  </section>

</div>

<script>
  // Refresh every 5 minutes
  setInterval(() => location.reload(), 300000);

  // Wire up Lock In Pick buttons
  document.querySelectorAll('button.lock').forEach(btn => {
    btn.addEventListener('click', async () => {
      const { name, cid, eid } = btn.dataset;
      if (!confirm(`Lock in ${name}?`)) return;
      btn.disabled = true;
      btn.textContent = 'Locking…';
      try {
        const r = await fetch('/pick', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ canonical_id: cid, canonical_event_id: eid })
        });
        const data = await r.json();
        if (r.ok) {
          alert(`✓ ${name} locked in.`);
          location.reload();
        } else {
          alert(`Failed: ${data.error || r.statusText}`);
          btn.disabled = false;
          btn.textContent = 'Lock In Pick';
        }
      } catch (e) {
        alert(`Network error: ${e.message}`);
        btn.disabled = false;
        btn.textContent = 'Lock In Pick';
      }
    });
  });
</script>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    all_players = get_available_players()
    available_top = all_players[:25]
    return render_template_string(
        HTML_TEMPLATE,
        available_players=available_top,
        available_total=len(all_players),
        recommendations=get_recommendations(top_n=5),
        season_summary=get_season_summary(),
        major_tracker=get_major_tracker(),
        upcoming_events=get_upcoming_events(),
        current_event=get_current_event_info(),
        pick_history=get_pick_history(),
        font=CSS_FONT,
        **CSS_COLORS,
    )


@app.route("/pick", methods=["POST"])
def lock_pick():
    """Commit a pick from the dashboard. Mirrors record_pick.py's guardrails:
    refuses if the player is already burned or if a pick already exists for
    the event (use record_pick.py --replace to override)."""
    payload = request.get_json(silent=True) or {}
    cid = payload.get("canonical_id")
    eid = payload.get("canonical_event_id")
    if not cid or not eid:
        return jsonify({"error": "missing canonical_id or canonical_event_id"}), 400

    ledger = Ledger(DB_PATH)
    try:
        existing = ledger.get_pick_for_event(eid)
        if existing and not existing["voided"]:
            return jsonify({
                "error": "A pick already exists for this event. "
                         "Use `python record_pick.py --replace` to override."
            }), 409
        if cid in ledger.burned_player_ids(SEASON):
            return jsonify({"error": "Player has already been used this season."}), 409
        try:
            ledger.record_pick(
                season=SEASON,
                canonical_event_id=eid,
                canonical_player_id=cid,
                agent_confidence="WEB",
                agent_rationale="Locked via dashboard",
            )
        except sqlite3.IntegrityError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"ok": True})
    finally:
        ledger.close()


if __name__ == "__main__":
    debug = os.environ.get("QUIPU_DEBUG", "0") == "1"
    host = os.environ.get("QUIPU_HOST", "127.0.0.1")
    app.run(host=host, port=7071, debug=debug, use_reloader=debug)