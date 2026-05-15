# golf_dashboard.py

from flask import Flask, render_template_string, jsonify
import json
import os
import sqlite3
from datetime import date, datetime

from normalize.events import CANONICAL_EVENTS, get_upcoming
from normalize.players import get_all_available
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

# CXL brand colors and font
CSS_COLORS = {
    "bg": "#222126",
    "text": "#FFFEF8",
    "accent": "#E6FD76",
    "green": "#24483A",
    "blue": "#01455B",
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
        "skins_pot": pot,
        "skins_pot_size": pot,  # template expects both keys
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
    return out


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
    <title>Golf Dashboard</title>
    <style>
        body {
            background-color: {{ bg }};
            color: {{ text }};
            font-family: {{ font }};
            margin: 0;
            padding: 20px;
        }
        
        h1, h2 {
            color: {{ accent }};
        }

        .container { max-width: 100%; margin-bottom: 2rem; }
        
        .card {
            background-color: {{ bg }};
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 20px;
        }

        .section-title {
            border-left: 5px solid {{ accent }};
            padding-left: 10px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }

        th, td {
            padding: 0.75rem;
            text-align: left;
        }

        th {
            background-color: {{ green }};
            color: white;
        }
        
        tr:nth-child(even) {
            background-color: {{ blue }};
        }

        .progress-bar-container {
            width: 100%;
            background-color: #4d4c52;
            border-radius: 8px;
            overflow: hidden;
        }

        .progress-bar {
            height: 20px;
            background-color: {{ accent }};
            width: {% if season_summary.events_completed and season_summary.total_events %}{{ (season_summary.events_completed / season_summary.total_events) * 100 }}{% else %}5%{% endif %};
        }

        .performance-band {
            padding: 0.2rem;
            margin-bottom: 5px;
        }

        .bust { background-color: #ff4d4d; }
        .modest-win { background-color: {{ accent }}; color: black; }
        .big-win { background-color: #4caf50; }

        .confidence-badge {
            padding: 3px;
            border-radius: 4px;
            font-weight: bold;
        }

        .high { background-color: #2196F3; color: white; }
        .med { background-color: {{ accent }}; color: black; }
        .low { background-color: #f44336; color: white; }

        .event-card {
            border-left: 5px solid {{ blue }};
            padding: 10px;
        }

        .major { border-left-color: gold; }
        
    </style>
</head>

<body>
<div class="container">
    
<h1>Golf Dashboard - Chad's Fantasy Pool</h1>

<!-- Season Overview -->
<div class="card">
    <div class="section-title"><h2>Season Overview</h2></div>
    
    <p><strong>Total Winnings:</strong> ${{ "%.2f" % season_summary.total_winnings }}</p>
    
    <div class="progress-bar-container">
        <div class="progress-bar"></div>
    </div>

    <p>Events Completed: {{ season_summary.events_completed }} / 31</p>
    
    <div class="performance-band bust">Bust ($0) - Red band</div>
    <div class="performance-band modest-win">Modest Win ($100-$500) - Yellow band</div>
    <div class="performance-band big-win">Big Win ($500+) - Green band</div>
    
    <p><strong>Skins Pot:</strong> ${{ "%.2f" % season_summary.skins_pot_size }}</p>
</div>

<!-- Available Golfers -->
<div class="card">
    <h2>Available Players</h2>
    
    <table id="available-golfers">
        <thead><tr>
            <th>Name</th>
            <th>Tier</th>
            <th>OWGR</th>
            <th>Vegas Odds</th>
            <th>Implied Win %</th>
            <th>Recent Form</th>
        </tr></thead>
        
        {% for player in available_players %}
            <tr{% if not player.burned %} style="background-color: {{ accent }}; color:black;"{% endif %}>
                <td>{{ player.name }}</td>
                <td>{{ player.tier.title() }}</td>
                <td>#{{ player.owgr }}</td>
                <td>{{ player.vegas_odds }}</td>
                <td>~{{ "%.1f" % (player.implied_win_pct) }}%</td>
                <td>{{ player.recent_form }}</td>
            </tr>
        {% endfor %}
    </table>

</div>


<!-- Recommendations -->
<div class="card">
    <h2>Recommendations for Current Tournament</h2>
    
    {% if recommendations %}
        {% for rec in recommendations %}
            <div class="card" style="margin-bottom: 10px;">
                <h3>{{ rec.name }}</h3>
                
                <p><strong>Composite Score:</strong> {{ rec.composite_score }}/100</p>
                
                <div style="margin-bottom: 5px;">
                    {% for score in rec.score_breakdown %}
                        <span style="display:inline-block; width: 20px;">{{ score }}</span>
                    {% endfor %}
                </div>

                <p><strong>Vegas Odds:</strong> {{ rec.vegas_odds }} | 
                   <strong>Implied Win %:</strong> {{ "%.1f" % rec.implied_win_pct }}%</p>
                
                <p><strong>Course Fit:</strong></p>
                <div style="margin-left: 20px;">{{ rec.course_fit_reasoning }}</div>
                
                {% if rec.opportunity_cost %}
                    <p><strong>Opportunity Cost:</strong></p>
                    <div style="margin-left: 20px;">{{ rec.opportunity_cost }}</div>
                {% endif %}
                
                <p><strong>Skins Value:</strong> ${{ "%.2f" % rec.skins_value_probability }} EV</p>
                
                <div class="confidence-badge {{ rec.confidence_level.lower() }}">{{ rec.confidence_level }}</div>
                
                <button onclick="confirmPick('{{ rec.name }}')">Lock In Pick</button>
            </div>  
        {% endfor %}
    {% else %}
        <p>No recommendations available.</p>
    {% endif %}

</div>

<!-- Upcoming Events -->
<div class="card">
    <h2>Upcoming Schedule</h2>
    
    {% for event in upcoming_events %}
        <div class="event-card{% if event.type == 'MAJOR' %} major{% endif %}">
            <h3>{{ event.name }}</h3>
            
            <p><strong>Dates:</strong> {{ event.dates }} | 
               <strong>Venue:</strong> {{ event.venue }}</p>
            
            <p><strong>Purse:</strong> {{ event.purse }} | 
               <strong>Type:</strong> {{ event.type }}</p>
            
            {% if loop.index == 1 %}
                <p><strong>Best Available Fit:</strong> (see recommendations above)</p>
            {% endif %}
        </div>
    {% endfor %}

</div>

<!-- Season Ledger -->
<div class="card">
    <h2>Season History (Ledger)</h2>
    
    <!-- Placeholder for ledger table -->
</div>

<!-- Major Tracker -->
<div class="card">
    <h2>Major Contest Tracking (Score-to-Par)</h2>
    
    {% for major in major_tracker %}
        <div class="card" style="margin-bottom: 10px;">
            {% if major.pick_made %}
                <h3>{{ major.name }} - Pick Made</h3>
            {% else %}
                <h3 style="color: red;">{{ major.name }} - Pick TBD</h3>
            {% endif %}
            
            <p><strong>Finish Position:</strong> {{ major.finish_position or "N/A" }}</p>
            <p><strong>Winnings:</strong> ${{ "%.0f" % major.winnings if major.winnings else 0 }}</p>
        </div>
    {% endfor %}
</div>

<!-- Skins Tracker -->
<div class="card">
    <h2>Skins Pool Tracking</h2>
    
    <p><strong>Current Pot Size:</strong> ${{ "%.2f" % season_summary.skins_pot_size }}</p>
    <p><strong>Entries:</strong> 12</p>

    <!-- Placeholder for skins history -->
    
    <div style="display: flex; justify-content: space-between;">
        <p><strong>Skins Remaining:</strong> 5</p>
        <p><strong>Skins Collected:</strong> 7</p>
    </div>

</div>


<script type="text/javascript">
// Auto-refresh every 5 minutes (300,000 milliseconds)
setInterval(function() {
    location.reload();
}, 300000);

function confirmPick(playerName) {
    if (confirm("Are you sure you want to lock in " + playerName + "?")) {
        alert(playerName + "'s pick has been confirmed.");
    }
}
</script>

</div>
</body>
</html>"""


@app.route("/")
def dashboard():
    # Fetch data
    available_players = get_available_players()
    recommendations = get_recommendations(top_n=5)
    
    season_summary = get_season_summary()
    major_tracker = get_major_tracker() 
    upcoming_events = get_upcoming_events()

    # Render template with data
    return render_template_string(
        HTML_TEMPLATE,
        available_players=available_players,
        recommendations=recommendations,
        season_summary=season_summary,
        major_tracker=major_tracker,
        upcoming_events=upcoming_events
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7071, debug=True)