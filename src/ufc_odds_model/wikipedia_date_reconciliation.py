"""Import four individually reviewed UFC cards with conflicting calendar dates.

The stored date follows the cited UFC event listing. Both original Wikipedia
dates, exact page revisions, and the review evidence remain in the JSON report.
These retrospective dates do not establish a pre-fight observation time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from . import db
from .pipeline import utc_now, utc_string
from .raw_snapshots import retain_snapshot
from .wikipedia_catalog import CatalogEvent, CatalogYear, enumerate_event_catalog
from .wikipedia_history import (
    MediaWikiClient, ParsedEvent, _page_api_url, _record_action_lookups,
    import_wikipedia_pages, parse_event_page_title,
)


@dataclass(frozen=True)
class DateReview:
    year: int
    event_number: int
    title: str
    page_id: int
    catalog_date: str
    page_date: str
    accepted_date: str
    catalog_revision_id: int
    page_revision_id: int
    ufc_evidence_url: str


# Checked against the source revisions and UFC event listings on 2026-09-26.
# A subsequent edit requires another review instead of a blanket ±1-day rule.
REVIEWS = (
    DateReview(2015, 338, "UFC 193", 47434468, "2015-11-15", "2015-11-14",
               "2015-11-14", 1342518299, 1339758581,
               "https://www.ufc.com/event/ufc-193"),
    DateReview(2018, 458, "UFC Fight Night: Blaydes vs. Ngannou 2", 57859555,
               "2018-11-25", "2018-11-24", "2018-11-24", 1354697351,
               1334975657, "https://www.ufc.com/event/ufc-china-2018"),
    DateReview(2020, 544, "UFC on ESPN: Hermansson vs. Vettori", 64996911,
               "2020-12-06", "2020-12-05", "2020-12-05", 1375689810,
               1353831208,
               "https://www.ufc.com/event/ufc-fight-night-december-05-2020"),
    DateReview(2020, 525, "UFC on ESPN: Whittaker vs. Till", 64261712,
               "2020-07-25", "2020-07-26", "2020-07-25", 1375689810,
               1356909684,
               "https://www.ufc.com/event/ufc-fight-night-july-25-2020"),
)


def reviewed_event(
    review: DateReview, year: CatalogYear, entry: CatalogEvent, parsed: ParsedEvent,
) -> ParsedEvent:
    """Accept only the exact reviewed source revisions and original date pair."""
    if (year.year != review.year or year.revision_id != review.catalog_revision_id
            or entry.event_number != review.event_number or entry.page_title != review.title
            or entry.page_id != review.page_id or entry.event_date != review.catalog_date
            or parsed.title != review.title or parsed.page_id != review.page_id
            or parsed.revision_id != review.page_revision_id
            or parsed.event_date != review.page_date):
        raise ValueError(f"Date review evidence changed for {review.title}; review again")
    return replace(parsed, event_date=review.accepted_date)


def import_reviewed_date_cards(
    connection: sqlite3.Connection,
    *,
    raw_dir: str | Path = "data/raw/wikipedia/date_review",
    review_out: str | Path = "reports/wikipedia_date_reconciliation.json",
    fetch_json: Callable[[str], dict] | None = None,
) -> dict:
    """Retain catalog/event receipts and import only four checked result cards."""
    fetch = fetch_json or MediaWikiClient()
    raw_directory = Path(raw_dir)
    recorded_fetch = _record_action_lookups(connection, fetch, raw_directory)
    catalogs = {
        year: enumerate_event_catalog(year, year, fetch_json=recorded_fetch)
        for year in sorted({review.year for review in REVIEWS})
    }
    pages: list[tuple[dict, ParsedEvent]] = []
    reviewed_rows = []
    for review in REVIEWS:
        catalog = catalogs[review.year]
        entries = [entry for entry in catalog.events
                   if entry.event_number == review.event_number]
        if len(entries) != 1:
            raise ValueError(f"Date review catalog entry changed for {review.title}")
        payload = fetch(_page_api_url(review.title))
        parsed = parse_event_page_title(payload, review.title)
        accepted = reviewed_event(review, catalog.years[0], entries[0], parsed)
        pages.append((payload, accepted))
        reviewed_rows.append({
            **asdict(review),
            "catalog_source_url": catalog.years[0].source_url,
            "page_source_revision_url": (
                f"https://en.wikipedia.org/w/index.php?oldid={review.page_revision_id}"
            ),
            "reviewed_at_utc": utc_string(utc_now()),
            "basis": "explicit UFC event-listing calendar date; research outcomes only",
        })
    catalog_receipts = []
    for year, catalog in catalogs.items():
        source = catalog.years[0]
        raw = json.dumps(
            source.raw_payload, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        path, digest = retain_snapshot(raw, raw_directory / "catalog", kind="Wikipedia catalog")
        with connection:
            run = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("wikipedia_catalog", utc_string(utc_now()), str(path), digest),
            )
        catalog_receipts.append({
            "year": year, "page_id": source.page_id,
            "revision_id": source.revision_id,
            "ingestion_run_id": int(run.lastrowid), "sha256": digest,
        })
    return import_wikipedia_pages(
        connection, pages, raw_dir=raw_directory,
        review_out=review_out, fetch_json=fetch,
        review_scope={
            "date_reconciliation": reviewed_rows,
            "catalog_year_receipts": catalog_receipts,
            "research_only": True,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--raw-dir", default="data/raw/wikipedia/date_review")
    parser.add_argument("--review-out", default="reports/wikipedia_date_reconciliation.json")
    args = parser.parse_args(argv)
    if Path(args.db).resolve() == db.DEFAULT_DB.resolve():
        parser.error("Use an explicit separate --db path for Wikipedia research history")
    with closing(db.connect(args.db)) as connection:
        db.require_current_schema(connection)
        result = import_reviewed_date_cards(
            connection, raw_dir=args.raw_dir, review_out=args.review_out,
        )
    print(json.dumps({key: result[key] for key in (
        "events", "source_bouts", "imported_bouts", "skipped_unresolved_bouts",
        "review_out",
    )}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
