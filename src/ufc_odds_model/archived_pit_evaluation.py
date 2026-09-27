"""Research-only point-in-time replay of retained historical event revisions.

This evaluator never changes SQLite or the operating model gate. MediaWiki
publisher revisions are selected at old cutoffs but were fetched later; their
real fetch timestamps remain intact. A missing prior-event revision holds the
whole target event instead of silently using today's result table as history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import db
from .archive_identity import _requested_titles
from .elo import MODEL_VERSION as ELO_VERSION
from .features import FEATURE_NAMES, FeatureBuilder, FeatureRow
from .historical_archive import compare_archived_card, parse_archived_card
from .logistic import (
    MODEL_VERSION as LOGISTIC_VERSION,
    binary_metrics, chronological_event_split, fit_logistic, fit_temperature,
)
from .publisher_revisions import ApiReceipt, verify_historical_revision
from .research_evaluation import _calibration_bins, _paired_event_bootstrap


EVALUATION_VERSION = "archived-publisher-pit-research-v1"
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ValueError(f"{label} must be canonical UTC seconds")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid UTC timestamp") from exc


def _events(manifest: object) -> list[dict]:
    """Validate the checked 24-hour event manifest before reading any proof."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Historical manifest must have schema version 1")
    events = manifest.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("Historical manifest has no events")
    seen_slugs: set[str] = set()
    seen_pages: set[int] = set()
    previous_date: date | None = None
    for item in events:
        if not isinstance(item, dict):
            raise ValueError("Historical event must be an object")
        slug, page_id = item.get("slug"), item.get("page_id")
        if (not isinstance(slug, str) or not _SLUG.fullmatch(slug)
                or slug in seen_slugs or type(page_id) is not int or page_id <= 0
                or page_id in seen_pages):
            raise ValueError("Historical event has invalid or duplicate identity")
        seen_slugs.add(slug)
        seen_pages.add(page_id)
        try:
            event_date = date.fromisoformat(item["event_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{slug}: invalid event date") from exc
        if previous_date is not None and event_date <= previous_date:
            raise ValueError("Historical event dates must increase")
        previous_date = event_date
        start = _utc(item.get("earliest_start_at_utc"), f"{slug} start")
        prefight = _utc(item.get("prefight_cutoff_at_utc"), f"{slug} pre-fight cutoff")
        result = _utc(item.get("result_cutoff_at_utc"), f"{slug} result cutoff")
        if start - prefight != timedelta(hours=24) or result <= start:
            raise ValueError(f"{slug}: cutoffs do not describe a 24-hour decision")
        if abs((start.date() - event_date).days) > 1:
            raise ValueError(f"{slug}: UTC start differs materially from event date")
        url = urlsplit(str(item.get("official_start_url", "")))
        if (url.scheme != "https" or url.netloc not in {"ufc.com", "www.ufc.com"}
                or not url.path.startswith("/news/")):
            raise ValueError(f"{slug}: official start URL must be on ufc.com")
    for earlier, later in zip(events, events[1:]):
        if earlier["result_cutoff_at_utc"] != later["prefight_cutoff_at_utc"]:
            raise ValueError("Result cutoff must equal the next event's decision cutoff")
    return events


def _receipt_path(raw_root: Path, slug: str, role: str, cutoff: str, kind: str) -> Path:
    marker = cutoff.replace(":", "").replace("-", "")
    return raw_root / slug / f"{role}-{marker}.{kind}.receipt.json"


def _sidecar(path: Path, *, page_id: int, cutoff: str, role: str,
             kind: str) -> tuple[ApiReceipt, str]:
    """Bind a sidecar to its fixed sibling response and exact requested role."""
    if path.is_symlink():
        raise ValueError(f"Receipt sidecar is a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Receipt sidecar is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read()
            payload = json.loads(raw)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict) or any((
        payload.get("page_id") != page_id,
        payload.get("cutoff_utc") != cutoff,
        payload.get("role") != role,
        payload.get("response_kind") != kind,
    )):
        raise ValueError(f"Receipt sidecar page, cutoff, or role changed: {path}")
    expected_response = path.with_name(path.name.replace(".receipt.json", ".response.json"))
    try:
        actual_response = Path(payload["response_path"])
        if not actual_response.is_absolute():
            actual_response = path.parent / actual_response
        if actual_response.resolve() != expected_response.resolve():
            raise ValueError("Receipt response path differs from its fixed sibling")
        return (ApiReceipt(actual_response, payload["response_sha256"],
                           payload["request_url"], payload["fetched_at_utc"]),
                hashlib.sha256(raw).hexdigest())
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Receipt sidecar lacks response metadata: {path}") from exc


def review_archived_card(
    connection: sqlite3.Connection, item: dict, role: str,
    cutoff: str, raw_root: Path,
) -> dict:
    """Reverify the publisher's latest-at-cutoff query and compare exact IDs."""
    if role not in {"prefight", "result"}:
        raise ValueError("Archived card role must be prefight or result")
    selection, selection_sidecar_sha = _sidecar(
        _receipt_path(raw_root, item["slug"], role, cutoff, "selection"),
        page_id=item["page_id"], cutoff=cutoff, role=role, kind="selection",
    )
    content, content_sidecar_sha = _sidecar(
        _receipt_path(raw_root, item["slug"], role, cutoff, "content"),
        page_id=item["page_id"], cutoff=cutoff, role=role, kind="content",
    )
    proof = verify_historical_revision(
        selection, content, expected_page_id=item["page_id"], cutoff_at_utc=cutoff,
    )
    kind = "scheduled" if role == "prefight" else "completed"
    card = parse_archived_card(proof, kind)
    if card.event_date != item["event_date"]:
        raise ValueError("Archived card event date differs from manifest")
    if role == "result" and _utc(proof.revision_timestamp_utc, "result publication") < _utc(
        item["earliest_start_at_utc"], "event start"
    ):
        raise ValueError("Completed revision predates event start")
    compared = compare_archived_card(connection, card)
    if (compared["event_id"] != f"wikipedia_research:{item['page_id']}"
            or compared["event_date"] != item["event_date"]
            or compared["decision_cutoff_at_utc"] != cutoff
            or compared["kind"] != kind):
        raise ValueError("Archived comparison differs from manifest identity or cutoff")
    return {
        **compared,
        "role": role,
        "slug": item["slug"],
        "cutoff_at_utc": cutoff,
        "source_oldid_url": proof.source_url,
        "license_name": proof.license_name,
        "license_url": proof.license_url,
        "publisher_sha1": proof.publisher_sha1,
        "wikitext_sha256": proof.wikitext_sha256,
        "selection_sidecar_sha256": selection_sidecar_sha,
        "selection_sha256": selection.sha256,
        "content_sidecar_sha256": content_sidecar_sha,
        "content_sha256": content.sha256,
        "selection_fetched_at_utc": selection.fetched_at_utc,
        "content_fetched_at_utc": content.fetched_at_utc,
    }


def _verified_matches(report: dict, *, completed: bool) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for match in report["matches"]:
        if match.get("identity_verified") is not True:
            continue
        bout_id = match.get("bout_id")
        a_id, b_id = match.get("fighter_a_id"), match.get("fighter_b_id")
        if (not isinstance(bout_id, str) or not bout_id
                or not isinstance(a_id, str) or not isinstance(b_id, str)
                or not a_id or not b_id or a_id == b_id or bout_id in result):
            raise ValueError("Archived comparison has invalid or duplicate stable bout IDs")
        lookup_ids = match.get("identity_lookup_run_ids")
        if (not isinstance(lookup_ids, list) or not lookup_ids
                or any(type(value) is not int or value <= 0 for value in lookup_ids)
                or lookup_ids != sorted(set(lookup_ids))):
            raise ValueError("Archived stable match lacks exact identity lookup receipt IDs")
        if completed:
            outcome, winner = match.get("outcome"), match.get("winner_fighter_id")
            if (outcome not in {"win", "draw", "no_contest"}
                    or (outcome == "win" and winner not in {a_id, b_id})
                    or (outcome != "win" and winner is not None)):
                raise ValueError("Archived completed match has invalid outcome or winner")
        result[bout_id] = match
    return result


def _summary(rows: list[FeatureRow]) -> dict:
    dates = sorted({row.event_date for row in rows})
    return {"binary_bouts": len(rows), "events": len({row.event_id for row in rows}),
            "event_dates": len(dates), "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None}


def _hold(slug: str, reason: str, **detail: object) -> dict:
    return {"slug": slug, "reason": reason, **detail}


def _sha256_regular_file(path: Path) -> str:
    """Hash one retained file without following a linked leaf."""
    if path.is_symlink():
        raise ValueError(f"Evidence file is a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Evidence file is not regular: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError(f"Evidence file is a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Evidence file is not regular: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _guard_standalone_database(path: Path) -> None:
    """A main-file digest cannot represent uncheckpointed journal contents."""
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError(f"Research database has a nonempty {suffix} sidecar")


def _lookup_receipts(connection: sqlite3.Connection, run_ids: set[int]) -> list[dict]:
    """Bind used fighter identities to the exact retained lookup payloads."""
    evidence: list[dict] = []
    for run_id in sorted(run_ids):
        row = connection.execute(
            """SELECT run_id, source, fetched_at_utc, payload_path, sha256
               FROM ingestion_runs WHERE run_id = ?""", (run_id,),
        ).fetchone()
        if row is None or row["source"] != "wikipedia_action_api":
            raise ValueError(f"Identity lookup receipt {run_id} is missing or wrong source")
        path = Path(str(row["payload_path"])).expanduser()
        try:
            raw = _read_regular_file(path)
            actual_hash = hashlib.sha256(raw).hexdigest()
            if actual_hash != row["sha256"]:
                raise ValueError("payload hash changed")
            envelope = json.loads(raw)
            if (not isinstance(envelope, dict)
                    or not isinstance(envelope.get("response"), dict)
                    or not isinstance(envelope.get("request_url"), str)):
                raise ValueError("invalid lookup envelope")
            _requested_titles(envelope["request_url"])
            fetched = _utc(row["fetched_at_utc"], "identity lookup fetch")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Identity lookup receipt {run_id} is invalid: {exc}") from exc
        evidence.append({
            "run_id": run_id,
            "source": row["source"],
            "fetched_at_utc": fetched.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "payload_path": str(path.resolve()),
            "payload_sha256": actual_hash,
            "request_url": envelope["request_url"],
        })
    return evidence


def evaluate_archived_pit(
    manifest_path: str | Path,
    research_db: str | Path,
    raw_root: str | Path,
) -> dict:
    """Return reproducible research metrics from a complete revision matrix.

    Each target is held until all earlier cohort events have a separately
    selected completed revision at that target's exact decision time. Only
    exact stable-ID source matches contribute to labels and earlier history.
    The result is never an operating credential, even when model metrics exist.
    """
    manifest_file = Path(manifest_path).expanduser()
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ValueError("Historical manifest must be a regular file")
    manifest_raw = manifest_file.read_bytes()
    events = _events(json.loads(manifest_raw))
    database_input = Path(research_db).expanduser()
    if database_input.is_symlink() or not database_input.is_file():
        raise ValueError("Existing research database is required")
    database = database_input.resolve()
    _guard_standalone_database(database)
    database_sha256 = _sha256_regular_file(database)
    root = Path(raw_root).expanduser()
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    rows: list[FeatureRow] = []
    holds: list[dict] = []
    event_coverage: list[dict] = []
    proof_evidence: list[dict] = []
    proof_info_by_key: dict[tuple[str, str, str], dict] = {}
    included_by_proof: dict[tuple[str, str, str], dict[str, dict]] = {}
    proof_cache: dict[tuple[str, str, str], dict] = {}

    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            raise ValueError("Research database WAL mode cannot be bound to one main-file SHA-256")
        connection.execute("BEGIN")
        db.require_current_schema(connection)

        def checked(item: dict, role: str, cutoff: str) -> dict:
            key = item["slug"], role, cutoff
            if key not in proof_cache:
                report = review_archived_card(connection, item, role, cutoff, root)
                kind = "scheduled" if role == "prefight" else "completed"
                if (report.get("slug") != item["slug"]
                        or report.get("role") != role
                        or report.get("kind") != kind
                        or report.get("event_id") != f"wikipedia_research:{item['page_id']}"
                        or report.get("event_date") != item["event_date"]
                        or report.get("cutoff_at_utc") != cutoff
                        or report.get("decision_cutoff_at_utc") != cutoff
                        or type(report.get("source_bouts")) is not int
                        or report["source_bouts"] <= 0):
                    raise ValueError("Reviewed proof differs from expected event, role, or cutoff")
                for field in ("selection_sidecar_sha256", "selection_sha256",
                              "content_sidecar_sha256", "content_sha256",
                              "wikitext_sha256"):
                    digest = report.get(field)
                    if (not isinstance(digest, str) or len(digest) != 64
                            or any(char not in "0123456789abcdef" for char in digest)):
                        raise ValueError(f"Reviewed proof lacks valid {field}")
                _utc(report.get("selection_fetched_at_utc"), "selection fetch")
                _utc(report.get("content_fetched_at_utc"), "content fetch")
                proof_cache[key] = report
                proof_info = {
                    "slug": item["slug"], "role": role, "cutoff_at_utc": cutoff,
                    "revision_id": report["revision_id"],
                    "revision_timestamp_utc": report["revision_timestamp_utc"],
                    "source_oldid_url": report.get("source_oldid_url"),
                    "license_name": report.get("license_name"),
                    "license_url": report.get("license_url"),
                    "publisher_sha1": report.get("publisher_sha1"),
                    "wikitext_sha256": report.get("wikitext_sha256"),
                    "selection_sidecar_sha256": report.get("selection_sidecar_sha256"),
                    "selection_sha256": report.get("selection_sha256"),
                    "content_sidecar_sha256": report.get("content_sidecar_sha256"),
                    "content_sha256": report.get("content_sha256"),
                    "selection_fetched_at_utc": report.get("selection_fetched_at_utc"),
                    "content_fetched_at_utc": report.get("content_fetched_at_utc"),
                    "source_holds_by_reason": dict(sorted(Counter(
                        hold.get("reason", "unknown") for hold in report.get("holds", [])
                    ).items())),
                    "identity_holds_by_reason": dict(sorted(Counter(
                        hold.get("reason", "unknown") for hold in report.get("identity_holds", [])
                    ).items())),
                }
                proof_evidence.append(proof_info)
                proof_info_by_key[key] = proof_info
            return proof_cache[key]

        def include(report: dict, match: dict) -> None:
            key = report["slug"], report["role"], report["cutoff_at_utc"]
            binding = {
                "bout_id": match["bout_id"],
                "fighter_a_id": match["fighter_a_id"],
                "fighter_b_id": match["fighter_b_id"],
                "identity_lookup_run_ids": list(match["identity_lookup_run_ids"]),
            }
            previous = included_by_proof.setdefault(key, {}).get(match["bout_id"])
            if previous is not None and previous != binding:
                raise ValueError("A source proof gives conflicting identity receipts for one bout")
            included_by_proof[key][match["bout_id"]] = binding

        for index, item in enumerate(events):
            slug = item["slug"]
            cutoff = item["prefight_cutoff_at_utc"]
            history_before = min(item["event_date"], cutoff[:10])
            try:
                scheduled = checked(item, "prefight", cutoff)
                completed = checked(item, "result", item["result_cutoff_at_utc"])
                pre_matches = _verified_matches(scheduled, completed=False)
                result_matches = _verified_matches(completed, completed=True)
            except FileNotFoundError as exc:
                holds.append(_hold(slug, "missing_target_proof", detail=str(exc)))
                continue
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                holds.append(_hold(slug, "invalid_target_proof", detail=str(exc)))
                continue

            paired = []
            pair_holds = 0
            for bout_id, pre_match in pre_matches.items():
                result = result_matches.get(bout_id)
                if result is None:
                    pair_holds += 1
                    continue
                if (pre_match["fighter_a_id"] != result["fighter_a_id"]
                        or pre_match["fighter_b_id"] != result["fighter_b_id"]):
                    pair_holds += 1
                    continue
                if result["outcome"] == "win":
                    paired.append(result)
            prior_reports: list[dict] = []
            missing_prior: list[str] = []
            invalid_prior: list[dict] = []
            for prior in events[:index]:
                try:
                    report = checked(prior, "result", cutoff)
                    _verified_matches(report, completed=True)
                    prior_reports.append(report)
                except FileNotFoundError:
                    missing_prior.append(prior["slug"])
                except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    invalid_prior.append({"slug": prior["slug"], "detail": str(exc)})
            if missing_prior or invalid_prior:
                holds.append(_hold(slug, "incomplete_prior_revision_matrix",
                                   missing_prior_events=missing_prior,
                                   invalid_prior_events=invalid_prior,
                                   paired_binary_bouts_held=len(paired)))
                event_coverage.append({
                    "slug": slug, "event_date": item["event_date"],
                    "source_bouts": scheduled["source_bouts"],
                    "paired_binary_bouts": len(paired), "scored_binary_bouts": 0,
                    "unpaired_or_identity_held_bouts": scheduled["source_bouts"] - len(paired),
                    "prior_events_required": len(prior_reports) + len(missing_prior) + len(invalid_prior),
                    "prior_events_verified": len(prior_reports),
                })
                continue

            history: list[dict] = []
            history_bindings: list[tuple[dict, dict]] = []
            source_history_bouts = 0
            seen_history_ids: set[str] = set()
            try:
                for report in prior_reports:
                    # A prior event can be listed chronologically yet share the
                    # target's UTC decision date. Its proof is still required,
                    # but without bout start times its result stays out of
                    # the feature replay for this target.
                    if report["event_date"] >= history_before:
                        continue
                    source_history_bouts += int(report["source_bouts"])
                    for match in _verified_matches(report, completed=True).values():
                        bout_id = str(match["bout_id"])
                        if bout_id in seen_history_ids:
                            raise ValueError("Duplicate prior bout ID across archived events")
                        seen_history_ids.add(bout_id)
                        history_bindings.append((report, match))
                        history.append({
                            "bout_id": bout_id, "event_id": report["event_id"],
                            "event_date": report["event_date"],
                            "fighter_a_id": match["fighter_a_id"],
                            "fighter_b_id": match["fighter_b_id"],
                            "outcome": match["outcome"],
                            "winner_fighter_id": match["winner_fighter_id"],
                        })
            except (KeyError, TypeError, ValueError) as exc:
                holds.append(_hold(slug, "invalid_prior_result_history", detail=str(exc),
                                   paired_binary_bouts_held=len(paired)))
                event_coverage.append({
                    "slug": slug, "event_date": item["event_date"],
                    "source_bouts": scheduled["source_bouts"],
                    "paired_binary_bouts": len(paired), "scored_binary_bouts": 0,
                    "unpaired_or_identity_held_bouts": scheduled["source_bouts"] - len(paired),
                    "prior_events_required": len(prior_reports),
                    "prior_events_verified": 0,
                })
                continue
            history.sort(key=lambda entry: (entry["event_date"], entry["event_id"], entry["bout_id"]))
            builder = FeatureBuilder()
            for _, same_date in groupby(history, key=lambda entry: entry["event_date"]):
                builder.update_group(list(same_date))
            scored_history = sum(entry["outcome"] != "no_contest" for entry in history)
            for result in paired:
                first, second = sorted((result["fighter_a_id"], result["fighter_b_id"]))
                features, coverage = builder.values_with_coverage(first, second, item["event_date"])
                rows.append(FeatureRow(
                    bout_id=result["bout_id"], event_id=scheduled["event_id"],
                    event_date=item["event_date"], fighter_a_id=first, fighter_b_id=second,
                    features=features, target=int(result["winner_fighter_id"] == first),
                    coverage=coverage, prior_result_rows_available=source_history_bouts,
                    prior_result_rows_observed=scored_history,
                ))
                include(scheduled, pre_matches[result["bout_id"]])
                include(completed, result)
            if paired:
                for prior_report, match in history_bindings:
                    include(prior_report, match)
            if pair_holds or scheduled["source_bouts"] != len(paired):
                holds.append(_hold(slug, "unpaired_or_identity_held_target_bouts",
                                   source_bouts=scheduled["source_bouts"],
                                   paired_binary_bouts=len(paired)))
            event_coverage.append({
                "slug": slug, "event_date": item["event_date"],
                "source_bouts": scheduled["source_bouts"],
                "paired_binary_bouts": len(paired), "scored_binary_bouts": len(paired),
                "unpaired_or_identity_held_bouts": scheduled["source_bouts"] - len(paired),
                "prior_events_required": len(prior_reports),
                "prior_events_verified": len(prior_reports),
                "prior_events_in_feature_history": sum(
                    report["event_date"] < history_before for report in prior_reports),
                "prior_source_bouts": source_history_bouts,
                "prior_stable_id_result_bouts": len(history),
                "prior_scored_result_bouts": scored_history,
            })

        used_lookup_ids: set[int] = set()
        for key, proof_info in proof_info_by_key.items():
            bindings = included_by_proof.get(key, {})
            proof_info["included_matches"] = [
                binding for _, binding in sorted(bindings.items())
            ]
            for binding in bindings.values():
                used_lookup_ids.update(binding["identity_lookup_run_ids"])
        identity_lookup_receipts = _lookup_receipts(connection, used_lookup_ids)

    _guard_standalone_database(database)
    if _sha256_regular_file(database) != database_sha256:
        raise ValueError("Research database changed during archived evaluation")
    rows.sort(key=lambda row: (row.event_date, row.event_id, row.bout_id))
    report: dict = {
        "schema_version": 1, "evaluation_version": EVALUATION_VERSION,
        "status": "research_only", "research_only": True,
        "model_eligible": False, "promotion_eligible": False,
        "source": "retained_mediawiki_latest_revision_at_each_cutoff",
        "manifest_path": str(manifest_file.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "raw_receipt_root": str(root.resolve()),
        "raw_receipt_naming_rule": "{root}/{slug}/{role}-{cutoff_without_colons_or_hyphens}.{selection|content}.{receipt|response}.json",
        "research_database_path": str(database),
        "research_database_sha256": database_sha256,
        "database_access": "read_only",
        "feature_history_rule": "verified_prior_event_result_revisions_at_each_target_cutoff;strictly_earlier_dates;no_profiles_or_stats",
        "feature_names": FEATURE_NAMES,
        "models": {"elo": ELO_VERSION, "logistic": LOGISTIC_VERSION},
        "feature_rows_sha256": hashlib.sha256(json.dumps([
            [row.bout_id, row.event_date, row.fighter_a_id, row.fighter_b_id,
             row.features, row.target, row.prior_result_rows_available,
             row.prior_result_rows_observed] for row in rows
        ], separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
        "event_coverage": event_coverage,
        "proof_evidence": proof_evidence,
        "identity_lookup_receipts": identity_lookup_receipts,
        "holds": holds,
        "coverage": {
            "manifest_events": len(events),
            "events_with_verified_target_and_history": sum(
                event["scored_binary_bouts"] > 0 for event in event_coverage),
            "source_bouts_in_reviewed_targets": sum(event["source_bouts"] for event in event_coverage),
            "paired_binary_bouts_in_reviewed_targets": sum(
                event["paired_binary_bouts"] for event in event_coverage),
            "scored_binary_bouts": len(rows),
            "held_target_events": len(events) - sum(
                event["scored_binary_bouts"] > 0 for event in event_coverage),
        },
        "bookmaker": {"status": "not_evaluated_without_historical_point_in_time_prices",
                      "available_bouts": 0, "roi": None},
        "limits": [
            "Research cohort begins at the manifest's first event; earlier fighter history is left truncated.",
            "Fighter title-to-page-ID receipts were fetched retrospectively and are not independent pre-fight identity observations.",
            "Held source bouts create selection bias; metrics describe only paired, stable-ID verified fights.",
            "The later event slice overlaps a previously inspected smaller pilot and is exploratory, not untouched model-selection evidence.",
            "The official start URL is manually checked in the manifest, not an immutable official source receipt.",
            "Saved API responses and digests are not publisher signatures; acquisition provenance still needs review.",
            "No historical bookmaker price baseline or betting return is inferred.",
        ],
    }
    report["checked_input_sha256"] = hashlib.sha256(json.dumps({
        "manifest_sha256": report["manifest_sha256"],
        "research_database_sha256": database_sha256,
        "proof_evidence": proof_evidence,
        "identity_lookup_receipts": identity_lookup_receipts,
        "feature_rows_sha256": report["feature_rows_sha256"],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    try:
        train, validation, test = chronological_event_split(rows)
    except ValueError as exc:
        report["evaluation_status"] = "insufficient_verified_history"
        report["reason"] = str(exc)
        report["split"] = None
        report["calibration"] = None
        report["test"] = None
        return report

    model = fit_logistic(train)
    report["models"]["logistic_weights_sha256"] = hashlib.sha256(json.dumps(
        model.weights, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    validation_probs = [model.predict_probability(row.features) for row in validation]
    calibrator = fit_temperature(validation_probs, [int(row.target) for row in validation])
    outcomes = [int(row.target) for row in test]
    elo_probs = [1.0 / (1.0 + 10.0 ** (-row.features[0])) for row in test]
    raw_probs = [model.predict_probability(row.features) for row in test]
    calibrated_probs = [calibrator.predict_probability(value) for value in raw_probs]
    report.update({
        "evaluation_status": "exploratory_chronological_holdout",
        "split": {name: _summary(group) for name, group in (
            ("train", train), ("validation", validation), ("test", test))},
        "calibration": {"method": "symmetric_temperature_on_validation_only",
                        "validation_binary_bouts": len(validation),
                        "applied": len(validation) >= 30, "scale": calibrator.scale},
        "test": {
            "elo": {"metrics": binary_metrics(elo_probs, outcomes),
                    "calibration_bins": _calibration_bins(elo_probs, outcomes)},
            "logistic_uncalibrated": {
                "metrics": binary_metrics(raw_probs, outcomes),
                "calibration_bins": _calibration_bins(raw_probs, outcomes)},
            "logistic_calibrated": {
                "metrics": binary_metrics(calibrated_probs, outcomes),
                "calibration_bins": _calibration_bins(calibrated_probs, outcomes)},
        },
        "test_uncertainty": _paired_event_bootstrap(test, elo_probs, calibrated_probs),
        "sample_size_flags": {
            "train_under_50_bouts": len(train) < 50,
            "validation_under_30_bouts": len(validation) < 30,
            "test_under_30_bouts": len(test) < 30,
            "warning": "Counts, outcome selection, and left-truncated history prevent operating model promotion.",
        },
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("docs/HISTORICAL_2026_PILOT_CARDS.json"))
    parser.add_argument("--research-db", type=Path, default=Path("data/ufc_research_2011_2025.sqlite"))
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/historical-revisions"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate_archived_pit(args.manifest, args.research_db, args.raw_root)
        if args.output.exists() or args.output.is_symlink():
            raise ValueError(f"Output already exists: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True,
                      allow_nan=False)
            stream.write("\n")
        print(json.dumps({"output": str(args.output.resolve()),
                          "status": report["status"],
                          "evaluation_status": report["evaluation_status"],
                          "coverage": report["coverage"]}))
    except (OSError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"archived PIT evaluation stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
