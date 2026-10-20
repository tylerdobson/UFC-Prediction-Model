"""Read a published research forecast without touching operating evidence.

``load_saved_forecast()`` defaults to the checked-in V6 final report. A custom
report must be inside ``project_root`` and have a sibling release-manifest.json
with schema_version=1, matching forecast metadata and an ``outputs`` mapping of
project-relative POSIX paths to SHA-256 digests. JSON and sibling HTML outputs
are required; every declared output is verified. No training database is read.

Available results contain the public ``report`` schema, a research-only label,
event_status (upcoming/event_passed), verified json/html/csv/pdf artifact metadata
and explicit false operating/promotion eligibility. Artifact paths are relative
to project_root. Callers may use them for downloads; this loader never executes
the saved HTML. Market freshness, if present in the frozen report, describes its
forecast cutoff only. Unavailable results contain no report, artifact paths or
exception text. Release hashes establish retained-byte integrity, not source
independence, human review, operating approval or predictive validity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .prediction_report import validate_report


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = Path("output/ufc333-20261019-v6-final/prediction-report.json")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPORT_BYTES = 5_000_000
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_ARTIFACT_BYTES = 25_000_000


def _utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must have a timezone")
    return result.astimezone(timezone.utc)


def _public_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON number")


def _json(content: bytes) -> dict[str, Any]:
    result = json.loads(content, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("JSON document must be an object")
    return result


def _safe_file(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or any(ord(character) < 32 for character in relative)):
        raise ValueError("Unsafe artifact path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in {".", ".."} for part in parts.parts) or parts.as_posix() != relative:
        raise ValueError("Unsafe artifact path")
    candidate = root
    for part in parts.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Symlinked artifact")
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        raise ValueError("Artifact missing or outside project")
    return candidate


def _read(path: Path, limit: int) -> bytes:
    if path.stat().st_size > limit:
        raise ValueError("Artifact exceeds bounded size")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Artifact exceeds bounded size")
    return content


def _unavailable() -> dict[str, Any]:
    return {"status": "unavailable", "label": "Saved research forecast unavailable",
            "reason": "The saved report, release hashes or forecast chronology could not be verified.",
            "research_only": True, "operating_eligible": False, "promotion_eligible": False,
            "report": None, "artifacts": {}}


def load_saved_forecast(report_path: str | Path | None = None, *,
                        project_root: str | Path | None = None,
                        as_of: datetime | None = None) -> dict[str, Any]:
    """Verify and load frozen public values; never mutate a file or fit a model."""
    try:
        root = Path(project_root).expanduser().resolve() if project_root is not None else PROJECT_ROOT
        now = as_of if as_of is not None else datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        now = now.astimezone(timezone.utc)
        supplied = Path(report_path).expanduser() if report_path is not None else DEFAULT_REPORT
        if supplied.is_absolute():
            relative = supplied.relative_to(root).as_posix()
        else:
            relative = supplied.as_posix()
        report_file = _safe_file(root, relative)
        report_bytes = _read(report_file, _MAX_REPORT_BYTES)
        manifest_relative = (PurePosixPath(relative).parent / "release-manifest.json").as_posix()
        manifest = _json(_read(_safe_file(root, manifest_relative), _MAX_MANIFEST_BYTES))
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise ValueError("Unsupported release schema")
        outputs = manifest.get("outputs")
        html_relative = PurePosixPath(relative).with_suffix(".html").as_posix()
        if not isinstance(outputs, dict) or not 2 <= len(outputs) <= 50 or relative not in outputs or html_relative not in outputs:
            raise ValueError("Release must verify report JSON and sibling HTML")
        verified = {}
        for artifact_path, expected in outputs.items():
            if not isinstance(expected, str) or not _HASH.fullmatch(expected):
                raise ValueError("Invalid release hash")
            path = _safe_file(root, artifact_path)
            content = report_bytes if artifact_path == relative else _read(path, _MAX_ARTIFACT_BYTES)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("Release artifact bytes changed")
            verified[artifact_path] = {"path": artifact_path, "sha256": expected, "bytes": len(content)}
        report = validate_report(_json(report_bytes))
        forecast = report["forecast"]
        published = manifest.get("forecast")
        if not isinstance(published, dict) or published.get("research_only") is not True:
            raise ValueError("Release must identify research-only forecasts")
        for field in ("model_version", "status"):
            if published.get(field) != forecast[field]:
                raise ValueError("Release forecast metadata differs")
        if _utc(published.get("cutoff_at_utc")) != _utc(forecast["cutoff_at_utc"]):
            raise ValueError("Release forecast cutoff differs")
        for field in ("model_label", "forecast_id"):
            if field in published and published[field] != forecast.get(field):
                raise ValueError("Release forecast identity differs")
        for field, observed in (("scored_matchups", len(report["bouts"])),
                                ("unscored_matchups", len(report["unscored_bouts"]))):
            if field in manifest and (type(manifest[field]) is not int or manifest[field] != observed):
                raise ValueError("Release coverage differs")
        cutoff = _utc(forecast["cutoff_at_utc"])
        generated = _utc(report["generated_at_utc"])
        if cutoff > now or generated > now:
            raise ValueError("Saved forecast evidence is in the future")
        event = report["event"]
        event_date = date.fromisoformat(event["event_date"])
        event_start = (_utc(event["start_time_utc"]) if event.get("start_time_utc") else
                       datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc))
        if cutoff >= event_start:
            raise ValueError("Forecast cutoff does not precede event")
        pairs = [frozenset((bout["fighter_a"].casefold(), bout["fighter_b"].casefold())) for bout in report["bouts"]]
        if any(len(pair) != 2 for pair in pairs) or len(set(pairs)) != len(pairs):
            raise ValueError("Ambiguous matchup names")
        event_passed = now >= event_start
        artifacts = {"json": verified[relative], "html": verified[html_relative]}
        csv_relative = PurePosixPath(relative).with_suffix(".csv").as_posix()
        if csv_relative in verified:
            artifacts["csv"] = verified[csv_relative]
        pdfs = [value for key, value in verified.items() if key.lower().endswith(".pdf")]
        if len(pdfs) == 1:
            artifacts["pdf"] = pdfs[0]
        return {"status": "available", "reason": None, "label": "Saved research forecast" +
                (" · event date passed" if event_passed else ""),
                "event_status": "event_passed" if event_passed else "upcoming",
                "as_of_utc": _public_time(now), "research_only": True,
                "operating_eligible": False, "promotion_eligible": False,
                "market_freshness_basis": "forecast_cutoff_only", "report": report, "artifacts": artifacts}
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return _unavailable()
