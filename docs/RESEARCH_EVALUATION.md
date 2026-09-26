# Retrospective Wikipedia model comparison

The separate `research_evaluation` command measures whether result-only features predict **accepted** UFC outcomes in the Wikipedia research database. It is an exploratory holdout, not the operating `ufc-model evaluate` command. Wikipedia revisions and their 2026 import receipts cannot show which past results, roster, or statistics were published at an old pre-fight decision time. This command never changes the database, produces an alert, estimates bookmaker performance, or calculates ROI.

Run the source integrity check first, then create a report from the existing local research database and the checked combined identity worksheet:

```bash
python -m ufc_odds_model.integrity --db data/ufc_research_2011_2025.sqlite
python -m ufc_odds_model.research_evaluation \
  --db data/ufc_research_2011_2025.sqlite \
  --identity-review reports/wikipedia_1993_2026_identity_review.json \
  --out reports/ufc_research_1993_2026_holdout.json
```

The report is JSON and stays local under Git-ignored `reports/`. Omit `--out` to print it. Without `--identity-review`, model metrics still run but omitted-bout coverage is unknown. When a worksheet is supplied, the command checks its distinct `(event_id, bout_position)` held rows against the latest exact page or section receipt for **every** completed research event, including the receipt's imported count, revision consistency, and retained payload hash. A missing, changed, or incomplete worksheet fails instead of showing an inflated accepted share.

## Method

- Read only completed `wikipedia_research` event, bout, and result rows. Sort fighter IDs within every pair, so winner-first table order cannot set the label. Draws update subsequent history; draws and no contests do not become binary targets.
- Rebuild Elo, prior bout counts, smoothed win rate, rest, and recent form from **strictly earlier calendar dates**. All results on a date are withheld until that date's targets have been formed. Current profiles, career averages, reach, and post-fight statistics are absent and remain neutral. Same-date tournament results have no reliable intra-day times; their order can affect the *following* date's Elo replay.
- Split whole event dates chronologically: 70% train, 15% validation, and the remainder untouched test. Fit regularized logistic weights only on train. Fit a symmetric temperature from validation probabilities and outcomes. Freeze both before reporting Elo, uncalibrated logistic, and calibrated logistic on test.
- Report five probability bins, accuracy, Brier score, and log loss on test. Accuracy at exactly 0.5 uses a fixed threshold and should be read cautiously; Brier and log loss are the main probability scores. Paired 95% intervals for calibrated-logistic-minus-Elo loss use 2,000 deterministic bootstrap resamples of **whole test event dates**. These intervals cover sampling within the accepted subset, not the identity-selection or historical-timing problem.
- Include SHA-256 fingerprints of the ordered accepted source rows, the generated feature rows, the logistic weights, and (when supplied) the identity worksheet. The report also records model versions, split date boundaries, counts, and temperature. It does not recheck raw source payload hashes; use the separate integrity command for that.

## Checked local run, September 26, 2026

The input contained 790 completed events and 7,228 accepted results: 7,097 wins, 55 draws, and 76 no contests. The accepted source-row fingerprint was `cbd1204f75c8cc55fce9185920a911597ca8ef9feaa85736552086b97e654999`. All 7,097 binary targets were assigned to disjoint date periods:

| Period | Event dates | Binary bouts | First date | Last date | Accepted share of accepted + held source bouts |
| --- | ---: | ---: | --- | --- | ---: |
| Train | 548 | 5,272 | 1993-11-12 | 2021-03-13 | 89.1% |
| Validation | 117 | 1,044 | 2021-03-20 | 2023-12-02 | 75.7% |
| Untouched test | 118 | 781 | 2023-12-09 | 2026-09-19 | 53.9% |

The validation temperature scale was **1.3787**. The untouched test contained 781 accepted binary outcomes:

| Model | Accuracy | Brier score | Log loss |
| --- | ---: | ---: | ---: |
| Elo | 52.50% | 0.24961 | 0.69229 |
| Logistic, uncalibrated | 55.95% | 0.24661 | 0.68632 |
| Logistic, validation-calibrated | 55.95% | 0.24620 | 0.68546 |

The calibrated logistic minus Elo Brier difference was **−0.00341**, with a paired event-date bootstrap 95% interval of **−0.00732 to +0.00008**. The log-loss difference was **−0.00683**, with interval **−0.01475 to +0.00024**. Both intervals include zero. More important, 675 of 1,463 parsed source bout rows in the test window were held for fighter identity, leaving only 53.9% accepted. That severe, likely nonrandom missingness prevents a population-level performance claim or promotion of this model.

The database has **zero** historical bookmaker quote rows. Bookmaker probability, executable edge, paper profit, and ROI remain unavailable or `null`. A trustworthy operating comparison still needs rights-cleared source-dated fight and roster history, verified pre-fight timestamps, stable identities for held bouts, and contemporaneous two-sided prices. See the [research dataset card](RESEARCH_DATASET_CARD.md) for coverage and source evidence.
