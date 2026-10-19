# README screenshots

Both images were captured on October 19, 2026 and are included without pixel
editing. Their source files are independent of the frozen model experiments.

| Image | Provenance | What it demonstrates |
|---|---|---|
| [mysql-workbench.png](mysql-workbench.png) | User-provided Desktop capture, copied byte-for-byte | Workbench connected to MySQL 8.4.11 on the project port; normalized warehouse tables and the initial archive-count rows |
| [pipeline-demo.jpg](pipeline-demo.jpg) | Actual Chrome window capture through Computer Use | The generated offline synthetic demonstration, its explicit synthetic label and quality checks |

The Workbench screenshot shows the query from
[portfolio_screenshot.sql](../../../sql/mysql/portfolio_screenshot.sql). The
first five count rows are visible in the captured result grid; the full query
returns nine rows. A direct execution against the active complete publication
also verified two fighter facts per usable bout, zero facts from held bouts and
accepted-plus-held bout reconciliation. The screenshot is not a claim that all
nine result rows are visible in its frame.

The synthetic report is generated with `./scripts/demo.sh`, at
`output/pipeline-demo/index.html`, using run ID
`synthetic-demo-647323d883e17cb706301c4b`. Its five cards, six bouts and four
fighter profiles are invented and distinct from the real warehouse counts.
The native capture returned JPEG bytes, so that image uses a `.jpg` extension.

Source-image SHA256 values:

```text
mysql-workbench.png  4151b6452306441a44eca01b3d0b4c03f8bb43f1e2656b01676a52824f6fe143
pipeline-demo.jpg   f20dc647da8c1ef8d6842776accd5845516d0f525e06be1bac1c72f6855921dc
```

No database password or private runtime configuration is displayed in either
image. Regenerate the report or rerun the SQL to inspect current state; these
images do not prove continuous service availability or a future scheduled run.
