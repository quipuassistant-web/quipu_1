# quipu_1 — Golf One-&-Done Optimizer

PGA Tour pick-one-and-done fantasy golf optimizer. Pulls live field + leaderboard from ESPN, scores players against a multi-objective model (season earnings, majors, cuts, skins EV), and tracks picks/results in a single SQLite ledger.

## What it does

- **Fetches** — ESPN scoreboard + per-event summary (live). Vegas lines come in via `add_odds.py` paste-in. Weather from Open-Meteo (free, no key). OWGR rankings from manually-downloaded weekly PDFs.
- **Normalizes** — Player identities across data sources via OWGR seed + fuzzy name matching, with a manual-review queue for ambiguous cases.
- **Scores** — Probability cascade (DataGolf → Vegas → OWGR → ESPN form → flat prior) feeding a four-objective composite: season earnings, majors score-to-par, cuts made, weekly skins EV (Vegas-weighted pool-share).
- **Backfills** — Multi-season historical leaderboards for course-history modeling. Refuses to write when ESPN returns incomplete data.
- **Pipelines** — Monday open → Thursday lock → Sunday close, each with `--espn-event-id` override.
- **Ledger** — Picks, events, fields, results, skins, OWGR rankings — all in one SQLite DB. Voided picks preserved for audit (partial unique indexes).
- **Dashboard** — Flask web UI on `:7071`. Live recommendations + one-click pick locking.
- **Backtester** — Walk-forward simulator that compares the agent's strategy against vegas / owgr / random baselines across the four payout buckets.

## Setup

```bash
git clone https://github.com/quipuassistant-web/quipu_1.git
cd quipu_1

python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`rapidfuzz` is required for partial-name fuzzy matching. `pdfplumber` is required to import OWGR weekly PDFs.

Set pool config via env vars (defaults shown match Chad's 12-entry pool):

```bash
export QUIPU_DB=data/golf.db           # SQLite path
export QUIPU_SEASON=2026               # default: current year
export QUIPU_POOL_ENTRIES=12           # total entries in your pool
export QUIPU_WEEKLY_SKINS=372.0        # $ per week into the skins pot
```

Every CLI honors these.

## Quick start

```bash
# Mid-season cold start: backfill events + season form (network — talks to ESPN)
python backfill.py --from 2026-01-01

# Import existing pick history from CHAD_PICKS_2026 in import_picks.py
python import_picks.py

# Open this week's event (resolves field, surfaces unresolved players)
python -m pipeline.monday_open

# Paste Vegas lines for the scorer
python add_odds.py --event-id e_401609513 --book draftkings

# Refresh OWGR ranks (drop the latest weekly PDF from owgr.com/archive)
python -m normalize.owgr_live --import data/owgr/owgr20-2026.pdf

# Score + print top recommendations
python -m pipeline.thursday_lock

# Lock a pick from the CLI (auto-detects upcoming event)
python record_pick.py --player "Rory McIlroy"

# Or from the dashboard
python golf_dashboard.py
# http://127.0.0.1:7071/

# After the event: ingest results
python -m pipeline.sunday_close --winners-picked 2

# Evaluate the scorer against baselines over a past season
python -m evaluation.backtester --season 2025 --strategies agent,vegas,owgr,random
```

## Project structure

```
quipu_1/
├── normalize/         Crosswalk (players.py), seed.py, OWGR live import,
│                      course metadata loader, canonical events
├── fetchers/          ESPN client + Open-Meteo weather
├── ledger/ledger.py   Picks, events, fields, results, skins, owgr_rankings;
│                      auto-migrates picks UNIQUE constraint on first open
├── scoring/           inputs.py + multi_objective.py + skins_ev.py + allocation.py
├── pipeline/          monday_open.py / thursday_lock.py / sunday_close.py
├── evaluation/        backtester.py — walk-forward strategy comparison
├── cache/             SQLite TTL cache for HTTP
├── data/              golf.db (gitignored), course_metadata.json, owgr/ PDFs
├── backfill.py        Week-by-week ESPN walk → ledger
├── multi_season_backfill.py  Wrapper for multi-season historical fills
├── import_picks.py    Bootstrap ledger from a hand-typed pick list (idempotent)
├── add_odds.py        Paste-in Vegas odds → event_odds
├── record_pick.py     Thursday CLI to commit a pick (--replace preserves audit)
├── course_history.py  Per-player per-venue shrinkage estimator (+ CLI)
├── golf_dashboard.py  Flask UI on :7071
└── test_*.py          Smoke tests, not pytest-discovered
```

`data/golf.db` is created on first run and holds everything (players, picks, events, owgr_rankings, http_cache, event_odds). `data/course_metadata.json` (per-event lat/lon, course_type, grass, is_major flags) ships with the repo.

## Docs & references

- [DataGolf API docs](https://datagolf.com/api) — paid; the disabled `fetchers/datagolf.py` would slot in here
- [ESPN scoreboard endpoint](https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard) — unauthenticated
- [Open-Meteo](https://open-meteo.com) — free weather API (no key)
- [OWGR archive](https://www.owgr.com/archive) — download weekly PDFs from here
- [`CLAUDE.md`](./CLAUDE.md) — agent-facing notes on architecture, design decisions, and known fragilities.
