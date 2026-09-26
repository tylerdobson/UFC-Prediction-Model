# Dated card and roster observations

The reviewed CSV importer retains the exact input file and an immutable
`ingestion_runs` receipt. It now also writes immutable `card_event_snapshots`
and `card_bout_snapshots` joined to that receipt. These tables preserve the
event status, scheduled start, each supplied bout status, stable fighter IDs,
matchup, and result state as they appeared in that import. Reimporting a later
version of a card adds another snapshot; it never edits an older snapshot.

## Reviewed CSV fields

Use [the template](../examples/bouts_template.csv) as a starting point. Its
fictional rows intentionally leave observation and attribution fields blank.
One row represents one bout. A file can contain several events, but all rows
for a given event must agree on event metadata and observation time. Each
`bout_id` can occur only once in a file. To record a later card change, import
a new file and receipt.

| Optional column | Meaning |
| --- | --- |
| `source_observed_at_utc` | UTC ISO timestamp when the **cited source actually showed this event status and these supplied pairings**, such as `2026-10-10T18:00:00Z`. Offsets are accepted and normalized to UTC. Leave blank if unknown. |
| `source_url` | Exact source or archived revision URL used for the review. |
| `source_revision_id` | Provider revision or archive identifier, if available. |
| `license_name`, `license_url` | Source license and attribution link, when applicable. |
| `reviewed_by` | Local reviewer name or identifier. |
| `event_provider_status`, `bout_provider_status` | Detailed source state, if available; live/delayed states are not pre-fight eligible. |

`source_observed_at_utc` must not be later than the import clock. A missing
value is stored as SQL `NULL` with basis `unknown`; the importer **never**
copies the event date or import clock into this field. A completed-results page
viewed today does not establish that its current pairing was published before
the fight. For a historical pre-fight claim, review an archived, timestamped
revision that actually contains the pairing and retain its exact URL/revision.
The timestamp is still a reviewer assertion, not cryptographic proof; retain
the source evidence and review notes privately. If using Wikimedia content,
include the relevant attribution and license metadata and review its terms.

The optional UFCStats and Sportradar adapters record their own local fetch
time with basis `local_fetch`, linked to each raw response receipt. That means
"this application saw these rows then," not that the provider published them
then. A retrospective fetch after an event is flagged as late. Sportradar's
Daily Summaries may contain only part of a card; the saved rows therefore do
not establish complete card coverage. Provider access rights must be reviewed
separately before using a feed for betting decisions.

Import a current card before the event, then import a new file when a
replacement or cancellation is confirmed. Keep the same stable ID for an
unchanged bout. A changed opponent needs a **new** `bout_id`; mark the old
bout `cancelled` explicitly and add the replacement. A missing row in a later
file is not interpreted as a cancellation, because the file may be a partial
card. The audit compares snapshots and flags possible substitutions for
review. It also reports events without any snapshot, snapshots lacking source
observation times, and observations at or after the event start.

## What the audit can and cannot establish

Run `ufc-model audit` after each import. Relevant findings include
`missing_card_snapshot`, `missing_roster_observation_time`,
`roster_observation_source_missing`, `roster_observed_at_or_after_start`,
`roster_possible_opponent_substitution`,
and `roster_snapshot_matchup_mismatch`. The summary includes snapshot counts,
missing/late timing counts, and possible substitution pairs. A warning calls
for a source review; it is not automatic evidence of a confirmed change.

The `latest_prefight_roster` helper checks the latest imported observation
for an event and bout. It accepts only a dated observation fetched by the
decision time, before the recorded event start, whose scheduled event and
matchup still match the current database. It verifies the selected receipt's
retained payload hash on disk and defaults to a 24-hour maximum roster age.
A missing, linked, unreadable, or changed payload, live/delayed provider
status, missing/late/future observation, changed start/status, cancelled bout,
or absent bout returns a reason instead of a pass. This is one input to the
pre-fight decision gate. The gate must
also verify source rights and identity, current bookmaker quote timing,
model cutoff, price drift, and exposure limits.

These snapshots cover **the rows supplied in a file**, not a proof that an
entire UFC card was complete. Event start is the card-level conservative
cutoff, not each bout's walkout time. A historical source observation time
does not by itself prove that a bookmaker quote was available or executable.
