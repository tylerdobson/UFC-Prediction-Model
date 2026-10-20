# Official ranked-athlete portraits

`rankings-portraits.json` contains observed remote UFC image URLs and capture
receipts for all **203 distinct official athlete links** published across the
captured media/Meta rankings, including division champions and pound-for-pound
lists. All 203 selected images returned HTTP 200 and valid PNGs at 256 × 160.
Their SHA-256 values are distinct. There are no generated faces or invented URLs.

The public file bundles no portrait binaries. Credit: **UFC**. Each athlete URL
comes directly from the official rankings. Its public athlete page was fetched,
and the selected event-results headshot's observed filename matched that same
page's own `og:image` metadata. Opponent images were excluded. The page's own
hero portrait is a supported fallback when an identifiable headshot is absent;
it was not needed in this capture.

The manifest records source and effective URLs, HTTP status, source-page/image
hashes, header hashes, byte counts, dimensions, timestamps and selection basis.
Page/image response-body filesystem timestamps are labeled explicitly; batch
completion observations are retained separately. The manifest capture completed
at `2026-10-20T03:19:02Z`.

Raw public page/image receipts and QA copies are retained separately under the
ignored `data/raw/ranking-portraits/20261020031614Z/` capture when installed by
the project operator. Portraits remain remotely hosted in the dashboard; those
private local captures are not a redistribution license or part of Git assets.
The exact source/capture digests remain in the manifest.

`ranking_portraits.validate_portraits(document, rankings=...)` binds the map to
the raw official rankings hash and exact athlete identities/names/occurrences.
Re-fetching identical raw rankings may have a different capture-specific JSON
digest. `load_portraits(..., as_of=...)` also rejects post-cutoff captures.
Unsuccessful future fetches must remain explicit `unavailable` entries rather
than guessed pictures. Current portraits are display context, never historical
model inputs.

The separate MySQL document contract is `rankings-portraits/current`. Its
`portraits` object is keyed by the exact official `athlete_url`. Only entries
with `status: "available"` should render an `image_url`. The root
`snapshot_sha256` detects changes to the complete metadata document.
