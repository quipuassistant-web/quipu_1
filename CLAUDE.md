# quipu_1 — agent notes

PGA Tour pick-one-and-done optimizer for Chad's 12-entry pool. This file is for you (the agent), not for end users. Read the README for the user-facing pitch; read this for the wiring, the rough edges, and the design decisions that aren't obvious from the code.

## Run model

- Python 3.10+ (the code uses runtime generics like `list[dict]`). `requirements.txt` lists `Flask`, `requests`, `beautifulsoup4`, `rapidfuzz`. The crosswalk requires `rapidfuzz` for partial-name fuzzy matching (e.g. resolving `"Poston"` → `"J.T. Poston"`); there's a try/except fallback to naive Jaccard token-set in `normalize/players.py`, but that fallback scores partial inputs below the 0.70 queue threshold and is too strict for real use — install the real library.
- Run from the repo root. Internal imports are bare (`from normalize.players import ...`, `from ledger.ledger import ...`, `from fetchers.espn import ...`), so `cwd == /Users/chad/Projects/quipu_1` and that path must be on `sys.path`. There is a top-level `__init__.py` but the package is NOT pip-installable — bare imports won't work as `quipu_1.normalize.players`. Scripts inside `pipeline/` need `python -m pipeline.monday_open` (the bare `python pipeline/monday_open.py` form will fail import).
- **Env vars** (used by the dashboard and `thursday_lock`):
  - `QUIPU_DB` — DB path (default `data/golf.db`)
  - `QUIPU_SEASON` — season year (default: current year)
  - `QUIPU_POOL_ENTRIES`, `QUIPU_WEEKLY_SKINS`, `QUIPU_TOTAL_EVENTS` — Chad's pool config (defaults 12, 372.0, 31)
  - `QUIPU_LOG` — optional log file path for `thursday_lock`
- Default SQLite DB: `data/golf.db` (relative — created on first run). All tables (`players`, `player_aliases`, `player_source_ids`, `players_unresolved`, `events`, `picks`, `skins`, `event_field`, `season_results`, `event_odds`, `http_cache`) live in this single file by design.
- ESPN, DataGolf, and the sportsbook scrapers all need a real network — the sandbox can't reach them. Backfills and live pipeline runs go on the user's machine.

## Layout (actual, vs. the README's aspirational tree)

```
quipu_1/                       <- repo root; cwd for every script
├── normalize/                 player crosswalk + canonical events
│   ├── players.py             PlayerCrosswalk (sqlite-backed) + compat shim
│   ├── seed.py                seed from OWGR_TOP_200 + SUPPLEMENTAL
│   ├── seed_owgr.py           static OWGR top-200 list w/ ESPN ids
│   ├── events.py              hardcoded CANONICAL_EVENTS dict (2026 schedule)
│   └── join.py
├── fetchers/
│   ├── espn.py                primary live source — scoreboard, eventlog
│   ├── datagolf.py            DISABLED (ToS); needs paid API key to revive
│   ├── odds.py                DISABLED (anti-bot); use add_odds.py paste-in
│   └── weather.py
├── ledger/ledger.py           Ledger class — picks, events, skins, field, results
├── scoring/
│   ├── inputs.py              build_event_inputs — hydrates PlayerInputs from ledger
│   ├── multi_objective.py     score_field — probability cascade + composite
│   ├── skins_ev.py            skins EV math + re-exported odds helpers
│   └── allocation.py          multi-event allocation planner
├── pipeline/
│   ├── monday_open.py         resolve field, persist event_field, surface unresolved
│   ├── thursday_lock.py       final rec print using score_field + Ledger
│   └── sunday_close.py        resolve picks, update skins pot, write results
├── cache/cache.py             SQLite TTL cache (Cache class + module-level helpers)
├── backfill.py                week-by-week ESPN walk → season_results
├── multi_season_backfill.py   wrapper around backfill_range for 3-4 seasons
├── course_history.py          per-player per-venue shrinkage estimator
├── add_odds.py                paste-in Vegas odds → event_odds table
├── import_picks.py            bootstrap ledger from CHAD_PICKS_2026 (hardcoded)
├── record_pick.py             Thursday CLI: lookup → confirm → insert into picks
├── golf_dashboard.py          Flask UI on :7071 (reads ledger directly)
└── test_*.py                  smoke tests, not pytest-discovered
```

## Where the source of truth lives

- **Who has been picked**: `picks` table. The partial unique index `uq_picks_player_season_active` enforces one-and-done only for `voided = 0` rows. A WD-restored player can be repicked because their voided row doesn't trip the constraint.
- **What the field is**: `event_field` table, written by `monday_open.run_monday_open`. Anything not in `event_field` is invisible to the scorer.
- **Per-player season form**: `season_results`, written by `sunday_close` and `backfill`. `scoring/inputs._hydrate_season_form` reads from here. Mid-season starts need `backfill.py` first or the scorer falls through to the flat prior.
- **Vegas odds**: `event_odds` (de-vigged at insert time by `add_odds.py`). Scorer auto-loads via `load_odds_for_event` if caller doesn't pass `vegas_odds=` explicitly.
- **Skins pot state**: `skins` table. `Ledger.current_skins_pot` computes `weekly_contribution × (1 + rolled_events)`.

## Design decisions worth keeping in mind

### Crosswalk

- **Append-only.** `players.py` never auto-merges or auto-deletes canonical records. Wrong merges propagate silently for weeks; manual review of the unresolved queue is the safety valve.
- **Resolve order**: source_id → exact normalized alias → fuzzy ≥ 0.95 with ≥ 0.05 gap to runner-up → queue. `AUTO_RESOLVE_MIN = 0.95`, `QUEUE_MIN = 0.70`. The 0.05 gap rule is what prevents Cameron Young / Cameron Smith from collapsing.
- **Name normalization is deliberately lossy in helpful ways**: diacritics stripped via NFKD + explicit substitutions for atomic codepoints (`ø→o`, `æ→ae`, `ß→ss`, `ł→l`, etc. — NFKD doesn't decompose those), `Last, First` flipped, initials separated, generation suffixes (`Jr`/`III`) stripped.
- **Nickname expansion is comparison-only.** Never store nickname-expanded aliases — keep "Cam" and "Cameron" distinct, or the system loses its ability to detect their collision later. The boost to 0.97 when expanded forms match AND last names match is what makes "Cam Young" resolve to "Cameron Young" without lowering the floor for other fuzzies.
- **Birth year is the disambiguator of last resort** (Tom Kim vs Si Woo Kim, Charlie vs Tiger). Stored on the canonical row; not yet used in `similarity()` — TODO for harder ambiguous cases.

### Scoring

- **Probability cascade** in `_base_finish_probs`: DataGolf finish distribution → Vegas implied (de-vigged) → ESPN season form (only if `season_starts ≥ 3`) → flat prior (1/field_size). Each step records a `source` + `confidence` so the composite gets discounted appropriately.
- **Venue lift is multiplicative on top of the base**, applied to win/top5/top10/top20 in tandem (correlated). Cut prob gets a softer adjustment (cut-making is less venue-dependent). Lift is capped at [0.5, 2.0] and scaled by evidence: 0 starts → no adjustment, 5+ starts → full lift, linear in between.
- **Composite is normalized within field, then discounted by source confidence.** A flat-prior player can't out-score a Vegas-anchored player by virtue of normalization alone — `composite *= probs.confidence` is doing the work.
- **Majors weight redistributes to season earnings when `can_win_majors_bucket = False`** (you joined mid-season and missed a major; the majors bucket is unreachable so its weight goes back into the pile).
- **Skins EV scales co-pickers by relative chalk** (`probs.win / avg_win`, capped 5x). Chalk plays share the pot more than longshots do. Coarse — a better model would weight by Vegas-implied directly.
- **Course history shrinkage**: `SHRINKAGE_ALPHA = 4` pseudo-starts of baseline blended before trusting the player's venue rate. Baselines: top-10 = 12%, top-20 = 20%, cut = 70% (weighted across field sizes; PGA Tour pools span 70–156).

### Pipelines

- Three discrete scripts, one per day, no daemon. **monday_open** is the only one that mutates `event_field`. **thursday_lock** is read-only — it scores and prints, doesn't write to `picks` (that's `record_pick.py`). **sunday_close** is idempotent — safe to re-run.
- ESPN is the primary live source. DataGolf and sportsbook scrapers are intentionally disabled (see "Known bugs", #5/#6). The paste-in `add_odds.py` flow exists because direct scraping was too brittle/ToS-iffy.
- HTTP cache TTLs are tuned per state: 5 min during live play, 4 h during the current week with no play, 30 d for completed events. `backfill.py` against a cold cache is 5–15 minutes for 4 seasons; re-runs are cheap.

### Ledger

- **One DB by design** — `data/golf.db` holds the crosswalk, the ledger, the HTTP cache, and the odds table. Backups are simpler; cross-table joins are SQL-local. The compat shim in `players.py` violates this (see bug #4).
- **Voided picks are kept, not deleted.** The audit trail matters and the partial unique index handles the constraint logic. Use `Ledger.void_pick()` after the fact, or `record_pick(voided=True)` if you knew at pick time.

## Two event-ID schemes — know which to use where

`normalize/events.py` keys events by string slugs (`"pga_championship_2026"`). The ledger uses `e_<espn_event_id>` (`"e_401609513"`). These never converge — there is no translator and there shouldn't need to be one.

- **Display only** (upcoming schedule, major tracker name lookup): `CANONICAL_EVENTS` slugs. Hand-typed forward-looking schedule, will go stale, doesn't drive any state.
- **Scoring + persistence**: ledger canonical ids. The dashboard's `_current_ledger_event` and `thursday_lock`'s `_next_scheduled_event` both read from the ledger's `events` table where `monday_open` writes. Score against those.

If you find yourself trying to translate one to the other, you're probably reaching for the wrong table.

## Disabled integrations (deliberate)

- **Sportsbook odds scrapers** (`fetchers/odds.py`) — anti-bot triggers were too brittle. `fetchers/__init__.py` comments out the exports. Paste fresh lines via `add_odds.py` instead; the scorer reads from `event_odds`.
- **DataGolf scraper** (`fetchers/datagolf.py`) — scrapes the consumer site, ToS violation. To revive properly, subscribe to `feeds.datagolf.com` (~$270/yr) and rewrite to use the API key. The scorer already has a slot for `datagolf_finish_distribution` in `PlayerInputs`.

## Fragility notes (not bugs, but worth knowing)

- **`Ledger.MAJOR_NAME_FRAGMENTS` matches by substring.** "u.s. open" matches "The U.S. Open" but also any future event containing those words. Brittle if ESPN renames anything. The `is_major` flag is computed only at insert — re-running `upsert_event` keeps it.

## Things to NOT do

- Don't auto-resolve crosswalk entries below the 0.95 + gap thresholds. A wrong merge means a player gets double-burned silently. The unresolved queue is the deliberate friction.
- Don't write nickname-expanded names into `player_aliases`. Comparison-time only.
- Don't add new event IDs to `CANONICAL_EVENTS` and assume the scorer will use them — the scorer reads from the `events` table (ledger), not from `CANONICAL_EVENTS`. That dict is for the dashboard's "upcoming" view and for `import_picks.py` fuzzy matching.
- Don't reintroduce sportsbook scrapers without re-checking ToS and anti-bot — the previous attempts were brittle. Paste-in via `add_odds.py` is the supported path.
- Don't delete voided pick rows. The audit trail and the partial-unique-index contract both depend on them sticking around.
- Don't reintroduce broad `except Exception: return []` swallows in the dashboard. They mask real errors. Logger.warning + targeted handling for the known empty-state cases (no scheduled event, no odds yet) is what's in place.

## Useful entry points when working on this

- Want to reason about a player ID issue? Start at `normalize/players.py:430` (`PlayerCrosswalk.resolve`) and look at the test cases in `__main__` (line 624+).
- Want to change scoring? `scoring/multi_objective.py:341` (`score_field`) is the only public entry. Probability cascade is `_base_finish_probs` at line 124.
- Want to add a real data source? Hydrate `PlayerInputs.datagolf_finish_distribution` (best signal) or `vegas_win_implied_pct` (next best) in `scoring/inputs.py:140`, and the cascade picks it up automatically.
- Want to debug a pipeline run end-to-end? `python pipeline/monday_open.py --demo` runs a self-contained mock against a fresh DB. Good sanity check after any change to `players.py` or `ledger.py`.

## Environment

`.env.example` lists `DATAGOLF_API_KEY`, `ESPN_API_KEY`, `ODDSTRADER_API_KEY`, `GMAIL_APP_PASSWORD`, `GMAIL_FROM`. In practice ESPN doesn't need a key (the endpoints we use are unauthenticated), DataGolf and OddsTrader integrations are disabled, and the Gmail credentials are unused in current code. The `.env` file is real config only for whatever the user wires up next.
