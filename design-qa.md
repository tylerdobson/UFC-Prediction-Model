# MySQL dashboard verification — October 20, 2026 UTC

The approved sports layout is preserved. The latest unedited [dashboard screenshot](docs/assets/screenshots/ufc-dashboard.png), [rankings screenshot](docs/assets/screenshots/ufc-rankings.png), and [archive screenshot](docs/assets/screenshots/ufc-history.png) come from the actual MySQL-backed app at device scale 1. The main/archive captures are 1600 × 1200; the updated rankings capture is 1600 × 1900 to show the champion and all 15 pictured contenders.

The portrait pass loaded the champion and all 15 contender headshots, checked exact athlete order and original 256 × 160 image dimensions, preserved media ties, and verified the 390-pixel layout without page overflow. All 203 ranked identities have source-verified portraits; images remain remotely hosted on UFC.

The native browser pass confirmed seven responsive tabs, six matchup selections, 12 loaded official portraits, exact earlier-round bar counts, chart tooltips, eight sourced profile comparisons, eight visible identity holds, byte-identical PDF/CSV downloads, and working icon navigation. Layouts at 1600, 1280, 1024, 768 and 390 pixels have no page overflow; the phone card strip scrolls horizontally.

A second pass exercised historical search, both ranking systems, a selected champion/top-15 division, the media panel's tied-rank note, the unpromoted V7 comparison, three forward-ledger entries and the expanded past-opponent/style panel. No JavaScript or Streamlit exceptions occurred. Focused AppTest separately verifies seven-tab unavailable states and prohibits SQLite connections/schema setup during native page loads.

Evidence scripts and full result JSON remain in `/private/tmp/ufc-dashboard-qa/`. Browser verification used local Playwright Chromium. The following earlier reference-comparison record remains useful for the approved art direction; its previous six-tab references describe the pre-migration state.

---

# UFC dashboard reference verification

Final result: passed

The second dashboard pass follows the supplied sports workspace while keeping
the existing Streamlit app, saved forecasts and seven MySQL sections. No actionable
P0, P1 or P2 findings remain in the captured default, comparison and responsive
states. This is a UFC adaptation of the reference, rather than its football copy
or live-match behavior.

## Visual truth and capture conditions

- Source: [VALMAX, Live Sports Analytics Dashboard](https://dribbble.com/shots/26961358-Live-Sports-Analytics-Dashboard).
- Source image: [original 1600 × 1200 image](https://cdn.dribbble.com/userupload/46311570/file/24821d233198025d49923866952d5d8b.jpg?resize=1600x1200), retained locally as `/private/tmp/ufc-reference.jpg`.
- Implementation: `http://127.0.0.1:8501`, [actual browser screenshot](docs/assets/screenshots/ufc-dashboard.png).
- Capture: 1600 × 1200 CSS pixels, 1600 × 1200 image pixels, device scale factor 1.
- Source density: treated as a 1× presentation image; its framed workspace is 1411 pixels wide. Implementation workspace is the same width. Neither side was stretched for comparison.
- State: Upcoming card, saved V6 UFC 333 edition, Volkanovski/Evloev selected, Earlier fight history selected, identity holds and source notes collapsed. Official portraits finished loading; licensed local fonts were ready.
- Responsive captures: 1280, 1024 and 768 CSS pixels wide; phone 390 × 844 at 1× density. The source supplies no mobile state, so these are usability checks rather than invented mobile pixel targets.
- Browser: local Playwright Chromium. Built-in Chrome and IAB browser backends were unavailable. Browser-rendered verification was completed; HTTP health alone was not used as evidence.

## Comparison evidence

The reference and rendered implementation were opened together in each composite
before judging the result. Temporary evidence is in `/private/tmp/ufc-dashboard-qa/`.
These composites are review aids; the README screenshot is an unedited browser
capture, copied byte for byte.

| Comparison | Source crop | Implementation crop | Evidence file |
|---|---|---|---|
| Full primary workspace | x95, y136 to x1506, y1143 | x95, y32 to x1506, y1074 | `reference-comparison.png` |
| Strip, rail, portraits, chart | x124, y242 to x1485, y810 | x124, y131 to x1476, y695 | `detail-upper-comparison.png` |
| Table and archive | x225, y825 to x1485, y1120 | x215, y708 to x1476, y1074 | `detail-lower-comparison.png` |

The surrounding black presentation padding was excluded. Equal pixel scale was
preserved, with blank padding where crop heights differ. The implementation's
source/evidence expanders below the primary workspace are intentional additions.

## Five fidelity surfaces

| Surface | Result and intentional adaptation |
|---|---|
| Typography | Local Barlow body and brand type, Barlow Condensed italic headings. The original family is not identified; these licensed faces preserve the sports hierarchy. Full surnames fit, body/table text is readable, and essential evaluation/caveat text is at least 11px. Tiny portrait credits and secondary metadata remain subordinate. |
| Spacing and layout | Same framed gray workspace, narrow icon rail, black six-card strip, compact four-portrait grid, wider chart, and lower table/archive split. Selector controls and evidence notes account for extra chart-panel height. At 1100px and below, primary columns stack before portraits become cramped. Tables and matchup strips scroll within their own regions. |
| Colors and tokens | Near-black canvas/strip, #f3f3f3 workspace, #e2e2e2 frames, white cards, #adff00 lime and #8d40ff purple. Visible selection and keyboard focus styles use these tokens. Chart identity colors accompany explicit fighter names and tooltips. |
| Image fidelity | Twelve real official UFC portraits, checked for successful load, correct identity, transparency and clear head/torso crops. No generated fighter faces. Standard Tabler outline icons share a consistent style. Football crests, article photographs and SportX branding are intentionally replaced with UFC identities and this project's brand. |
| Copy and content | Actual saved probabilities, observed prior rounds, reach and eight profile comparisons replace football scores/goals. Chart explicitly describes separate opponents/dates and up to five earlier fights. Archive tiles link to real UFCStats bouts. Reused retrospective benchmarks, recent-year results, cutoff, identity holds and absence of live odds remain explicit. |

## Iteration history

1. First implementation used two large portraits and a generated concept. The
   user's request for another attempt returned the original Dribbble image to
   visual truth. Rebuilt the middle region as four compact portraits plus a wide
   lime/purple earlier-round chart, added the icon rail, and aligned lower panels.
2. `second-pass-first.png`: P1 rail anchored over content; P2 surnames wrapped
   mid-word, event title inherited a small span style, portrait tiles dominated
   the middle row, and essential notes were too small. Fixed the rail's actual
   Streamlit positioning context, scoped the date class, widened names, reduced
   tiles to 146px, and raised essential notes to 11px. Recaptured as
   `second-pass-revised.png`.
3. Revised desktop: P2 Streamlit's negative markdown margin made the history
   caveat collide with probability/identity text. Removed that margin within the
   chart body, shortened identity spacing, and reserved normal caption flow.
   Corrected copy to “up to five earlier fights.” `second-pass-final.png` shows
   the caption below both columns with clear separation.
4. Tablet: P2 the 768px layout still squeezed portrait cards. Added a targeted
   1100px stacking breakpoint through Streamlit's layout wrappers. The fresh
   `second-pass-tablet.png` shows full-width portrait/history panels; browser
   assertions confirm the breakpoint applies. Final combined comparisons use
   the corrected desktop state. No remaining P0/P1/P2 findings were observed.

## Functional and evidence checks

- All six matchup selections update portrait identity, probabilities, highlighted table row and exact observed-round bar counts. No missing fights or rounds are padded.
- Chart hover exposes fighter, opponent, bout date, round, landed strikes and validated exposure.
- Fighter statistics reveals eight comparisons. All eight identity-held bouts remain inspectable without invented probabilities.
- PDF and CSV downloads match the frozen release byte for byte.
- All six navigation tabs respond; the phone's rightmost tab remains reachable. The icon rail scrolls to its target section.
- No page overflow at 1600, 1280, 1024, 768 or 390 pixels. Phone matchup selection and statistics controls work. Horizontal card/table content stays locally scrollable.
- No browser page exceptions or failed requests in the interaction run. The app reports no Streamlit exception.
- Full Python suite: 570 tests completed successfully, including one optional integration test skipped. The serialized history export was also reproduced from retained inputs with identical bytes and its focused validation tests rerun.
- All 20 protected baseline hashes and the canonical statistics database SHA-256 remain unchanged. Frozen forecast probabilities were not refitted.

The browser run is recorded locally in `results.json`; focused mobile evidence
includes `second-pass-mobile.png`, `second-pass-mobile-chart.png`,
`second-pass-mobile-statistics.png` and `second-pass-mobile-record.png`.
Docker packaging was reviewed, but a local container build was unavailable
because Docker is not installed on this machine. This is a packaging validation
limit, not an untested browser render.

Follow-up polish: a public hosted preview would make portfolio review easier.
Portrait availability still depends on UFC's image host. These do not block
the verified local dashboard.

final result: passed
