# Pre-fight alert and data latency policy

The current build produces **pre-fight** predictions and local paper decisions. It does not model round-by-round action or place orders. `score-event` stops at the known event start; an alert must also pass a fresh-quote check immediately before it is displayed.

For a pre-fight alert, keep these times distinct:

- `feature_cutoff_at_utc`: latest information the prediction was allowed to use.
- `bookmaker_updated_at_utc`: the bookmaker's last reported market update.
- `captured_at_utc`: when this project received the price snapshot.
- `alert_checked_at_utc`: when the local alert gate examined that snapshot.
- `start_time_utc`: the earliest known start of the UFC card.

An alert should be rejected when a time is missing or out of order, the price came from an older ingestion run, the bookmaker update or capture is stale, the event has started, or the model's expected profit no longer clears a configured margin after adverse price movement. The margin is a stress test; it does **not** guarantee that a sportsbook will accept the displayed price. Refresh the market again when a human acts.

The proposed 5–30 second lag for public live fight metrics is a source-specific hypothesis, not a reliable upper bound. Future in-play modeling would need the actual observation time of each metric, its publication and fetch times, and a contemporaneous executable market quote. A wider modeled edge alone cannot recover events that happened during an unknown data delay. Until those timestamps and live settlement behavior can be verified, this project should not generate in-play betting alerts.
