"""Reverify a dated historical pilot cohort from exact retained source files.

Run with ``python -m scripts.review_historical_pilot`` from the project root.
This command reads the accepted-results research DB and local API receipts;
it writes only a new report. Its coverage is not a model evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from scripts.review_historical_revision import review


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be UTC ISO text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be UTC ISO text") from exc
    if (parsed.tzinfo != timezone.utc or parsed.microsecond
            or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value):
        raise ValueError(f"{label} must use canonical UTC seconds and Z")
    return parsed


def validate_manifest(manifest: dict) -> list[dict]:
    """Prevent a changed date or cutoff from silently picking another revision."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Historical pilot manifest needs schema version 1")
    events = manifest.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("Historical pilot manifest has no events")
    seen_slugs: set[str] = set()
    seen_pages: set[int] = set()
    prior_date: date | None = None
    for item in events:
        if not isinstance(item, dict):
            raise ValueError("Historical pilot event must be an object")
        slug, page_id = item.get("slug"), item.get("page_id")
        if (not isinstance(slug, str) or not _SLUG.fullmatch(slug)
                or slug in seen_slugs or type(page_id) is not int or page_id <= 0
                or page_id in seen_pages):
            raise ValueError("Historical pilot has invalid or duplicate event identity")
        seen_slugs.add(slug)
        seen_pages.add(page_id)
        try:
            event_date = date.fromisoformat(item["event_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{slug}: invalid event date") from exc
        if prior_date is not None and event_date <= prior_date:
            raise ValueError("Historical pilot event dates must increase")
        prior_date = event_date
        start = _utc(item.get("earliest_start_at_utc"), f"{slug} start")
        pre = _utc(item.get("prefight_cutoff_at_utc"), f"{slug} pre-fight cutoff")
        post = _utc(item.get("result_cutoff_at_utc"), f"{slug} result cutoff")
        if start - pre != timedelta(hours=24) or post <= start:
            raise ValueError(f"{slug}: cutoffs do not describe a 24-hour pre-event decision")
        # A locally dated international event may start after midnight UTC.
        if abs((start.date() - event_date).days) > 1:
            raise ValueError(f"{slug}: UTC start differs materially from event date")
        url = urlsplit(str(item.get("official_start_url", "")))
        if (url.scheme != "https" or url.netloc not in {"ufc.com", "www.ufc.com"}
                or not url.path.startswith("/news/")):
            raise ValueError(f"{slug}: official start URL must be on ufc.com")
    for earlier, later in zip(events, events[1:]):
        if earlier["result_cutoff_at_utc"] != later["prefight_cutoff_at_utc"]:
            raise ValueError("Each completed result cutoff must equal the next event's decision")
    return events


def _receipt_pair(raw_root: Path, slug: str, role: str, cutoff: str) -> tuple[Path, Path]:
    marker = cutoff.replace(":", "").replace("-", "")
    stem = raw_root / slug / f"{role}-{marker}"
    return Path(f"{stem}.selection.receipt.json"), Path(f"{stem}.content.receipt.json")


def review_cohort(
    manifest_path: str | Path,
    research_db: str | Path,
    raw_root: str | Path,
) -> dict:
    path = Path(manifest_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Historical pilot manifest must be a regular file")
    raw = path.read_bytes()
    manifest = json.loads(raw)
    events = validate_manifest(manifest)
    rows: list[dict] = []
    for item in events:
        slug = item["slug"]
        pre_receipts = _receipt_pair(Path(raw_root), slug, "prefight", item["prefight_cutoff_at_utc"])
        result_receipts = _receipt_pair(Path(raw_root), slug, "result", item["result_cutoff_at_utc"])
        prefight = review(*pre_receipts, research_db, "scheduled")
        completed = review(*result_receipts, research_db, "completed")
        for report, expected_cutoff in ((prefight, item["prefight_cutoff_at_utc"]),
                                        (completed, item["result_cutoff_at_utc"])):
            if (report["event_id"] != f"wikipedia_research:{item['page_id']}"
                    or report["event_date"] != item["event_date"]
                    or report["decision_cutoff_at_utc"] != expected_cutoff):
                raise ValueError(f"{slug}: source page ID, event date, or cutoff changed")
        pre_ids = {entry["bout_id"] for entry in prefight["matches"]
                   if entry["identity_verified"]}
        completed_by_id = {entry["bout_id"]: entry for entry in completed["matches"]
                           if entry["identity_verified"]}
        paired = pre_ids & completed_by_id.keys()
        binary = sum(completed_by_id[bout_id]["outcome"] == "win" for bout_id in paired)
        rows.append({
            "slug": slug,
            "event_id": prefight["event_id"],
            "event_date": item["event_date"],
            "source_page_id": item["page_id"],
            "official_start_url": item["official_start_url"],
            "start_conflict_note": item.get("start_conflict_note"),
            "earliest_start_at_utc": item["earliest_start_at_utc"],
            "prefight_cutoff_at_utc": item["prefight_cutoff_at_utc"],
            "result_cutoff_at_utc": item["result_cutoff_at_utc"],
            "source_bouts": prefight["source_bouts"],
            "prefight_id_verified_bouts": len(pre_ids),
            "result_id_verified_bouts": len(completed_by_id),
            "paired_bouts": len(paired),
            "paired_binary_bouts": binary,
            "prefight_held_bouts": prefight["held_bouts"],
            "prefight_revision_id": prefight["revision_id"],
            "result_revision_id": completed["revision_id"],
            "prefight_revision_published_at_utc": prefight["revision_timestamp_utc"],
            "result_revision_published_at_utc": completed["revision_timestamp_utc"],
        })
    return {
        "purpose": "retrospective source coverage, not model validation",
        "model_eligible": False,
        "manifest_path": str(path.resolve()),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "research_database_path": str(Path(research_db).expanduser().resolve()),
        "events": rows,
        "totals": {key: sum(row[key] for row in rows) for key in (
            "source_bouts", "prefight_id_verified_bouts", "result_id_verified_bouts",
            "paired_bouts", "paired_binary_bouts", "prefight_held_bouts",
        )} | {"event_dates": len(rows)},
        "remaining_evidence": [
            "completed revisions for all earlier events at every later target cutoff",
            "enough verified binary bouts across chronological train, validation, and test dates",
            "historical priced bookmaker baseline or prospective paper results",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("docs/HISTORICAL_2026_PILOT_CARDS.json"))
    parser.add_argument("--research-db", type=Path, default=Path("data/ufc_research_2011_2025.sqlite"))
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/historical-revisions"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = review_cohort(args.manifest, args.research_db, args.raw_root)
        if args.output.exists() or args.output.is_symlink():
            raise ValueError(f"Output already exists: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        print(json.dumps({"output": str(args.output.resolve()), "totals": report["totals"],
                          "model_eligible": report["model_eligible"]}))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"historical pilot review stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
