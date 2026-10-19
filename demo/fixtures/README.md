# Synthetic offline fixtures

All fighters, events, outcomes and statistics in this directory are invented for an engineering demonstration. They are not UFC observations, predictions or evidence of model quality.

The placeholder `ufcstats.com` URLs satisfy the real parser's identifier contract. No request is made to those addresses. The demo removes these placeholder links from its sample analytics report.

The fixture intentionally contains:

- One missing reach measurement, retained as `NULL`.
- One missing control-time observation, retained as `NULL` while usable strike counts survive.
- One unknown duration schedule: paired counts survive, time-based rates remain unavailable.
- One incomplete paired-round bout, held out of normalized fighter facts while its raw rows remain inspectable.
- A two-bout target card whose own statistics must not enter its pre-fight comparisons.

The capture timestamp is a fixed logical fixture timestamp, not an assertion about a real HTTP fetch. `fixture_manifest.json` pins the exact checked-in CSV bytes and expected quality outcomes.
