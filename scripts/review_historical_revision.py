"""Verify saved MediaWiki revision receipts and compare one card offline.

This review never imports historical cards into the operating database and
never enables model or betting alerts. It records which exact linked pairs in
one archived revision agree with the separate accepted-results research DB.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.historical_archive import compare_archived_card, parse_archived_card
from ufc_odds_model.publisher_revisions import ApiReceipt, verify_historical_revision


def _receipt(path: Path) -> tuple[dict, ApiReceipt]:
    if path.is_symlink():
        raise ValueError(f"Receipt sidecar is a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Receipt sidecar is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = json.load(stream)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError("Receipt sidecar must be a JSON object")
    try:
        raw_path = Path(payload["response_path"])
        if not raw_path.is_absolute():
            raw_path = (path.parent / raw_path).resolve()
        receipt = ApiReceipt(
            raw_path, payload["response_sha256"], payload["request_url"],
            payload["fetched_at_utc"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("Receipt sidecar is missing exact response metadata") from exc
    return payload, receipt


def review(
    selection_sidecar: str | Path,
    content_sidecar: str | Path,
    research_db: str | Path,
    kind: str,
) -> dict:
    selection_meta, selection = _receipt(Path(selection_sidecar).expanduser())
    content_meta, content = _receipt(Path(content_sidecar).expanduser())
    expected_role = "prefight" if kind == "scheduled" else "result" if kind == "completed" else None
    if (expected_role is None or selection_meta.get("role") != expected_role
            or selection_meta.get("response_kind") != "selection"):
        raise ValueError("Revision kind differs from selection receipt role")
    page_id = selection_meta.get("page_id")
    cutoff = selection_meta.get("cutoff_utc")
    if type(page_id) is not int or not isinstance(cutoff, str):
        raise ValueError("Selection receipt needs page ID and decision cutoff")
    if (content_meta.get("role") != expected_role
            or content_meta.get("response_kind") != "content"
            or content_meta.get("page_id") != page_id
            or content_meta.get("cutoff_utc") != cutoff):
        raise ValueError("Content receipt page ID or cutoff differs from selection")
    proof = verify_historical_revision(
        selection, content, expected_page_id=page_id, cutoff_at_utc=cutoff,
    )
    card = parse_archived_card(proof, kind)
    path = Path(research_db).expanduser()
    if not path.is_file():
        raise ValueError(f"Research database does not exist: {path}")
    connection = db.connect(path)
    try:
        db.require_current_schema(connection)
        report = compare_archived_card(connection, card)
    finally:
        connection.close()
    return {
        **report,
        "source_page_id": proof.page_id,
        "source_page_title": proof.page_title,
        "source_oldid_url": proof.source_url,
        "license_name": proof.license_name,
        "license_url": proof.license_url,
        "publisher_sha1": proof.publisher_sha1,
        "wikitext_sha256": proof.wikitext_sha256,
        "selection": {
            "request_url": selection.request_url,
            "fetched_at_utc": selection.fetched_at_utc,
            "response_path": str(selection.path),
            "response_sha256": selection.sha256,
        },
        "content": {
            "request_url": content.request_url,
            "fetched_at_utc": content.fetched_at_utc,
            "response_path": str(content.path),
            "response_sha256": content.sha256,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-receipt", required=True, type=Path)
    parser.add_argument("--content-receipt", required=True, type=Path)
    parser.add_argument("--research-db", default="data/ufc_research_2011_2025.sqlite", type=Path)
    parser.add_argument("--kind", choices=("scheduled", "completed"), required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = review(
            args.selection_receipt, args.content_receipt, args.research_db, args.kind,
        )
        if args.output.exists() or args.output.is_symlink():
            raise ValueError(f"Output already exists: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        print(json.dumps({
            "report": str(args.output.resolve()), "kind": report["kind"],
            "source_bouts": report["source_bouts"],
            "matched_bouts": report["matched_bouts"],
            "held_bouts": report["held_bouts"],
            "model_eligible": report["model_eligible"],
        }))
    except (OSError, ValueError) as exc:
        print(f"historical revision review stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
