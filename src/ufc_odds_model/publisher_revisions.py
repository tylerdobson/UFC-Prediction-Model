"""Offline verification of retrospective MediaWiki revision evidence.

The two Action API responses must have been retained exactly as fetched. The
selection response asks for the newest revision at a historical cutoff; the
content response asks for that revision by ID. This proves internal consistency
of retained publisher metadata and text, not when our application downloaded
it. ``fetched_at_utc`` therefore remains the actual later capture time.

A receipt and its digest are not a publisher signature. The acquisition path
must retain the real request URL/response bytes and review source provenance.
Never use this proof as a contemporaneous live-card observation or odds quote.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


ACTION_API = "https://en.wikipedia.org/w/api.php"
LICENSE_NAME = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/deed.en"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class ApiReceipt:
    """Acquisition metadata paired with an exact saved JSON response."""

    path: str | Path
    sha256: str
    request_url: str
    fetched_at_utc: str


@dataclass(frozen=True)
class RevisionProof:
    page_id: int
    page_title: str
    revision_id: int
    revision_timestamp_utc: str
    cutoff_at_utc: str
    publisher_sha1: str
    wikitext_sha256: str
    wikitext: str
    selection_receipt: ApiReceipt
    content_receipt: ApiReceipt
    source_url: str
    license_name: str = LICENSE_NAME
    license_url: str = LICENSE_URL


def _utc(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ValueError(f"{label} must be a canonical UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} must be a UTC timestamp")
    return parsed


def _read_response(receipt: ApiReceipt, label: str) -> dict:
    if not isinstance(receipt.sha256, str) or not _SHA256.fullmatch(receipt.sha256):
        raise ValueError(f"{label} receipt needs a SHA-256 digest")
    path = Path(receipt.path).expanduser()
    # Reject a linked leaf, including a link swapped in between checks.
    if path.is_symlink():
        raise ValueError(f"{label} receipt cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} receipt cannot be opened as a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{label} receipt must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(body).hexdigest() != receipt.sha256:
        raise ValueError(f"{label} receipt SHA-256 mismatch")
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} receipt is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(f"{label} receipt has no successful JSON response")
    return payload


def _request_params(url: str, label: str) -> dict[str, str]:
    if not isinstance(url, str):
        raise ValueError(f"{label} request URL is missing")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "en.wikipedia.org"
            or parsed.netloc != "en.wikipedia.org" or parsed.path != "/w/api.php"
            or parsed.fragment):
        raise ValueError(f"{label} must use the English Wikipedia Action API")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    params = dict(pairs)
    if len(params) != len(pairs):
        raise ValueError(f"{label} has duplicate request parameters")
    for key, value in (('action', 'query'), ('prop', 'revisions'),
                       ('format', 'json'), ('formatversion', '2'),
                       ('rvslots', 'main')):
        if params.get(key) != value:
            raise ValueError(f"{label} has an unexpected {key} parameter")
    if 'maxlag' in params and (not params['maxlag'].isdigit()
                               or int(params['maxlag']) <= 0):
        raise ValueError(f"{label} has an invalid maxlag parameter")
    return params


def _validate_selection_request(receipt: ApiReceipt, page_id: int, cutoff: str) -> None:
    params = _request_params(receipt.request_url, 'Selection')
    allowed = {'action', 'prop', 'format', 'formatversion', 'rvslots',
               'pageids', 'rvdir', 'rvstart', 'rvlimit', 'rvprop', 'maxlag'}
    if set(params) - allowed or not {'pageids', 'rvdir', 'rvstart', 'rvlimit', 'rvprop'} <= set(params):
        raise ValueError('Selection request has unexpected or missing parameters')
    if (params['pageids'] != str(page_id) or params['rvdir'] != 'older'
            or params['rvstart'] != cutoff or params['rvlimit'] != '1'
            or set(params['rvprop'].split('|')) != {'ids', 'timestamp', 'sha1', 'slotsha1'}):
        raise ValueError('Selection request must select the latest revision at the exact cutoff')


def _validate_content_request(receipt: ApiReceipt, revision_id: int) -> None:
    params = _request_params(receipt.request_url, 'Content')
    allowed = {'action', 'prop', 'format', 'formatversion', 'rvslots',
               'revids', 'rvprop', 'maxlag'}
    if set(params) - allowed or not {'revids', 'rvprop'} <= set(params):
        raise ValueError('Content request has unexpected or missing parameters')
    if (params['revids'] != str(revision_id)
            or set(params['rvprop'].split('|')) !=
            {'ids', 'timestamp', 'sha1', 'slotsha1', 'content', 'contentmodel'}):
        raise ValueError('Content request must fetch the selected revision by ID')


def _one_revision(payload: dict, label: str, expected_page_id: int) -> tuple[dict, dict]:
    query = payload.get('query')
    pages = query.get('pages') if isinstance(query, dict) else None
    if not isinstance(pages, list) or len(pages) != 1:
        raise ValueError(f'{label} needs exactly one page')
    page = pages[0]
    if (not isinstance(page, dict) or type(page.get('pageid')) is not int
            or page['pageid'] != expected_page_id or page.get('ns') != 0
            or not isinstance(page.get('title'), str) or not page['title'].strip()
            or 'missing' in page or 'invalid' in page):
        raise ValueError(f'{label} page ID/title does not match the requested article')
    revisions = page.get('revisions')
    if not isinstance(revisions, list) or len(revisions) != 1:
        raise ValueError(f'{label} needs exactly one accessible revision')
    revision = revisions[0]
    if (not isinstance(revision, dict) or type(revision.get('revid')) is not int
            or revision['revid'] <= 0 or 'texthidden' in revision
            or 'sha1hidden' in revision or 'suppressed' in revision):
        raise ValueError(f'{label} revision is missing or hidden')
    _utc(revision.get('timestamp'), f'{label} revision timestamp')
    if not isinstance(revision.get('sha1'), str) or not revision['sha1']:
        raise ValueError(f'{label} revision has no publisher SHA-1')
    slots = revision.get('slots')
    if not isinstance(slots, dict) or set(slots) != {'main'}:
        raise ValueError(f'{label} must contain exactly one main content slot')
    main = slots['main']
    if not isinstance(main, dict) or 'sha1hidden' in main:
        raise ValueError(f'{label} main slot is hidden')
    if not isinstance(main.get('sha1'), str) or not main['sha1']:
        raise ValueError(f'{label} main slot has no publisher SHA-1')
    return page, revision


def _base36_sha1(hex_digest: str) -> str:
    value = int(hex_digest, 16)
    digits = ''
    while value:
        value, remainder = divmod(value, 36)
        digits = _BASE36[remainder] + digits
    return digits.zfill(31)


def _matches_sha1(source: str, stated: str) -> bool:
    digest = hashlib.sha1(source.encode('utf-8')).hexdigest()
    return stated.lower() in {digest, _base36_sha1(digest)}


def verify_historical_revision(
    selection: ApiReceipt,
    content: ApiReceipt,
    *,
    expected_page_id: int,
    cutoff_at_utc: str,
) -> RevisionProof:
    """Verify a saved latest-at-cutoff query and its exact revision content.

    Both receipts may have been fetched long after ``cutoff_at_utc``. The
    publication timestamp, not the download timestamp, supports retrospective
    availability. Acquisition authenticity still depends on the retained
    response actually coming from the recorded publisher request.
    """
    if type(expected_page_id) is not int or expected_page_id <= 0:
        raise ValueError('Expected page ID must be a positive integer')
    cutoff = _utc(cutoff_at_utc, 'Decision cutoff')
    selected_at = _utc(selection.fetched_at_utc, 'Selection fetch')
    content_at = _utc(content.fetched_at_utc, 'Content fetch')
    if content_at < selected_at:
        raise ValueError('Content fetch predates the selection fetch')
    _validate_selection_request(selection, expected_page_id, cutoff_at_utc)
    selected_page, selected_rev = _one_revision(
        _read_response(selection, 'Selection'), 'Selection', expected_page_id,
    )
    revised = _utc(selected_rev['timestamp'], 'Selected revision timestamp')
    if revised >= cutoff:
        raise ValueError('Selected revision was not published before the decision cutoff')
    if revised > selected_at:
        raise ValueError('Selection fetch predates publisher revision')
    _validate_content_request(content, selected_rev['revid'])
    content_page, content_rev = _one_revision(
        _read_response(content, 'Content'), 'Content', expected_page_id,
    )
    if (content_page['title'] != selected_page['title']
            or content_rev['revid'] != selected_rev['revid']
            or content_rev['timestamp'] != selected_rev['timestamp']
            or content_rev['sha1'] != selected_rev['sha1']
            or content_rev['slots']['main']['sha1'] != selected_rev['slots']['main']['sha1']):
        raise ValueError('Content revision differs from selected publisher revision')
    if revised > content_at:
        raise ValueError('Content fetch predates publisher revision')
    slot = content_rev['slots']['main']
    if slot.get('contentmodel') != 'wikitext' or not isinstance(slot.get('content'), str):
        raise ValueError('Selected revision has no main-slot wikitext')
    source = slot['content']
    if not (_matches_sha1(source, slot['sha1'])
            and _matches_sha1(source, content_rev['sha1'])):
        raise ValueError('Publisher SHA-1 does not match selected wikitext')
    return RevisionProof(
        page_id=expected_page_id,
        page_title=selected_page['title'],
        revision_id=selected_rev['revid'],
        revision_timestamp_utc=selected_rev['timestamp'],
        cutoff_at_utc=cutoff_at_utc,
        publisher_sha1=content_rev['sha1'],
        wikitext_sha256=hashlib.sha256(source.encode('utf-8')).hexdigest(),
        wikitext=source,
        selection_receipt=selection,
        content_receipt=content,
        source_url=f'https://en.wikipedia.org/w/index.php?oldid={selected_rev["revid"]}',
    )
