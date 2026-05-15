# quipu_1 — Golf One-&-Done Optimizer

PGA Tour pick-one-and-done fantasy golf optimizer. Pulls live field + leaderboard from ESPN, scores players against a multi-objective model (season earnings, majors, cuts, skins EV), and tracks picks/results in a single SQLite ledger.

## What it does

- **Fetches** — ESPN scoreboard + per-player eventlog (live). DataGolf and sportsbook scrapers are present but disabled (ToS / anti-bot); Vegas lines come in via `add_odds.py` paste-in.
- **Normalizes** — Player identities across data sources via OWGR seed + fuzzy name matching, with a manual-review queue for ambiguous cases.
- **Scores** — Probability cascade (DataGolf → Vegas → ESPN form → flat prior) feeding a four-objective composite: season earnings, majors score-to-par, cuts made, weekly skins EV.
- **Backfills** — Multi-season historical leaderboards for course-history modeling.
- **Pipelines** — Monday open → Thursday lock → Sunday close.
- **Ledger** — Picks, events, fields, results, skins state — all in one SQLite DB.
- **Dashboard** — Flask web UI on `:7071`. Live recommendations + one-click pick locking.

## Setup

```bash
git clone https://github.com/quipuassistant-web/quipu_1.git
cd quipu_1

python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Seed the player crosswalk (210 players from OWGR + supplemental)
python normalize/seed.py
```

`rapidfuzz` is required for partial-name fuzzy matching (e.g. resolving `"Poston"` → `"J.T. Poston"`).

## Quick start

```bash
# Mid-season cold start: backfill events + season form (network — talks to ESPN)
python backfill.py --from 2026-01-01 --season 2026

# Import existing pick history from CHAD_PICKS_2026 in import_picks.py
python import_picks.py

# Open this week's event (resolve field, surface unresolved players)
python -m pipeline.monday_open

# Paste Vegas lines for the scorer
python add_odds.py --event-id e_401609513 --book draftkings

# Score + print top recommendations
python -m pipeline.thursday_lock

# Lock a pick from the CLI (auto-detects upcoming event)
python record_pick.py --player "Rory McIlroy"

# Or from the dashboard
python golf_dashboard.py
# http://127.0.0.1:7071/
```

## Project structure

```
quipu_1/
├── normalize/         Crosswalk (players.py), seed.py, OWGR list, canonical events
├── fetchers/          ESPN client (espn.py); datagolf.py / odds.py disabled
├── ledger/ledger.py   Picks, events, fields, results, skins, season_results
├── scoring/           inputs.py + multi_objective.py + skins_ev.py + allocation.py
├── pipeline/          monday_open.py / thursday_lock.py / sunday_close.py
├── cache/             SQLite TTL cache for HTTP
├── backfill.py        Week-by-week ESPN walk → ledger
├── import_picks.py    Bootstrap ledger from a hand-typed pick list
├── add_odds.py        Paste-in Vegas odds → event_odds
├── record_pick.py     Thursday CLI to commit a pick
├── course_history.py  Per-player per-venue shrinkage estimator
├── golf_dashboard.py  Flask UI on :7071 (read-only views + POST /pick)
└── test_*.py          Smoke tests, not pytest-discovered
```

`data/golf.db` is created on first run and holds everything (players, picks, events, http_cache, event_odds).

See `.env.example` for optional `QUIPU_*` config (DB path, season, pool size, etc.).

## Docs & references

- [DataGolf API docs](https://datagolf.com/api)
- [ESPN API overview](https://github.com/ew-misc/espn-api)
- [OWGR rankings](https://www.owgr.com)
- [`CLAUDE.md`](./CLAUDE.md) — agent-facing notes on architecture, design decisions, and known fragilities.
