# quipu_1 — agent notes

PGA Tour pick-one-and-done optimizer for Chad's 12-entry pool. This file is for you (the agent), not for end users. Read the README for the user-facing pitch; read this for the wiring, the rough edges, and the design decisions that aren't obvious from the code.

## Run model

- Python 3.10+ (the code uses runtime generics like `list[dict]`). `requirements.txt`: `Flask`, `requests`, `beautifulsoup4`, `rapidfuzz`, `pdfplumber`. `rapidfuzz` is required (the naive Jaccard fallback in `normalize/players.py` scores partial inputs below the 0.70 queue threshold and is too strict). `pdfplumber` is only needed to import OWGR weekly PDFs.
- Run from the repo root. Internal imports are bare (`from normalize.players import ...`, `from ledger.ledger import ...`, `from fetchers.espn import ...`), so `cwd == /Users/chad/Projects/quipu_1` and that path must be on `sys.path`. There is a top-level `__init__.py` but the package is NOT pip-installable. Scripts inside `pipeline/` need `python -m pipeline.monday_open` (bare `python pipeline/monday_open.py` form will fail import).
- **Env vars** (now used by every CLI — dashboard, monday_open, thursday_lock, sunday_close, record_pick, backfill, import_picks, evaluation/backtester):
  - `QUIPU_DB` — DB path (default `data/golf.db`)
  - `QUIPU_SEASON` — season year (default: current year)
  - `QUIPU_POOL_ENTRIES`, `QUIPU_WEEKLY_SKINS`, `QUIPU_TOTAL_EVENTS` — Chad's pool config (defaults 12, 372.0, 31)
  - `QUIPU_LOG` — optional log file path for `thursday_lock`
- Default SQLite DB: `data/golf.db` (relative — created on first run). All tables (`players`, `player_aliases`, `player_source_ids`, `players_unresolved`, `events`, `picks`, `skins`, `event_field`, `season_results`, `event_odds`, `owgr_rankings`, `http_cache`) live in this single file by design.
- ESPN is unauthenticated. Open-Meteo (weather) is unauthenticated. OWGR live data comes from manually-downloaded PDFs (no API), parsed via pdfplumber.

## Layout

```
quipu_1/                          <- repo root; cwd for every script
├── normalize/                    player crosswalk + canonical events + metadata
│   ├── players.py                PlayerCrosswalk + ensure_seeded() public helper
│   ├── seed.py                   seed crosswalk from OWGR_TOP_200 + SUPPLEMENTAL
│   ├── seed_owgr.py              static OWGR top-200 snapshot w/ ESPN ids
│   ├── owgr_live.py              import weekly OWGR PDFs into owgr_rankings table
│   ├── course_metadata.py        loader for data/course_metadata.json (is_major etc.)
│   ├── events.py                 hardcoded CANONICAL_EVENTS dict (display-only)
│   └── join.py
├── fetchers/
│   ├── espn.py                   primary live source — scoreboard, summary, ranges
│   ├── weather.py                Open-Meteo (free, no key) + venue→latlon map
│   ├── datagolf.py               DISABLED (ToS); needs paid API key to revive
│   └── odds.py                   DISABLED (anti-bot); use add_odds.py paste-in
├── ledger/ledger.py              Ledger class — picks, events, skins, field, results
│                                 + auto-migration of picks UNIQUE constraint
├── scoring/
│   ├── inputs.py                 build_event_inputs — hydrates PlayerInputs + EventInputs
│   ├── multi_objective.py        score_field — probability cascade + composite
│   ├── skins_ev.py               estimate_pool_picks + odds helpers
│   └── allocation.py             multi-event allocation planner (used by test_scorer only)
├── pipeline/
│   ├── monday_open.py            resolve field, persist event_field, surface unresolved
│   ├── thursday_lock.py          final rec print using score_field + Ledger
│   └── sunday_close.py           resolve picks, update skins pot, write results
├── evaluation/
│   └── backtester.py             walk-forward 4-bucket strategy comparison
├── cache/cache.py                SQLite TTL cache (Cache class + module-level helpers)
├── data/
│   ├── golf.db                   the one DB (gitignored)
│   ├── course_metadata.json      per-event lat/lon, course_type, grass, is_major, etc.
│   └── owgr/                     drop weekly PDFs here for owgr_live --import
├── backfill.py                   week-by-week ESPN walk → season_results
├── multi_season_backfill.py      wrapper around backfill_range for 3-4 seasons
├── course_history.py             per-player per-venue shrinkage estimator (+ CLI)
├── add_odds.py                   paste-in Vegas odds → event_odds table
├── import_picks.py               bootstrap ledger from CHAD_PICKS_2026 (idempotent)
├── record_pick.py                Thursday CLI: lookup → confirm → insert into picks
├── golf_dashboard.py             Flask UI on :7071 (reads ledger directly)
└── test_*.py                     smoke tests, not pytest-discovered
```

## Where the source of truth lives

- **Who has been picked**: `picks` table. Two partial unique indexes enforce one-active-pick-per-event and one-and-done-per-player-season — both only on `voided = 0` rows. Voided picks (replacements, WD-restored) stay for audit but don't block re-use. Old DBs with the unconditional `UNIQUE (canonical_event_id)` constraint auto-migrate on first `Ledger.__init__`.
- **What the field is**: `event_field` table, written by `monday_open.run_monday_open`. `build_event_inputs` falls back to deriving the field from `season_results` when `event_field` is empty (backfill case, useful for backtests).
- **Per-player season form**: `season_results`, written by `backfill.py` (NOT by `sunday_close` — design gap worth noting). `scoring/inputs._hydrate_season_form` reads from here. Honors `as_of_date` to prevent backtest leakage.
- **Vegas odds**: `event_odds` (de-vigged at insert time by `add_odds.py`). Scorer auto-loads via `load_odds_for_event` if caller doesn't pass `vegas_odds=` explicitly.
- **OWGR rankings**: `owgr_rankings` table (written by `normalize.owgr_live --import path/to/owgr.pdf`). `_owgr_rank_map` overlays live data on top of the `seed_owgr.py` static snapshot.
- **Skins pot state**: `skins` table. `Ledger.current_skins_pot` computes `weekly_contribution × (1 + rolled_events)`.

## Design decisions worth keeping in mind

### Crosswalk

- **Append-only.** `players.py` never auto-merges or auto-deletes canonical records. Wrong merges propagate silently for weeks; manual review of the unresolved queue is the safety valve.
- **Resolve order**: source_id → exact normalized alias → fuzzy ≥ 0.95 with ≥ 0.05 gap to runner-up → queue. `AUTO_RESOLVE_MIN = 0.95`, `QUEUE_MIN = 0.70`. The 0.05 gap rule is what prevents Cameron Young / Cameron Smith from collapsing.
- **Name normalization is deliberately lossy**: diacritics stripped via NFKD + explicit substitutions for atomic codepoints (`ø→o`, `æ→ae`, `ß→ss`, `ł→l`, etc. — NFKD doesn't decompose those), `Last, First` flipped, initials separated, generation suffixes (`Jr`/`III`) stripped.
- **Nickname expansion is comparison-only.** Never store nickname-expanded aliases — keep "Cam" and "Cameron" distinct, or the system loses its ability to detect their collision later. The boost to 0.97 when expanded forms match AND last names match makes "Cam Young" resolve to "Cameron Young" without lowering the floor for other fuzzies.
- **Birth year is the disambiguator of last resort** (Tom Kim vs Si Woo Kim, Charlie vs Tiger). Stored on the canonical row; not yet used in `similarity()` — TODO for harder ambiguous cases.
- **`ensure_seeded(xw)`** is the public helper that seeds an empty crosswalk from OWGR top 200. Every pipeline CLI calls it on init.

### Scoring

- **Probability cascade** in `_base_finish_probs`: DataGolf finish distribution → Vegas implied (de-vigged) → **OWGR rank** (log-decay calibrated against typical implied odds; rank 1 ≈ 10%, rank 50 ≈ 1.4%, floor 0.5%) → ESPN season form (only if `season_starts ≥ 3`) → flat prior (1/field_size). Each step records a `source` + `confidence` so the composite gets discounted appropriately.
- **Venue lift** is multiplicative on top of the base, applied to win/top5/top10/top20 in tandem (correlated). Cut prob gets a softer adjustment (cut-making is less venue-dependent). Lift is capped at [0.5, 2.0] and scaled by evidence: 0 starts → no adjustment, 5+ starts → full lift, linear in between.
- **Composite is normalized within field, then discounted by source confidence.** A flat-prior player can't out-score a Vegas-anchored player by virtue of normalization alone — `composite *= probs.confidence` is doing the work.
- **Majors weight redistributes to season earnings when `can_win_majors_bucket = False`** (you joined mid-season and missed a major; the majors bucket is unreachable so its weight goes back into the pile).
- **Skins EV is Vegas-weighted.** `_pool_pick_distribution` (in `multi_objective.py`) builds a per-player share-of-pool-picks via `estimate_pool_picks` (Vegas-implied → top-heavy weights). `co_pickers = pool_entries × share` replaces the old flat-heuristic `expected_winner_pickers × relative_chalk`. Chalk plays correctly split the pot more; longshots get a bigger slice when they hit.
- **Course history shrinkage**: `SHRINKAGE_ALPHA = 4` pseudo-starts of baseline blended before trusting the player's venue rate. Baselines: top-10 = 12%, top-20 = 20%, cut = 70%.
- **Weather** is plumbed into `EventInputs.weather` (Open-Meteo per-day summary) and surfaced in scorer rationale; not yet a model input. Future: wind-sensitive players, links-course wind adjustment.

### Pipelines

- Three discrete scripts, one per day, no daemon. **monday_open** is the only one that mutates `event_field`. **thursday_lock** is read-only — it scores and prints, doesn't write to `picks` (that's `record_pick.py`). **sunday_close** is idempotent — safe to re-run.
- All three honor `--espn-event-id` to override the auto-detected current event. Monday_open additionally has a `find_upcoming(reference_date)` fallback when the scoreboard returns a completed event (the just-finished tournament).
- `--espn-event-id` for scheduled events works because `ESPNFetcher.event_by_id` falls back to a `tournaments_in_range` scan when the `/summary` endpoint 404s.
- Sunday_close + backfill both refuse to write to `season_results` when ESPN returns a leaderboard with all-NULL positions (typical state when the `/summary` endpoint is 502-ing). Better to skip than corrupt the table the scorer reads.
- HTTP cache TTLs: 5 min during live play, 4 h during the current week with no play, 30 d for completed events. `backfill.py` against a cold cache is 5–15 minutes for 4 seasons; re-runs are cheap.

### Ledger

- **One DB by design** — `data/golf.db` holds the crosswalk, the ledger, the HTTP cache, the odds table, OWGR rankings, course metadata is JSON-on-disk. Backups are simpler; cross-table joins are SQL-local.
- **Voided picks are kept, not deleted.** The audit trail matters and the partial unique indexes handle the constraint logic. `record_pick --replace` calls `Ledger.replace_pick` which voids the existing row (reason="replaced via record_pick --replace") and inserts the new one in one transaction. Re-running `record_pick` against an existing pick fails with "use --replace to override."
- **Schema migration**: `_migrate_picks_unique_constraint` runs on every `Ledger.__init__`. Detects the old `UNIQUE (canonical_event_id)` table constraint and rebuilds the table with a partial unique index instead. Idempotent; no-op on fresh DBs.
- **`get_pick_for_event`** filters to `voided = 0` by default. Pass `include_voided=True` for audit-style access.

### Borrowed from gmalbert/golf-predictions

- `data/course_metadata.json` — their JSON file (MIT) under `data/`. `normalize/course_metadata.lookup()` is the loader. Used by `Ledger._looks_like_major` (replaces the fragile substring matching). NOTE: their lat/lon for rotating-major venues is locked to past years (e.g. PGA at Valhalla 2024); `fetchers/weather.py:VENUE_COORDS` has the current-year overrides.
- `normalize/owgr_live.py` — country-list backwards-match parser, adapted from their `scrapers/parse_owgr_pdfs_v3.py`. Their workflow uses Playwright + chromium to scrape OWGR PDFs; we use the lighter manual-download pattern (drop PDF into `data/owgr/`, run `python -m normalize.owgr_live --import path.pdf`).
- `evaluation/backtester.py` — walk-forward simulator inspired by their `evaluation/backtester.py`. Strategies: agent / vegas / owgr / random. 4-bucket comparison matching Chad's pool. Built-in caveats around OWGR snapshot leakage and the Chad-only skins pot reset are documented in the module docstring.

## Two event-ID schemes — know which to use where

`normalize/events.py` keys events by string slugs (`"pga_championship_2026"`). The ledger uses `e_<espn_event_id>` (`"e_401609513"`). These never converge — there is no translator and there shouldn't need to be one.

- **Display only** (CANONICAL_EVENTS slugs): hand-typed forward-looking schedule, will go stale, doesn't drive any state. Used by `get_major_tracker` (date-proximity matching against ledger rows) and by `import_picks.py` fuzzy matching.
- **Scoring + persistence**: ledger canonical ids. The dashboard's upcoming list pulls from the ledger first (where monday_open writes), tops up from CANONICAL_EVENTS only for slots the ledger hasn't seen yet.

## Disabled integrations (deliberate)

- **Sportsbook odds scrapers** (`fetchers/odds.py`) — anti-bot triggers were too brittle. Paste fresh lines via `add_odds.py` instead.
- **DataGolf scraper** (`fetchers/datagolf.py`) — scrapes the consumer site, ToS violation. To revive properly, subscribe to `feeds.datagolf.com` (~$270/yr) and rewrite to use the API key. The scorer already has a slot for `datagolf_finish_distribution` in `PlayerInputs`.
- **OWGR Playwright scraper** — gmalbert's approach. We chose the manual-PDF pattern to avoid the chromium dependency.

## Fragility notes

- **OWGR snapshot leakage in the backtester.** The current `_owgr_rank_map` returns whatever the most-recent OWGR import shows, regardless of the simulated event date. Means backtest "as of date X" still sees future rank changes. Address by filtering `owgr_rankings.week_ending ≤ as_of_date`.
- **Skins pot reset in the backtester** assumes Chad's pick winning is the only trigger. Doesn't simulate other pool entries' wins. Tends to over-credit strategies that land single wins late in the season.
- **Field-from-season_results fallback** in `build_event_inputs` is correct for backfilled past events but means the "field" you see for an in-progress event without `monday_open` having run will be empty. Always run monday_open first for live events.
- **rotating-major venue lat/lons in course_metadata.json** are year-locked to gmalbert's snapshot. `fetchers/weather.py:VENUE_COORDS` overrides for current-year venues.

## Things to NOT do

- Don't auto-resolve crosswalk entries below the 0.95 + gap thresholds. A wrong merge means a player gets double-burned silently.
- Don't write nickname-expanded names into `player_aliases`. Comparison-time only.
- Don't add new event IDs to `CANONICAL_EVENTS` and assume the scorer will use them — the scorer reads from the ledger's `events` table.
- Don't reintroduce sportsbook scrapers without re-checking ToS and anti-bot.
- Don't delete voided pick rows. The audit trail and partial-unique-index contract both depend on them.
- Don't reintroduce broad `except Exception: return []` swallows. Narrow them or re-raise after logging.
- Don't remove the partial unique indexes on `picks` without also removing `Ledger.replace_pick` — the void-and-insert flow depends on them.
- Don't bake pool-specific defaults (entry count, weekly contribution, current pot $) into modules. Read from `QUIPU_*` env vars.

## Useful entry points when working on this

- **Player ID issue?** Start at `normalize/players.py:430` (`PlayerCrosswalk.resolve`) and look at the test cases in `__main__` (line 603+).
- **Change scoring?** `scoring/multi_objective.py:363` (`score_field`) is the only public entry. Probability cascade is `_base_finish_probs` at line 125. The OWGR-cascade calibration constants are in that function.
- **Add a real data source?** Hydrate `PlayerInputs.datagolf_finish_distribution` (best signal) or `vegas_win_implied_pct` (next best) in `scoring/inputs.py:94`. Cascade picks it up automatically.
- **Validate scoring quality?** `python -m evaluation.backtester --season 2025 --strategies agent,owgr,random,vegas` once `season_results` is populated for 2025.
- **Debug a pipeline run end-to-end?** `python -m pipeline.monday_open --demo` runs a self-contained mock against a fresh DB. Good sanity check after any change to `players.py` or `ledger.py`.
- **Refresh OWGR ranks?** Download the latest weekly PDF from `owgr.com/archive`, drop into `data/owgr/`, run `python -m normalize.owgr_live --import data/owgr/<file>.pdf`.

## Environment

`.env.example` lists `DATAGOLF_API_KEY`, `ESPN_API_KEY`, `ODDSTRADER_API_KEY`, `GMAIL_APP_PASSWORD`, `GMAIL_FROM`. In practice ESPN doesn't need a key, DataGolf and OddsTrader are disabled, and the Gmail credentials are unused in current code. The `QUIPU_*` env vars in the run-model section above are the ones that actually matter day-to-day.
