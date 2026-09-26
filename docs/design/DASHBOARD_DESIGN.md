# Dashboard visual spec

The four Image Gen concepts in this folder are the visual references for the first read-only Streamlit dashboard. They were generated before implementation. The UI is code-native; the PNGs are design references, not screen backgrounds.

## Screens and content

| View | Concept | Required content |
| --- | --- | --- |
| Upcoming card | `dashboard-upcoming-concept.png` | Event selector, source status, bout/model/quote/age/decision table, decision requirements. |
| Model evidence | `dashboard-evidence-concept.png` | Elo/logistic/bookmaker comparison, split dates and counts, calibration, visible missing evidence. |
| Data quality | `dashboard-quality-concept.png` | Ingestion receipts, audit summary, issue list, price coverage. |
| Ledgers | `dashboard-ledgers-concept.png` | Separate paper and manually recorded actual wagers, open exposure and settlement state. |

## Shared system

- True white page background; deep navy text (`#0b1d39`), blue-gray supporting text (`#536681`), teal selected navigation (`#0b7885`), cool-gray rule (`#dce4ed`), and table head (`#f1f5f8`). No photographic or decorative assets.
- Georgia-like serif for the brand and section headings; a deliberate system sans-serif for body, tabs, controls, tables and metadata. Desktop outer gutter about 40px, content no wider than 1500px. Mobile stacks the status rail and content without horizontal page overflow; wide tables scroll within their own wrapper.
- Header is a single brand/workspace line plus three evidence timestamps. Four horizontal tabs share one underline treatment. Thin dividers and open space dominate; only purposeful source, comparison, and ledger panels use borders.
- Visible page copy follows the concepts. Required live data or safety metadata may appear below the first viewport. If source fields are unavailable, display **Unavailable**, **Not assessed**, or a concrete reason; never synthesize metrics, quote times, or recommendations.
- Navigation and event selection are the only controls. No ingestion, training, settlement, or order controls exist in the dashboard.
- Footer: “All times UTC · Read-only · No wagers placed from this app”, followed by the current page snapshot timestamp so an idle browser tab cannot be mistaken for a fresh market check.

The generated concepts intentionally show empty states because this repository does not contain verified real UFC history or live licensed feed credentials. A populated state replaces the empty tables with stored rows while preserving the same table-first layout. The data adapter owns availability status; the UI does not promote a quote to an actionable bet.

Two evidence-led copy changes from the concepts are intentional: the quote column says **Observed quote** because a saved price is not executable proof, and the calibration chart says **test set** because the saved evaluation's displayed bins are measured on the untouched test period while calibration is fit on validation data.
