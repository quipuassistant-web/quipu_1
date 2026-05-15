# quipu_1 — Golf One-&-Done Optimizer

 PGA Tour pick-one-and-done fantasy golf optimizer. Fetches live odds, player stats, and course history to generate optimal weekly selections across multiple scoring modes.

## What it does

- **Fetches** — DataGolF, ESPN, OddsTrader, and Weather APIs
- **Normalizes** — Player identities across data sources via OWGR seed + fuzzy name matching
- **Scores** — Multi-objective allocation: Majors, Signature Events, regular Ferts, + ante-post skins EV
- **Backfills** — Multi-season historical performance by tournament for course-history modeling
- **Pipelines** — Monday open → Thursday lock → Sunday close workflow automation
- **Ledger** — Tracks picks, results, and bankroll across the season
- **Dashboard** — Web UI for live standings and pick tracking

## Setup

```bash
# Clone
git clone https://github.com/quipuassistant-web/quipu_1.git
cd quipu_1

# Python env (3.10+)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Environment variables
cp .env.example .env
# Add your API keys — see .env.example for the full list

# Initialize the player database
python normalize/seed.py
```

## Quick start

```bash
# Run the weekly pipeline
python pipeline/monday_open.py

# Check picks in the dashboard
python golf_dashboard.py

# Record a pick
python record_pick.py --event "PGA Championship" --player "Rory McIlroy"
```

## Project structure

```
golf_agent/
├── fetchers/          # API clients (datagolf, espn, odds, weather)
│   └── normalize/     # Player cross-reference database
├── normalize/         # Player/name normalization & OWGR seeding
├── scoring/           # Multi-objective allocation engine
├── pipeline/          # Weekly automation scripts
│   ├── monday_open.py
│   ├── thursday_lock.py
│   └── sunday_close.py
├── cache/             # HTTP response cache
├── ledger/            # Pick history & bankroll tracker
├── data/              # SQLite tournament data
└── course_history.py   # Course-fit modeling

golf_dashboard.py       # Flask web UI (port 7071)
```

## Environment variables

| Variable | Description |
|---|---|
| `DATAGOLF_API_KEY` | DataGolF API token |
| `ESPN_API_KEY` | ESPN fantasy API key |
| `ODDSTRADER_API_KEY` | OddsTrader API key |
| `GMAIL_APP_PASSWORD` | Gmail app password (ledger email alerts) |
| `GMAIL_FROM` | Sender email address |

See `.env.example` for the full list.

## Docs & references

- [DataGolF API docs](https://datagolf.com/api)
- [ESPN API overview](https://github.com/ew-misc/espn-api)
- [OWGR rankings](https://www.owgr.com)