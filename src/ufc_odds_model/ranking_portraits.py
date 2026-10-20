"""Authentic official UFC portraits for current ranked athlete identities.

Image URLs must be observed on that athlete's official page. Opponent pictures,
generated faces and filename/endpoint guesses are never portrait fallbacks.
Public manifests contain remote URLs and provenance, not redistributed images.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import struct
import subprocess
from urllib.parse import unquote, urljoin, urlsplit
import zlib

from .rankings import _athlete_url, _utc, validate_rankings

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[2] / "assets/data/rankings-portraits.json"
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json({k:v for k,v in value.items() if k != "snapshot_sha256"}).encode()).hexdigest()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _now():
    return _utc(datetime.now(timezone.utc))


def image_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.ufc.com", "ufc.com"}
            or not parsed.path.startswith("/images/") or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in {None, 443}):
        raise ValueError("Portrait URL is not an observed official UFC HTTPS image")
    return value


def ranked_athletes(rankings):
    """Deduplicate exact official athlete links across both sets and P4P lists."""
    validate_rankings(rankings); athletes = {}
    for ranking_set in rankings["ranking_sets"]:
        for division in ranking_set["divisions"]:
            rows = [*division["champions"], *division["contenders"], *([division["leader"]] if division["leader"] else [])]
            for row in rows:
                url = row.get("athlete_url")
                if not url:
                    raise ValueError("A ranked athlete lacks an exact official profile identity")
                _athlete_url(url)
                athlete = athletes.setdefault(url, {"name": row["name"], "athlete_url": url, "occurrences": []})
                if athlete["name"] != row["name"]:
                    raise ValueError("One official athlete link has conflicting published names")
                athlete["occurrences"].append({"ranking_set": ranking_set["id"], "division": division["id"],
                                               "rank": row.get("rank"), "status": row.get("status")})
    return athletes


class _PortraitPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.canonical = []; self.og_images = []; self.images = []
    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "link" and attributes.get("rel") == "canonical": self.canonical.append(attributes.get("href"))
        if tag == "meta" and attributes.get("property") == "og:image": self.og_images.append(attributes.get("content"))
        if tag == "img": self.images.append(attributes)


def extract_athlete_portraits(html, *, athlete_url, response_url=None):
    """Return observed own headshots first, then the page's own hero portrait.

    Fight cards contain opponent images too. An unlabelled event headshot is
    accepted only when its actual filename matches this page's og:image. The
    profile's hero-image role is an independent explicit own-athlete fallback.
    """
    athlete_url = _athlete_url(athlete_url); response_url = _athlete_url(response_url or athlete_url)
    raw = html.encode() if isinstance(html, str) else bytes(html)
    if not raw or len(raw) > MAX_PAGE_BYTES: raise ValueError("Athlete page is empty or too large")
    parser = _PortraitPage(); parser.feed(raw.decode("utf-8-sig")); parser.close()
    if len(set(parser.canonical)) != 1 or not parser.canonical:
        raise ValueError("Athlete page has no unambiguous canonical identity")
    canonical = _athlete_url(parser.canonical[0])
    identity = lambda url: urlsplit(url).path.rstrip("/")
    if identity(canonical) != identity(response_url):
        raise ValueError("Athlete page canonical identity differs from the observed response")
    own_names = {unquote(urlsplit(url).path.rsplit("/", 1)[-1]) for url in parser.og_images if url}
    candidates = []
    for attributes in parser.images:
        source = attributes.get("src")
        if not source: continue
        source = urljoin(response_url, source)
        try: image_url(source)
        except ValueError: continue
        filename = unquote(urlsplit(source).path.rsplit("/", 1)[-1])
        if re.search(r"(?:placeholder|no[_-]?image|default[_-]?athlete|silhouette)", filename, re.I): continue
        classes = attributes.get("class", "").split()
        if ("image-style-event-results-athlete-headshot" in classes or "/event_results_athlete_headshot/" in source) and filename in own_names:
            candidates.append({"image_url": source, "image_kind": "headshot", "identity_basis": "official_page_og_image_filename_matches_observed_headshot"})
        elif "hero-profile__image" in classes:
            candidates.append({"image_url": source, "image_kind": "profile_full_body", "identity_basis": "official_athlete_page_own_hero_image"})
    candidates.sort(key=lambda row: 0 if row["image_kind"] == "headshot" else 1)
    unique = []; seen = set()
    for row in candidates:
        if row["image_url"] not in seen: unique.append(row); seen.add(row["image_url"])
    return {"canonical_athlete_url": canonical, "candidates": unique, "page_sha256": _sha(raw), "page_bytes": len(raw)}


def inspect_image(raw, content_type=None):
    """Validate PNG chunks/CRCs or a bounded JPEG frame and its dimensions."""
    raw = bytes(raw)
    if not raw or len(raw) > MAX_IMAGE_BYTES: raise ValueError("Portrait image is empty or too large")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        offset = 8; width = height = None; idat = False; ended = False
        while offset + 12 <= len(raw):
            size = struct.unpack(">I", raw[offset:offset+4])[0]; kind = raw[offset+4:offset+8]; end = offset + 12 + size
            if end > len(raw): raise ValueError("Truncated portrait PNG")
            data = raw[offset+8:offset+8+size]; crc = struct.unpack(">I", raw[offset+8+size:end])[0]
            if zlib.crc32(kind + data) & 0xffffffff != crc: raise ValueError("Portrait PNG checksum differs")
            if offset == 8:
                if kind != b"IHDR" or size != 13: raise ValueError("Portrait PNG has no valid header")
                width, height = struct.unpack(">II", data[:8])
            elif kind == b"IHDR": raise ValueError("Portrait PNG repeats its header")
            if kind == b"IDAT": idat = True
            offset = end
            if kind == b"IEND": ended = size == 0; break
        if not ended or not idat or offset != len(raw): raise ValueError("Portrait PNG is incomplete")
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9"):
        width = height = None; offset = 2
        while offset + 4 <= len(raw):
            if raw[offset] != 255: raise ValueError("Invalid portrait JPEG marker")
            while offset < len(raw) and raw[offset] == 255: offset += 1
            marker = raw[offset]; offset += 1
            if marker in {0xd8, 0xd9} or 0xd0 <= marker <= 0xd7: continue
            size = int.from_bytes(raw[offset:offset+2], "big")
            if size < 2 or offset + size > len(raw): raise ValueError("Truncated portrait JPEG")
            if marker in {0xc0, 0xc1, 0xc2}:
                height, width = struct.unpack(">HH", raw[offset+3:offset+7]); break
            if marker == 0xda: break
            offset += size
        if width is None: raise ValueError("Portrait JPEG has no supported frame")
        mime = "image/jpeg"
    else: raise ValueError("Response is not a supported portrait image")
    if not 16 <= width <= 8192 or not 16 <= height <= 8192: raise ValueError("Portrait dimensions are invalid")
    if content_type and content_type.split(";", 1)[0].strip().lower() != mime:
        raise ValueError("Portrait response type and actual bytes disagree")
    return {"image_content_type": mime, "image_width": width, "image_height": height,
            "image_sha256": _sha(raw), "image_bytes": len(raw)}


def _download(url, destination, *, kind, timeout):
    validate_url = _athlete_url if kind == "page" else image_url; validate_url(url)
    headers = destination.with_suffix(destination.suffix + ".headers.txt")
    result = subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https",
        "--proto-redir", "=https", "--max-time", str(timeout), "--max-filesize", str(MAX_PAGE_BYTES if kind == "page" else MAX_IMAGE_BYTES),
        "--dump-header", str(headers), "--output", str(destination), "--write-out", "%{http_code}\n%{url_effective}\n%{content_type}", url],
        capture_output=True, text=True, timeout=timeout + 5, check=False)
    values = result.stdout.strip().splitlines()
    if result.returncode or len(values) != 3 or values[0] != "200": raise ValueError("Official portrait source did not return HTTP 200")
    validate_url(values[1])
    return {"requested_url": url, "response_url": values[1], "http_status": 200, "content_type": values[2],
            "fetched_at_utc": _now(), "headers_sha256": _sha(headers.read_bytes()), "access_mode": "direct_public_https_curl"}


def capture_athlete(athlete, directory, *, timeout=30, downloader=None):
    """Capture one exact official identity, retaining explicit unsuccessful gaps."""
    downloader = downloader or _download; directory = Path(directory); directory.mkdir(parents=True, exist_ok=False)
    result = {"name": athlete["name"], "athlete_url": athlete["athlete_url"], "source_page": athlete["athlete_url"],
              "credited_owner": "UFC", "occurrences": athlete["occurrences"], "status": "unavailable", "image_url": None}
    try:
        page = directory / "page.html"; page_receipt = downloader(athlete["athlete_url"], page, kind="page", timeout=timeout)
        parsed = extract_athlete_portraits(page.read_bytes(), athlete_url=athlete["athlete_url"], response_url=page_receipt["response_url"])
        result.update(canonical_athlete_url=parsed["canonical_athlete_url"], page_sha256=parsed["page_sha256"], page_bytes=parsed["page_bytes"], page_receipt=page_receipt)
        if not parsed["candidates"]: raise ValueError("Official athlete page publishes no identifiable portrait")
        attempts = []
        for number, candidate in enumerate(parsed["candidates"]):
            try:
                image = directory / f"portrait-{number}.image"; receipt = downloader(candidate["image_url"], image, kind="image", timeout=timeout)
                checked = inspect_image(image.read_bytes(), receipt["content_type"])
                result.update(candidate, **checked, image_receipt=receipt, fetched_at_utc=receipt["fetched_at_utc"], status="available", gap_reason=None)
                break
            except (OSError, ValueError, subprocess.SubprocessError): attempts.append({"image_url": candidate["image_url"], "status": "unavailable"})
        if result["status"] != "available": raise ValueError("Observed official portrait candidates were unavailable or invalid")
        if attempts: result["unsuccessful_image_candidates"] = attempts
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["gap_reason"] = str(exc) if isinstance(exc, ValueError) else "Official portrait capture failed"
        result["fetched_at_utc"] = _now()
    (directory / "receipt.json").write_text(_json(result) + "\n", encoding="utf-8")
    return result


def build_snapshot(rankings, portraits, *, fetched_at_utc=None, capture_id=None):
    athletes = ranked_athletes(rankings)
    if set(portraits) != set(athletes): raise ValueError("Portrait coverage does not match all ranked athlete identities")
    available = sum(row["status"] == "available" for row in portraits.values())
    document = {"schema_version": 1, "artifact_type": "official_ufc_rankings_portraits", "fetched_at_utc": _utc(fetched_at_utc or _now()),
                "rankings_snapshot_sha256": rankings["snapshot_sha256"], "rankings_source_sha256": rankings["source"]["raw_sha256"],
                "attribution": "Athlete portraits © UFC. Source: official UFC athlete pages.", "images_bundled": False,
                "portraits": portraits, "coverage": {"ranked_unique_athletes": len(athletes), "available": available,
                    "unavailable": len(athletes)-available, "headshots": sum(p.get("image_kind") == "headshot" for p in portraits.values())},
                "historical_model_feature_eligible": False,
                "limitations": ["Authentic remotely hosted official portraits; availability can change after capture.",
                                "Current portrait captures are display evidence and are not historical model inputs."]}
    if capture_id is not None: document["raw_capture_id"] = capture_id
    document["snapshot_sha256"] = _digest(document)
    return validate_portraits(document, rankings=rankings)


def validate_portraits(document, *, rankings=None):
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or document.get("artifact_type") != "official_ufc_rankings_portraits" or document.get("historical_model_feature_eligible") is not False
            or document.get("images_bundled") is not False or document.get("snapshot_sha256") != _digest(document)):
        raise ValueError("Unsupported or changed official portrait manifest")
    captured = _utc(document["fetched_at_utc"])
    if captured != document["fetched_at_utc"]: raise ValueError("Portrait capture timestamp is not normalized UTC")
    for field in ("rankings_source_sha256", "rankings_snapshot_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", document[field]): raise ValueError("Portrait roster provenance is incomplete")
    portraits = document["portraits"]
    if not isinstance(portraits, dict) or not portraits: raise ValueError("Portrait manifest is empty")
    for key, row in portraits.items():
        _athlete_url(key)
        if _utc(row["fetched_at_utc"]) > captured: raise ValueError("Portrait evidence was captured after the manifest timestamp")
        if row["athlete_url"] != key or row["source_page"] != key or row.get("credited_owner") != "UFC" or not row.get("name"):
            raise ValueError("Portrait official identity or attribution changed")
        if row["status"] == "available":
            image_url(row["image_url"])
            if (row.get("image_kind") not in {"headshot", "profile_full_body"} or row.get("image_content_type") not in {"image/png", "image/jpeg"}
                    or not re.fullmatch(r"[0-9a-f]{64}", row.get("image_sha256", ""))
                    or not re.fullmatch(r"[0-9a-f]{64}", row.get("page_sha256", ""))
                    or any(type(row.get(field)) is not int or not 16 <= row[field] <= 8192 for field in ("image_width", "image_height"))
                    or type(row.get("image_bytes")) is not int or row["image_bytes"] <= 0):
                raise ValueError("Available portrait lacks actual validated source/image evidence")
        elif row["status"] != "unavailable" or row.get("image_url") is not None or not row.get("gap_reason"):
            raise ValueError("Portrait gap must remain explicit")
    available = sum(row["status"] == "available" for row in portraits.values())
    expected = {"ranked_unique_athletes": len(portraits), "available": available, "unavailable": len(portraits)-available,
                "headshots": sum(p.get("image_kind") == "headshot" for p in portraits.values())}
    if document["coverage"] != expected: raise ValueError("Portrait coverage changed")
    if rankings is not None:
        athletes = ranked_athletes(rankings)
        # Re-fetching identical raw rankings changes its capture-specific JSON
        # digest. The actual raw source and exact roster/occurrences must agree.
        if (document["rankings_source_sha256"] != rankings["source"]["raw_sha256"]
                or set(portraits) != set(athletes) or any(portraits[url]["name"] != athlete["name"] or portraits[url].get("occurrences") != athlete["occurrences"] for url, athlete in athletes.items())):
            raise ValueError("Portrait source map belongs to a different rankings snapshot")
    return document


def load_portraits(path=DEFAULT_SNAPSHOT, *, rankings=None, as_of=None):
    document = validate_portraits(json.loads(Path(path).read_text()), rankings=rankings)
    if as_of is not None and document["fetched_at_utc"] > _utc(as_of): raise ValueError("Portrait capture is later than the requested observation time")
    return document


def fetch_portraits(rankings, *, raw_directory, workers=3, timeout=30, downloader=None):
    """Fetch exact ranked identities with bounded concurrency and retained receipts."""
    if type(workers) is not int or not 1 <= workers <= 4: raise ValueError("Portrait workers must be from one to four")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120: raise ValueError("Invalid portrait timeout")
    athletes = ranked_athletes(rankings); root = Path(raw_directory); root.mkdir(parents=True, exist_ok=True)
    capture_id = _now().replace("-", "").replace(":", "") + "-" + rankings["snapshot_sha256"][:12]
    capture = root / capture_id; capture.mkdir(exist_ok=False)
    def one(athlete):
        slug = urlsplit(athlete["athlete_url"]).path.rsplit("/", 1)[-1]
        return athlete["athlete_url"], capture_athlete(athlete, capture / slug, timeout=timeout, downloader=downloader)
    with ThreadPoolExecutor(max_workers=workers) as pool: portraits = dict(pool.map(one, athletes.values()))
    document = build_snapshot(rankings, portraits, capture_id=capture_id)
    (capture / "manifest.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document
