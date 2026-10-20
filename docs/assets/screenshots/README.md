# README screenshots

These images were captured on October 19, 2026 local time and are included without pixel
editing. Their source files are independent of the frozen model experiments.

| Image | Provenance | What it demonstrates |
|---|---|---|
| [ufc-dashboard.png](ufc-dashboard.png) | Actual local Streamlit screen captured with Playwright Chromium, 1600 × 1200 at 1× density | Verified saved V6 research probabilities, official UFC portraits, earlier-round statistics and downloadable report artifacts |
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

The prediction dashboard capture uses the checked-in V6 final release, with
six scored bouts and eight explicit identity holds. The real fighter images
were loaded from official UFC URLs recorded in the [portrait manifest](../../../assets/fighters/manifest.json).
This screenshot is an actual rendered interface, not the generated design concept.
Browser checks also covered all six matchup selections, their exact observed-round
bar counts and hover details, eight-stat comparisons, all six navigation tabs,
byte-identical PDF and CSV downloads, and 1280px, 1024px, 768px and 390px layouts.
The built-in browser connections were unavailable, so visual verification used
local Playwright Chromium. Portraits need internet access; the forecast is saved,
not a live feed.

Source-image SHA256 values:

```text
ufc-dashboard.png   447ab07b31c9f93f12d2a568682a5ae45a3e637a8346d38719baf23695b97576
mysql-workbench.png  4151b6452306441a44eca01b3d0b4c03f8bb43f1e2656b01676a52824f6fe143
pipeline-demo.jpg   f20dc647da8c1ef8d6842776accd5845516d0f525e06be1bac1c72f6855921dc
```

No database password or private runtime configuration is displayed in these
images. Regenerate the report or rerun the SQL to inspect current state; these
images do not prove continuous service availability or a future scheduled run.
