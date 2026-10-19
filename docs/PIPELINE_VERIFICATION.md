# Local pipeline verification — October 19, 2026

This is a bounded verification record for the delivered case study. It describes
observed local runs, not continuous uptime or future model performance.

## Installed and observed

| Component | Observed result |
|---|---|
| Separate native MySQL | MySQL 8.4.11, `127.0.0.1:3307`, database `ufc_warehouse` |
| MySQL user service | `com.ufcforecast.mysql` registered as a per-user LaunchAgent |
| Daily refresh | `com.ufcforecast.statistics-refresh`, 09:00 local time, MySQL enabled |
| Failure destination | Local Mac notification; scheduled webhooks disabled |
| Original evidence | All 20 previously protected model, database and report hashes unchanged |

The real source refresh resolved immutable publisher commit
`3e2f3db5751c14b5d4ea52e41921982ca23b9b23`. Its captured manifest SHA256 is
`1b26b33841206aefd8b159a182dcf674077be9b787409deaf7c530e74bcd50fd`.
The normalized logical content SHA256 is
`1337cf7825ac2417bc1362b6abbdbbe199152c8168074d13d887ef7bb1edd81f`.
Original frozen source stores were read only; the operations pipeline built a
separate version under its own ignored operations directory.

## Actual refresh and failure exercise

The initial live-source refresh, run `20261019T211237Z-13e64a9e28`, succeeded in
7.91 seconds on this machine. Capture, build, quality checks, MySQL publication
and local activation all succeeded. This single timing is not a performance SLO.

An intentional missing-manifest run, `20261019T211338Z-178d6ac44f`, then exited 1
at the source stage. The local notification command returned success, no webhook
was contacted, and the last-ready pointer's SHA256 remained identical. Whether
macOS shows a banner depends on the user's notification settings.

A manual launchd kickstart of the installed scheduled job produced run
`20261019T211340Z-ee4f880d3a`. It succeeded in 10.22 seconds, verified and reused
the same local version, and reused the same complete MySQL publication. This
verifies execution through launchd; it does not prove a future 09:00 run or
availability while the Mac is off.

## Direct SQL reconciliation

Queries against the active MySQL views returned:

| Grain | Rows |
|---|---:|
| Cards | 793 |
| Fighter profiles | 4,629 |
| Bouts, including held bouts | 8,950 |
| Usable paired-stat bouts | 8,818 |
| Held bouts | 132 |
| Fighter-bout facts | 17,636 |
| Fighter-round facts | 41,514 |

The publisher also retained four normalized source-file receipt rows and the
exact research crosswalks. Unmatched raw round rows remain in the raw/SQLite
layers rather than becoming invented normalized facts.

## Automated checks

The complete local suite ran **554 tests in 8.77 seconds**, with one gated
MySQL test skipped in that default run. A separate execution with
`UFC_MYSQL_INTEGRATION=1` passed all **12 warehouse tests**. The real-server test
verified replay, SQL foreign-key enforcement, NULL preservation and an injected
mid-load rollback against the actual local MySQL instance. The unit tests also
cover uncertain-commit handling using an injected connection failure.

The demo suite includes actual shell execution from a clean checkout with
spaces in its path and no `.venv`, with downloads disabled. It verifies raw
hashes, normalized counts, explicit gaps, strict historical cutoffs and replay
that preserves output bytes and modification times.

The operations suite checks empty feeds, unexpected archive shrinkage, source
and output tampering, separate-process lock contention, sanitized errors,
failed publication and post-activation journal failures. It confirms that
dataset-level quality failures cannot invoke MySQL publication.

The new [CI workflow](../.github/workflows/data-pipeline.yml) reproduces the
offline demonstration on Python 3.11 and 3.12 and exercises a real MySQL 8.4.11
service. This record covers local verification; hosted results are tracked in
[GitHub Actions](https://github.com/tylerdobson/UFC-Prediction-Model/actions).
The generated demo overview was also opened and inspected in Chrome.

## README visual evidence

The README now embeds an [architecture export](diagrams/statistics-pipeline.svg),
an actual [Workbench capture](assets/screenshots/mysql-workbench.png) supplied by
the user and an actual [Chrome demo capture](assets/screenshots/pipeline-demo.jpg).
The images are retained unchanged; [provenance](assets/screenshots/README.md)
records their hashes and visible scope. The separate
[screenshot SQL](../sql/mysql/portfolio_screenshot.sql) was executed against the
real active warehouse and returned nine rows with all three consistency checks
passing. This documentation change does not retrain or change the model.

## Useful next increments

1. **An operations dashboard.** Show source capture age, last successful run,
   stage duration, held-bout reasons and reach coverage. Use the versioned MySQL
   views and structured run receipts to explain why a dataset is trustworthy.
2. **A public portfolio walkthrough.** Publish only synthetic demo artifacts,
   architecture, reproducibility instructions and a short recorded SQL walkthrough.
   Keep local credentials and retained raw captures outside the public site.
3. **A prospective prediction ledger.** Freeze predictions before each event,
   then attach outcomes and calibration summaries. Keep pipeline reliability
   metrics separate from the model's future predictive performance.
4. **An unattended runner.** After observing the local schedule, move refreshes
   to a persistent host if runs must continue while this Mac is off. Preserve
   immutable captures and version activation, and verify database transport and
   alert delivery in that environment.

At this archive size, a verified full batch already completes quickly. Measure
growth and operational needs before introducing incremental merge logic or a
larger orchestration stack.
