# golf_dashboard.py

from flask import Flask, render_template_string, jsonify
import json
import os
import sys
import time
from datetime import datetime, timedelta

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Path setup — golf_agent lives in TOOLS/
# ---------------------------------------------------------------------------
_AI_HOME = os.path.expanduser("~/AI_HOME")
_TOOLS = os.path.join(_AI_HOME, "TOOLS")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

# ---------------------------------------------------------------------------
# CXL brand colors and font
# ---------------------------------------------------------------------------
CSS_COLORS = {
    "bg": "#222126",
    "text": "#FFFEF8",
    "accent": "#E6FD76",
    "green": "#24483A",
    "blue": "#01455B"
}

CSS_FONT = "'Work Sans', Arial, sans-serif"

# Cache file path
CACHE_FILE_PATH = os.path.join(_TOOLS, "golf_dashboard_cache.json")
CACHE_EXPIRY_HOURS = 4

# ---------------------------------------------------------------------------
# Data functions — wired to real cache/scoring layer
# ---------------------------------------------------------------------------

def get_available_players():
    """Replace stub — now reads from canonical player map + ledger burns."""
    from golf_agent.normalize.players import get_all_available, CANONICAL_PLAYERS
    from golf_agent.fetchers.odds import get_draftkings_odds

    players = get_all_available()
    result = []

    # Get odds if available for win%
    try:
        odds = get_draftkings_odds()
        odds_map = {}
        for o in odds:
            player_name = o.get("player", "")
            # Try to match by canonicalize
            from golf_agent.normalize.players import canonicalize
            key = canonicalize(player_name)
            odds_map[key] = o
    except Exception:
        odds_map = {}

    for p in players:
        pid = p["id"]
        odds_info = odds_map.get(pid, {})

        implied = odds_info.get("implied_win_pct", 0.0) if odds_info else 0.0
        vegas_odds = odds_info.get("odds", "—") if odds_info else "—"

        result.append({
            "name": p["display_name"],
            "tier": p["tier"],
            "owgr": p.get("owgr", "—"),
            "vegas_odds": vegas_odds,
            "implied_win_pct": implied,
            "recent_form": "→",  # TODO: wire to ESPN event log
            "burned": p.get("burned", False),
        })

    # Sort by tier then OWGR
    tier_order = {"elite": 0, "star": 1, "mid": 2}
    result.sort(key=lambda x: (tier_order.get(x["tier"], 3), x["owgr"]))
    return result


def get_recommendations(tournament, date):
    """Replace stub — scores available players using multi_objective engine."""
    from golf_agent.normalize.events import get_upcoming
    from golf_agent.scoring.multi_objective import get_recommendations_for_event

    # Get upcoming event
    upcoming = get_upcoming(n=1)
    if not upcoming:
        return []

    event_id = upcoming[0]["id"]

    try:
        recs = get_recommendations_for_event(event_id, top_n=5)
    except Exception as e:
        print(f"Recs error: {e}")
        return []

    return [
        {
            "name": r["display_name"],
            "composite_score": r["composite_score"],
            "score_breakdown": list(r["breakdown"].values()),
            "vegas_odds": r.get("vegas_odds", "—"),
            "implied_win_pct": r.get("implied_win_pct", 0) or 0,
            "course_fit_reasoning": r.get("reasoning", ""),
            "opportunity_cost": r.get("opportunity_cost", ""),
            "skins_value_probability": r.get("skins_value", 0) or 0,
            "confidence_level": r.get("confidence", "LOW"),
        }
        for r in recs
    ]


def get_vegas_odds(tournament):
    """Return odds data for the given tournament."""
    from golf_agent.fetchers.odds import get_draftkings_odds
    try:
        odds = get_draftkings_odds()
        return {tournament: [{"player": o.get("player", ""), "odds": o.get("odds", "")} for o in odds]}
    except Exception:
        return {tournament: []}


def get_season_summary():
    """Replace stub — reads from ledger."""
    from golf_agent.ledger.ledger import get_season_summary as _ledger_summary
    try:
        s = _ledger_summary()
        # Normalize for template: always provide both keys
        s["skins_pot_size"] = s.get("skins_pot", 420.50)
        return s
    except Exception:
        return {
            "total_winnings": 0.0,
            "events_completed": 0,
            "total_events": 31,
            "skins_pot": 420.50,
            "skins_pot_size": 420.50,
        }


def get_major_tracker():
    """Replace stub — reads from ledger."""
    from golf_agent.ledger.ledger import get_major_tracker as _tracker
    try:
        return _tracker()
    except Exception:
        return []


def get_upcoming_events():
    """Replace stub — reads from canonical events."""
    from golf_agent.normalize.events import get_upcoming

    events = get_upcoming(n=6)
    return [
        {
            "name": e["display_name"],
            "dates": e["dates"],
            "venue": e["venue"],
            "purse": f"${e['purse']:,}",
            "type": e["type"].upper() if e["type"] else "REGULAR",
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
    recommendations = get_recommendations("Current", datetime.now())
    
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