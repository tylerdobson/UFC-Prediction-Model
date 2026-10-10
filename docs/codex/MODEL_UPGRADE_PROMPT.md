# Codex task: make the UFC model measurably better and make the deliverable show it honestly

You are working in this repository (`UFC-Prediction-Model`, Python ≥ 3.11, stdlib-only core, SQLite, Streamlit dashboard). Read this whole brief before changing anything, keep it open, and re-read the relevant phase before starting it. It comes from a code-and-data review done on 2026-10-09; every number below was measured on the repository's own data with the repository's own model code.

## 1. Why this work is needed (review findings)

1. **The current model is close to a coin flip.** Published research holdout (`reports/ufc_research_1993_2026_holdout.json`, 800 test bouts, 2023-12-09 → 2026-09-19): Elo 52.3 % accuracy / log loss 0.6919; calibrated logistic 55.8 % / 0.6856 / Brier 0.2462. A coin flip scores 0.6931 / 0.2500. Elo ratings barely separate (median |Elo gap| in test = 28 points).
2. **The research data has outcome-correlated survivorship bias.** The Wikipedia importer skips any bout with an unlinked/unresolved fighter (`src/ufc_odds_model/wikipedia_history.py`, the `skipped_bouts` branch around lines 596–640). 1,647 of 8,905 bouts are dropped: 11 % of the train era, 24 % of validation, **45 % of the test period**. In one-sided held bouts the unresolved fighter **lost 95 %** of the time before 2021 (79 % in 2021–23, 63 % in 2024–26), because fighters who lose and get cut rarely get Wikipedia articles. Effects: winners lose wins from their records, newcomers look like winners (debutant win rate in kept data: 42 % in 1993–2010, 48 % in 2011–20, but 64 % in 2021–23 and 59 % in 2024–26), and the test set is selected on the outcome.
3. **Orientation artifact in calibration reports.** Bouts are oriented by sorted fighter ID (`wikipedia:<page_id>`), so “fighter A” is usually the fighter with the older Wikipedia article. Fighter A wins 51.7 % in train but 41.6 % in test, which makes the calibration bins look broken. Log loss and Brier are orientation-invariant; calibration bins and accuracy at exact 0.5 ties are not.
4. **Result-only features are weak.** Restoring the dropped bouts (provisional name IDs) fixes the bias but not the accuracy: on complete cards (1,447 test bouts) Elo K=32 scores 55.1 % / 0.6846 and logistic-prior-v2 53.5 % / 0.6881. New information is required.
5. **Age is the strongest free, rights-compatible signal.** Wikidata (CC0) has day-precision birth dates for 1,809 of the 1,878 research fighters. On bouts where both birth dates are known, the younger fighter won 57.4 % (2011–20) and 61–62 % (2021–26). Adding age to the logistic on complete cards: 53.5 % → 55.8 % accuracy, log loss 0.6881 → 0.6805; with an age-decline hinge, Elo K=64 and a 2005+ training window: **56.5 % / 0.6753**. On the 763 test bouts with both birth dates: 52.3 % → 58.5 %, log loss 0.6948 → 0.6636.
6. **Coverage flags leak.** The features `known_age_difference`, `known_reach_difference`, `known_sig_strike_stat_difference` encode whether a profile exists. Profile existence tracks Wikipedia notability, and notability tracks future success. In the review, adding `known_age_difference` raised test accuracy by 2.1 points with essentially no log-loss gain. That is leakage, not signal.
7. **Forward test (UFC 332, 2026-10-03), never settled in the repo.** Saved research forecasts (`reports/ufc332-forward-research-full-20260927T1643Z.json`): 8 bouts, 5/8 correct, log loss 0.641. On the 4 bouts with saved two-sided odds, the model went 1/4 (log loss 0.713) and the no-vig market 3/4 (0.541). The model gave Figueiredo 0.52 vs market 0.17 (“EV +217 %”) and King Green 0.52 vs 0.30 (“EV +68 %”); both lost by first-round TKO. Flat 1-unit stakes on every model +EV price: 4 bets, −1.35 units. Actual winners: Silva, Talbott, Ribovics, Soldić, Kopylov, Walker, Hernandez, Naurdiev.
8. Engineering quality is high (391 offline tests pass in about 7 s, strong provenance and receipts), but the model is about 300 lines while the provenance and gating stack is about 20k. Spend effort on data coverage, features, evaluation and an honest deliverable.

## 2. Ground rules

- **Branch first:** `git switch -c model-accuracy-upgrade` from `main`. Commit after each phase with a clear message. **Do not push.**
- **Pre-existing uncommitted work:** `git status` shows changes to `src/ufc_odds_model/dashboard_data.py` (card review queue, about +170 lines), `docs/DATA_SOURCE_DECISION.md` (Wikimedia licensing wording), and untracked `scripts/capture_completed_event.py`. **Do not discard or rewrite them.** Run the full suite; if it is green, commit them first as their own commit (“Add completed-event capture and card review queue”). If tests fail because of them, stop and report.
- Keep the core package **stdlib-only** (`dependencies = []`). The review's experiments needed nothing else.
- **Data sources:** no scraping of UFC.com or UFCStats, no paid sources, no new providers beyond Wikipedia/Wikidata and the already-configured The Odds API key. Use the project's existing `USER_AGENT` (`wikipedia_history.USER_AGENT`) for Wikimedia. Throttle requests to at least 1.5 s apart for SPARQL and honor 429/503 `Retry-After`. Never print or commit `.env` contents. Never put a personal email address in request headers.
- **Never delete, rename or overwrite** anything under `data/`, `backups/`, `reports/`, or `data/raw/`. Write new outputs to new files. The original research DB must stay byte-identical; open it read-only (`file:...?mode=ro`).
- Keep the research-only vs operating separation and every existing fail-closed decision gate. Anything that relaxes a gate must be an explicit opt-in flag whose default is unchanged.
- **Every phase must:** add or adjust unit tests with small synthetic fixtures, keep `python -m unittest discover -s tests` green, update the relevant docs, and record its metrics in `reports/model_upgrade_metrics.json` (append one entry per phase).
- **No test-set tuning.** Choose every hyperparameter on validation or rolling-origin folds. Treat the test split as read-once per phase. If a target below is missed, report the actual number. Never adjust the method to hit a target.

## 3. Fixed comparison protocol (Phase 0 builds it; every later phase reports through it)

- **Research DB:** `data/ufc_research_2011_2025.sqlite`. Despite the name, it holds 1993-11-12 → 2026-09-19: 790 events, 7,258 accepted bouts, 1,878 fighters.
- **Date split, hard-coded so later changes in composition cannot move it:** train ≤ 2021-03-13; validation 2021-03-20 … 2023-12-02; test ≥ 2023-12-09.
- **Metrics:** log loss (primary), Brier, accuracy; paired event-date block bootstrap for model-vs-model differences (reuse `research_evaluation._paired_event_bootstrap`).
- **Orientation:** report calibration with a deterministic hash orientation, never sorted IDs:
  ```python
  flip = int(hashlib.sha256(bout_id.encode()).hexdigest(), 16) & 1
  a, b = (fighter_b, fighter_a) if flip else (fighter_a, fighter_b)
  ```
  Also add a favorite-oriented reliability table: `p_fav = max(p, 1 - p)` against whether the favorite won.
- **Baselines in every report:** coin flip, “younger fighter wins” (bouts with both birth dates), Elo-only, logistic, and market no-vig wherever captured two-sided odds exist.
- Report each test metric twice: on **all** test bouts, and on the **originally kept** subset (so the effect of better features is separated from the effect of test-set composition).

## 4. Phases (execute in order)

### Phase 0: baseline harness
- Add `src/ufc_odds_model/model_lab.py` and a CLI subcommand `ufc-model research-compare --db PATH [--complete-db PATH] [--out reports/...json]`. It implements the protocol above and prints a compact table.
- **Acceptance:** on the current research DB it reproduces the published holdout exactly: test 800 bouts; Elo 0.6919 / 52.25 %; logistic calibrated 0.6856 / 55.75 %; temperature scale ≈ 1.3956.

### Phase 1: stop dropping fights (complete-card research history)
- New module `src/ufc_odds_model/research_complete.py` and CLI `ufc-model research-complete-history --db data/ufc_research_2011_2025.sqlite --out-db data/ufc_research_complete.sqlite`. Copy the DB with the SQLite backup API, then add the held bouts to the **copy only**.
- **Held set:** the positions in `reports/wikipedia_1993_2026_identity_review.json` (1,677), minus those recovered by identity replays (`research_evaluation._reconcile_identity_receipts` recovers 30), which leaves **1,647**.
- **Rebuild each held bout** by re-parsing its retained, hash-verified raw payload with `wikipedia_history.parse_event_page_title` (or the embedded-card parser in `wikipedia_embedded.py` for season/year pages). This yields both names, link titles, outcome and **method**. Fall back to the import review JSONs' `skipped_bouts` (on `def.` rows, `fighter_a` is the winner) only when re-parsing is impossible, and count the fallbacks.
- **Identities:**
  - Linked names map to the existing `wikipedia:<page_id>`. Resolve titles through the saved MediaWiki action-API responses under `data/raw/wikipedia/`, handling `normalized` and `redirects`.
  - Unlinked names get `provisional:wikipedia_name:<NFKC casefold, whitespace-collapsed name>`, with `fighters.source = 'wikipedia_provisional'`. Never merge a provisional ID into a real one automatically.
- Give restored bouts `bouts.source = 'wikipedia_research_restored'` and keep their method. The existing identity reconciliation keeps operating on `'wikipedia_research'` rows only, so it does not break.
- Add a collision report at `reports/research_complete_identity_report.json`. Flag provisional names that appear twice on one date, that appear in different weight-class families within 12 months, or whose normalized name collides with a real fighter's canonical name.
- Extend `research_feature_rows` (or add a sibling) to include restored bouts when evaluating the complete DB.
- **Acceptance (fixed split, complete DB):**
  - Test binary bouts: 1,447 ± 15.
  - Elo K=32: test log loss 0.6846 ± 0.004.
  - logistic-prior-v2: test log loss 0.6881 ± 0.004.
  - Held share reported ≈ 0 %.
  - The original DB is unchanged (verify by sha256 before and after).
  - This phase makes the data honest; it is **not** expected to raise accuracy.

### Phase 2: static fighter attributes from Wikidata, without leakage
- New module `src/ufc_odds_model/wikidata_profiles.py` and CLI `ufc-model import-wikidata-profiles --db PATH`.
  - Map `wikipedia:<page_id>` → enwiki title from saved responses, falling back to the MediaWiki `pageprops` API in batches of 50. Map the title to a Wikidata QID. `wikidata:Q…` fighters are used directly.
  - Query with SPARQL (POST `https://query.wikidata.org/sparql`, at most 250 values per query):
    ```sparql
    SELECT ?title ?item ?dob ?prec ?height ?ufc WHERE {
      VALUES ?title { "Alex Pereira"@en ... }
      ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title .
      OPTIONAL { ?item p:P569/psv:P569 [ wikibase:timeValue ?dob ; wikibase:timePrecision ?prec ] . }
      OPTIONAL { ?item p:P2048/psn:P2048/wikibase:quantityAmount ?height . }
      OPTIONAL { ?item wdt:P9722 ?ufc . }
    }
    ```
  - Accept a birth date only at day precision (`prec ≥ 11`). Treat multiple distinct dates as a conflict: skip the fighter and list it in a report. Store heights in cm and the UFC athlete ID (P9722) as an external ID for later identity review.
  - Keep raw responses as content-addressed receipts (`ingestion_runs` + sha256), like every other source. License: `https://creativecommons.org/publicdomain/zero/1.0/`.
  - **Expected coverage:** about 1,877 of 1,878 matched, about 1,809 with one day-precision birth date, 22 conflicts, about 620 with height.
- **Schema:** add migration `014_static_fighter_attributes.sql`, for example a `fighter_static_attributes` table (fighter_id, attribute, value, source, license, ingestion_run_id, immutable triggers).
  - Static, time-invariant attributes (birth date, height) are admissible at any decision cutoff in research replays. In operating scoring they must have been fetched before the decision, which prospective use satisfies.
  - Leave the existing dated-observation path for time-varying data unchanged.
  - Wire static attributes into `features._profiles_as_of`, or a sibling, without weakening the dated path.
- **Remove the leak:** new model version `logistic-prior-v3`. Its feature vector **excludes** `known_age_difference`, `known_reach_difference` and `known_sig_strike_stat_difference` (drop them, or force them to 0). Keep coverage in report metadata only. Keep v2 reproducible.
- **Add** `age_decline_32_difference = (max(age_a - 32, 0) - max(age_b - 32, 0)) / 5` when both ages are known, else 0. The age difference itself stays the existing `age_years_difference_10`, which is 0 unless both are known.
- **Acceptance (complete DB, fixed split):**
  - Age difference only: test log loss 0.6805 ± 0.003.
  - Plus the hinge: 0.6794 ± 0.003.
  - Report the both-birth-dates subset and the younger-fighter-wins baseline (≈ 0.62 on test).
  - Add a test proving the coverage flags cannot enter v3.

### Phase 3: tuning and rolling-origin evaluation
- Make the Elo K a recorded model parameter (included in the version hash). Choose K from {32, 48, 64, 96} on **validation**. The review found K=64 best on validation on complete data (validation log loss 0.6814 vs 0.6842 at K=32).
- **Optional:** experience-scaled K (higher for a fighter's first ~5 recorded bouts) and a margin-of-victory multiplier for finishes (method is now available for all bouts). Keep each only if validation or rolling-origin log loss improves.
- **Training window:** compare start years {1993, 2001, 2005, 2010} on rolling-origin folds. The review saw 2005+ help on test (0.6769 → 0.6753). Confirm on folds before adopting it, because that observation peeked at test.
- **Rolling-origin evaluation:** yearly folds 2016 … 2026. Train on dates before year Y, calibrate the temperature on the last 15 % of pre-Y dates, test on year Y. Report per-year and pooled log loss / accuracy with bootstrap CIs, plus baselines.
- **Acceptance:**
  - Fixed-split test log loss ≤ 0.6760 and accuracy ≥ 56 %.
  - Pooled rolling-origin log loss beats v2, with the paired CI reported (state plainly if the CI includes 0).
  - Add a short paragraph to the docs: the no-vig market is typically ~65 %+ accurate, so this model will not match it without market data or richer fight statistics.

### Phase 4: forward-test ledger, UFC 332 settlement, odds schedule, market guardrail
- Finish `scripts/capture_completed_event.py` and use it to capture the completed UFC 332 page.
- Add a forecast ledger as a table via migration 015 (or append-only JSONL under `reports/`). CLI:
  - `ufc-model forecast freeze EVENT` records model version, cutoff, probabilities, the market no-vig probability at capture, and hashes.
  - `ufc-model forecast settle EVENT` records outcome and per-bout log loss / Brier for the model and the market.
  - `ufc-model forecast summary` prints cumulative results.
- Import the saved UFC 332 research forecasts as the first ledger entry and settle them. **Expected:** 8 bouts, 5/8, log loss 0.641. On the 4 bouts with odds, the model scores 0.713 and the market 0.541.
- **Odds schedule:** document and support capturing The Odds API snapshots at T-7d, T-24h and T-2h for each event (the last pre-start snapshot is the closing proxy). Use the existing receipts. Refuse to run when the API's remaining-requests header is below 50.
- **Market-disagreement guardrail:** in `pipeline.score_event` and `alerts.gate_prefight_alerts`, when `|p_model - p_market_no_vig| > max_market_disagreement` (default **0.10**, flag `--max-market-disagreement`), the decision is `pass_market_disagreement` and never `candidate`. Add tests covering the UFC 332 Figueiredo and Green shapes.
- Report closing-line value (CLV) for paper and actual bets once two snapshots exist.

### Phase 5: opt-in live training source
- Today `score-event --model logistic` needs ≥ 100 source-dated binary bouts in the operating DB, and there are 0, so no live model forecast is possible.
- Add `--training-history PATH` to `score-event`, `alert-event` and `paper-trade`. It trains v3 on the complete research DB using strictly earlier-date features.
- Label it `training_history: research_retrospective` in the model version and in prediction metadata.
- Every decision gate (fresh odds, roster freshness, identity review, the market guardrail) stays as is. The default behavior is unchanged.

### Phase 6: the deliverable
- **`docs/MODEL_CARD.md`** (one page): what is predicted; data coverage (complete vs held, birth-date coverage); fixed-split and rolling-origin metrics against baselines with CIs; favorite-oriented reliability; the forward-test ledger summary; limitations (no fight statistics, no historical odds, the market gap); intended use (research, not betting advice).
- **Dashboard** (`app.py`, `dashboard_data.py`), still read-only:
  - Upcoming card: model vs market no-vig, the disagreement flag, the top three feature contributions per bout (`weight × value × temperature`, in logit units, with plain labels such as “age gap”), and data coverage per fighter.
  - Forward test tab: the ledger, with per-event and cumulative model vs market log loss and Brier.
  - Model tab: rolling-origin log loss by year against baselines, plus the reliability table.
- **README:** a one-paragraph summary, a “Current performance” table (honest, including the market gap), quickstart, a short “How it works”, and links to the deep docs. Move provenance detail into `docs/`. Target ≤ 150 lines.
- **`docs/EVENT_WORKFLOW.md`:** the exact prepare (card capture, odds snapshots, `forecast freeze`) and settle (results capture, `forecast settle`) commands.
- **Hygiene (do not act; report only):** note in docs that `data/ufc_research_2011_2025.sqlite` holds 1993–2026, and list the stale `data/*.sqlite` scratch copies in the final report as cleanup candidates.

### Phase 7 (optional, time-boxed, last): pre-UFC experience
- For fighters with articles, parse the “Mixed martial arts record” table from article wikitext, using the existing MediaWiki REST page-source path and receipts (CC BY-SA 4.0, attribution required).
- Derive, strictly from rows dated before each bout: pro fights, smoothed pro win rate, finish rate, KO/TKO losses, and days since the last pro fight including non-UFC fights.
- Coverage is notability-correlated, so use no coverage flags and neutral values when data is missing. Report coverage by split.
- Keep it only if rolling-origin log loss improves. If parsing is brittle, ship the partial result with a coverage report.

## 5. Final report (print it when done, and save it to `reports/model_upgrade_summary.md`)
1. A per-phase table: test bouts, accuracy, log loss, Brier (all and originally-kept), rolling-origin pooled log loss with CI, each against its target. State met or missed plainly.
2. The forward-test ledger summary.
3. Anything skipped and why.
4. Open decisions for Tyler:
   - Whether to license or permit a fight-statistics source (UFCStats-derived datasets are research-only and rights-unclear).
   - Whether to buy historical odds.
   - Whether to make `--training-history` the default.
   - The DB rename and cleanup.
5. Files changed and the exact commands to reproduce every number.
