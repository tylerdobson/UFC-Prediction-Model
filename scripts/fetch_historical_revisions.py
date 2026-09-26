"""Retain MediaWiki's latest-at-cutoff selection and exact revision content.

This is retrospective publisher evidence. The recorded fetch time is the real
download time, never an invented pre-fight observation. No bets or alerts are
created here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ufc_odds_model.publisher_revisions import ApiReceipt, verify_historical_revision
from ufc_odds_model.wikipedia_history import ACTION_API, USER_AGENT


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def canonical_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Use UTC timestamps in YYYY-MM-DDTHH:MM:SSZ format")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Use UTC timestamps in YYYY-MM-DDTHH:MM:SSZ format") from exc
    if (parsed.tzinfo != timezone.utc or parsed.microsecond
            or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value):
        raise ValueError("Use UTC timestamps in YYYY-MM-DDTHH:MM:SSZ format")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _url(parameters: dict[str, str]) -> str:
    return f"{ACTION_API}?{urllib.parse.urlencode(parameters)}"


def _base_parameters() -> dict[str, str]:
    return {"action": "query", "prop": "revisions", "format": "json",
            "formatversion": "2", "rvslots": "main", "maxlag": "5"}


def selection_url(page_id: int, cutoff_utc: str) -> str:
    if page_id <= 0:
        raise ValueError("MediaWiki page ID must be positive")
    canonical_utc(cutoff_utc)
    return _url({**_base_parameters(), "pageids": str(page_id),
                 "rvdir": "older", "rvstart": cutoff_utc, "rvlimit": "1",
                 "rvprop": "ids|timestamp|sha1|slotsha1"})


def content_url(revision_id: int) -> str:
    if revision_id <= 0:
        raise ValueError("MediaWiki revision ID must be positive")
    return _url({**_base_parameters(), "revids": str(revision_id),
                 "rvprop": "ids|timestamp|sha1|slotsha1|content|contentmodel"})


def parse_selection(raw: bytes, page_id: int, cutoff_utc: str) -> dict:
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MediaWiki selection is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("MediaWiki selection returned an error")
    pages = (payload.get("query") or {}).get("pages")
    if not isinstance(pages, list) or len(pages) != 1:
        raise ValueError("MediaWiki selection must contain exactly one page")
    page = pages[0]
    if (not isinstance(page, dict) or type(page.get("pageid")) is not int
            or page["pageid"] != page_id or page.get("ns") != 0
            or "missing" in page or not isinstance(page.get("title"), str)):
        raise ValueError("MediaWiki selection returned a different or missing article")
    revisions = page.get("revisions")
    if not isinstance(revisions, list) or len(revisions) != 1:
        raise ValueError("MediaWiki selection must contain exactly one revision")
    revision = revisions[0]
    if (not isinstance(revision, dict) or type(revision.get("revid")) is not int
            or revision["revid"] <= 0 or not isinstance(revision.get("timestamp"), str)
            or canonical_utc(revision["timestamp"]) >= canonical_utc(cutoff_utc)):
        raise ValueError("MediaWiki selected revision is not strictly before cutoff")
    main = (revision.get("slots") or {}).get("main")
    if (not isinstance(revision.get("sha1"), str) or not revision["sha1"]
            or not isinstance(main, dict) or not isinstance(main.get("sha1"), str)
            or not main["sha1"]):
        raise ValueError("MediaWiki selection lacks publisher SHA-1 metadata")
    return {"page_id": page_id, "page_title": page["title"],
            "revision_id": revision["revid"],
            "revision_timestamp_utc": revision["timestamp"],
            "publisher_sha1": revision["sha1"],
            "publisher_main_slot_sha1": main["sha1"]}


def fetch_once(url: str, *, timeout: int = 20) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            fetched_at_utc = utc_now()
            if response.status != 200:
                raise RuntimeError(f"MediaWiki returned HTTP {response.status}")
            return body, fetched_at_utc
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After", "unspecified")
            raise RuntimeError(
                f"MediaWiki rate limited this request (HTTP 429; Retry-After: {retry_after}). Stop and resume later."
            ) from exc
        raise RuntimeError(f"MediaWiki returned HTTP {exc.code}; stop and inspect before retrying") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"MediaWiki request failed: {exc.reason}") from exc


def _write_new(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".revision-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link creation fails if destination exists; exact receipts are
        # never silently overwritten by a later API response.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _saved_response(stem: Path, url: str, page_id: int, cutoff: str,
                    role: str, kind: str) -> tuple[bytes, dict] | None:
    raw_path = Path(f"{stem}.response.json")
    receipt_path = Path(f"{stem}.receipt.json")
    if not raw_path.exists() and not receipt_path.exists():
        return None
    if (not raw_path.is_file() or not receipt_path.is_file()
            or raw_path.is_symlink() or receipt_path.is_symlink()):
        raise ValueError(f"Incomplete or unsafe existing receipt: {stem}")
    try:
        receipt = json.loads(receipt_path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid existing receipt: {stem}") from exc
    raw = raw_path.read_bytes()
    if (not isinstance(receipt, dict) or receipt.get("request_url") != url
            or receipt.get("response_path") != str(raw_path)
            or receipt.get("response_sha256") != hashlib.sha256(raw).hexdigest()
            or receipt.get("page_id") != page_id or receipt.get("cutoff_utc") != cutoff
            or receipt.get("role") != role or receipt.get("response_kind") != kind):
        raise ValueError(f"Existing receipt failed verification: {stem}")
    canonical_utc(receipt.get("fetched_at_utc"))
    return raw, receipt


def save_response(stem: Path, url: str, page_id: int, cutoff: str,
                  role: str, kind: str, *, fetch=fetch_once) -> tuple[bytes, dict]:
    saved = _saved_response(stem, url, page_id, cutoff, role, kind)
    if saved is not None:
        return saved
    raw, fetched_at_utc = fetch(url)
    canonical_utc(fetched_at_utc)
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MediaWiki returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(f"MediaWiki API rejected the request: {payload.get('error')}")
    raw_path = Path(f"{stem}.response.json")
    receipt_path = Path(f"{stem}.receipt.json")
    receipt = {"response_path": str(raw_path),
               "response_sha256": hashlib.sha256(raw).hexdigest(),
               "request_url": url, "fetched_at_utc": fetched_at_utc,
               "page_id": page_id, "cutoff_utc": cutoff,
               "role": role, "response_kind": kind}
    _write_new(raw_path, raw)
    _write_new(receipt_path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return raw, receipt


def _api_receipt(record: dict) -> ApiReceipt:
    return ApiReceipt(path=record["response_path"], sha256=record["response_sha256"],
                      request_url=record["request_url"],
                      fetched_at_utc=record["fetched_at_utc"])


def save_proof(out_dir: Path, role: str, page_id: int, cutoff_utc: str,
               *, fetch=fetch_once) -> dict:
    if role not in {"prefight", "result"}:
        raise ValueError("Selection role must be prefight or result")
    marker = cutoff_utc.replace(":", "").replace("-", "")
    selected_raw, selected_receipt = save_response(
        out_dir / f"{role}-{marker}.selection", selection_url(page_id, cutoff_utc),
        page_id, cutoff_utc, role, "selection", fetch=fetch,
    )
    selected = parse_selection(selected_raw, page_id, cutoff_utc)
    content_stem = out_dir / f"{role}-{marker}.content"
    if not Path(f"{content_stem}.response.json").exists():
        time.sleep(1.0)
    _, content_receipt = save_response(
        content_stem, content_url(selected["revision_id"]), page_id,
        cutoff_utc, role, "content", fetch=fetch,
    )
    proof = verify_historical_revision(
        _api_receipt(selected_receipt), _api_receipt(content_receipt),
        expected_page_id=page_id, cutoff_at_utc=cutoff_utc,
    )
    manifest = {
        "schema_version": 1, "expected_page_id": page_id, "role": role,
        "cutoff_at_utc": cutoff_utc,
        "selection": selected_receipt, "content": content_receipt,
        "revision_id": proof.revision_id,
        "revision_timestamp_utc": proof.revision_timestamp_utc,
        "publisher_sha1": proof.publisher_sha1,
        "wikitext_sha256": proof.wikitext_sha256,
        "page_title": proof.page_title, "source_url": proof.source_url,
        "license_name": proof.license_name, "license_url": proof.license_url,
    }
    manifest_path = out_dir / f"{role}-{marker}.manifest.json"
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if manifest_path.exists():
        if manifest_path.is_symlink() or manifest_path.read_bytes() != encoded:
            raise ValueError(f"Existing manifest differs from verified receipts: {manifest_path}")
    else:
        _write_new(manifest_path, encoded)
    return {"manifest_path": str(manifest_path), **manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-slug", required=True, help="Stable local slug, e.g. ufc331")
    parser.add_argument("--page-id", required=True, type=int)
    parser.add_argument("--prefight-cutoff-utc", required=True)
    parser.add_argument("--result-cutoff-utc", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/raw/historical-revisions"))
    args = parser.parse_args(argv)
    if not _SLUG.fullmatch(args.event_slug):
        parser.error("--event-slug must be lowercase letters, digits, or hyphens")
    try:
        pre = canonical_utc(args.prefight_cutoff_utc)
        post = canonical_utc(args.result_cutoff_utc)
        if pre >= post or post > datetime.now(timezone.utc):
            raise ValueError("Cutoffs must be increasing and must not be in the future")
        if args.page_id <= 0:
            raise ValueError("Page ID must be positive")
        out_dir = (args.output_root / args.event_slug).resolve()
        for index, (role, cutoff) in enumerate((
            ("prefight", args.prefight_cutoff_utc),
            ("result", args.result_cutoff_utc),
        )):
            if index:
                time.sleep(1.0)
            proof = save_proof(out_dir, role, args.page_id, cutoff)
            print(json.dumps({"role": role, "manifest_path": proof["manifest_path"],
                              "revision_id": proof["revision_id"],
                              "revision_timestamp_utc": proof["revision_timestamp_utc"],
                              "selection_response_path": proof["selection"]["response_path"],
                              "content_response_path": proof["content"]["response_path"]}))
    except (ValueError, RuntimeError) as exc:
        print(f"historical revision fetch stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
