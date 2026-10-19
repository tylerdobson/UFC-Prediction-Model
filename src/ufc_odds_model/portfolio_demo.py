"""Run the portable engineering demo from checked-in SYNTHETIC fixtures.

The default path uses only the standard library and never requests a source,
loads credentials, fits a winner model, or requires a private database. It
exercises the production normalizer, quality checks and earlier-fight report.
MySQL publication is a separate explicit opt-in after local verification.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .card_statistics import build_card_report, render_csv, render_html, render_json
from .historical_stats import FILES, HistoryStore, build_history, export_coverage


PROTOCOL = "synthetic-offline-portfolio-demo-v1"
NOTICE = ("SYNTHETIC ENGINEERING DEMO. All fighters, events, outcomes and counts are invented. "
          "These artifacts demonstrate data processing, not real UFC observations or predictions.")
DEFAULT_OUTPUT = Path("output/pipeline-demo")
FIXTURE_DIRECTORY = Path(__file__).resolve().parents[2] / "demo/fixtures"
MYSQL_KEYS = {"UFC_MYSQL_HOST", "UFC_MYSQL_PORT", "UFC_MYSQL_USER", "UFC_MYSQL_PASSWORD", "UFC_MYSQL_DATABASE"}


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _fixtures(directory: Path) -> tuple[dict[str, Any], dict[str, str]]:
    path = directory / "fixture_manifest.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1 or fixture.get("synthetic") is not True:
        raise ValueError("The offline demo requires an explicitly synthetic fixture manifest")
    entries = {entry["file"]: entry for entry in fixture.get("files", [])}
    if set(entries) != set(FILES):
        raise ValueError("The synthetic fixture must contain exactly the four expected source CSVs")
    for name in FILES:
        source = directory / name
        if source.is_symlink() or not source.is_file() or _sha(source) != entries[name]["sha256"]:
            raise ValueError(f"Synthetic fixture bytes changed or are missing: {name}")
        if source.stat().st_size != entries[name]["bytes"]:
            raise ValueError(f"Synthetic fixture byte count changed: {name}")
    # Implementation fingerprints make replay boundaries explicit when code
    # changes; SQLite byte identity across different SQLite versions is not
    # claimed. Logical fixtures and quality expectations remain portable.
    import ufc_odds_model.historical_stats as normalizer
    import ufc_odds_model.card_statistics as card_report
    inputs = {"fixture_manifest_sha256": _sha(path), "normalizer_sha256": _sha(Path(normalizer.__file__)),
              "card_report_implementation_sha256": _sha(Path(card_report.__file__)),
              "demo_implementation_sha256": _sha(Path(__file__))}
    return fixture, inputs


def _quality(database: Path, expected: dict[str, int]) -> dict[str, Any]:
    with HistoryStore(database) as store:
        connection = store.connection
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        counts = {
            "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "bouts": connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0],
            "usable_bouts": connection.execute("SELECT COUNT(*) FROM bouts WHERE stats_status='usable'").fetchone()[0],
            "held_bouts": connection.execute("SELECT COUNT(*) FROM bouts WHERE stats_status='held'").fetchone()[0],
            "source_round_rows": connection.execute("SELECT COUNT(*) FROM source_round_rows").fetchone()[0],
            "missing_reach_profiles": connection.execute("SELECT COUNT(*) FROM fighters WHERE reach_cm IS NULL").fetchone()[0],
            "missing_control_fighter_bouts": connection.execute("SELECT COUNT(*) FROM fighter_bout_facts WHERE json_extract(totals_json,'$.control_seconds') IS NULL").fetchone()[0],
            "unknown_duration_usable_bouts": connection.execute("SELECT COUNT(*) FROM bouts WHERE stats_status='usable' AND elapsed_seconds IS NULL").fetchone()[0],
        }
        held = [dict(row) for row in connection.execute("SELECT bout_id,field,reason FROM gaps ORDER BY gap_id")]
    if integrity != "ok" or foreign_keys:
        raise ValueError("Synthetic demo failed SQLite or foreign-key integrity checks")
    if counts != expected:
        raise ValueError("Synthetic fixture quality outcomes changed; review the parser before creating a new demo edition")
    return {"schema_version": 1, "synthetic": True, "notice": NOTICE, "status": "passed_with_expected_gaps",
            "sqlite_integrity": integrity, "foreign_key_violations": len(foreign_keys),
            "fixture_expectations_match": True, "counts": counts, "gaps": held,
            "checks": [
                {"check": "Raw source hashes", "status": "pass", "result": "All four CSV byte hashes match the checked-in manifest."},
                {"check": "Relational integrity", "status": "pass", "result": "SQLite integrity and foreign keys pass."},
                {"check": "Paired-round completeness", "status": "held", "result": "One incomplete paired-round bout is excluded from normalized fighter facts; raw rows remain retained."},
                {"check": "Reach availability", "status": "warning", "result": "One absent measurement remains NULL."},
                {"check": "Control availability", "status": "warning", "result": "One missing control observation remains NULL; valid strike counts survive."},
                {"check": "Duration availability", "status": "warning", "result": "One unknown schedule retains counts without inventing a rate denominator."},
            ]}


def _coverage_csv(coverage: dict[str, Any]) -> str:
    columns = ["synthetic", "event_id", "name", "event_date", "bouts", "paired_stat_bouts", "duration_stat_bouts", "held_bouts", "both_reach_bouts"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for event in coverage["events"]:
        writer.writerow({"synthetic": True, **{key: event.get(key) for key in columns if key != "synthetic"}})
    return output.getvalue()


def _sample(database: Path, fixture: dict[str, Any]) -> dict[str, Any]:
    report = build_card_report(database, event_id=fixture["sample_event_id"], as_of=fixture["sample_as_of_utc"])
    report["synthetic"], report["notice"] = True, NOTICE
    report["generated_at_utc"] = fixture["captured_at_utc"]
    report["timestamp_basis"] = "fixed_logical_fixture_timestamp_not_wall_clock_execution"
    report["limitations"].insert(0, NOTICE)
    # Placeholder URLs satisfy the real source parser's ID grammar. They are
    # never presented as real observations or clickable source evidence.
    report["event"]["source_url"] = None
    report["source_context"]["repository_url"] = None
    for bout in report["bouts"]:
        for side in ("fighter_a", "fighter_b"):
            bout[side]["reach_source_url"] = None
            for fight in bout[side]["prior_fights"]:
                fight["source_url"] = None
    return report


def _landing(quality: dict[str, Any], coverage: dict[str, Any], run_id: str) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    counts = quality["counts"]
    checks = "".join(f"<tr><th scope='row'>{esc(row['check'])}</th><td>{esc(row['status'])}</td><td>{esc(row['result'])}</td></tr>" for row in quality["checks"])
    events = "".join(f"<tr><th scope='row'>{esc(row['name'])}</th><td>{row['bouts']}</td><td>{row['paired_stat_bouts']}</td><td>{row['held_bouts']}</td></tr>" for row in coverage["events"])
    style = """*{box-sizing:border-box}body{margin:0;background:#edf1ee;color:#17302c;font:15px/1.6 -apple-system,BlinkMacSystemFont,Arial,sans-serif}main{max-width:1000px;margin:30px auto;background:white;padding:38px 44px}header{border-bottom:3px solid #17302c;padding-bottom:22px}.eyebrow{color:#146d60;text-transform:uppercase;font-size:11px;letter-spacing:.12em;font-weight:750}h1{font-size:38px;letter-spacing:-1px;line-height:1.12;margin:12px 0}h2{font:700 25px Georgia,serif;margin-top:30px}.notice{padding:12px 16px;border-left:4px solid #a15b27;background:#fff5e9;font-size:12px}.pipeline{padding:14px 0;margin-top:20px;color:#146d60;font-weight:650;border-bottom:1px solid #dce4df}.metrics{display:flex;gap:35px;padding:18px 0;border-bottom:1px solid #dce4df}.metrics strong{font-size:30px;display:block}.metrics span{font-size:11px;color:#586d65}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:9px 7px;text-align:left;border-bottom:1px solid #dce4df;vertical-align:top}td:nth-child(2){color:#146d60}p,li{color:#52675f}a{color:#146d60}.links{padding-left:19px}.mono{font:11px/1.6 monospace;overflow-wrap:anywhere}.small{font-size:11px}.table-scroll{overflow-x:auto}footer{margin-top:30px;border-top:2px solid #17302c;padding-top:10px;font-size:11px}@media(max-width:700px){main{margin:0;padding:24px 18px}h1{font-size:29px}.metrics{gap:22px}.metrics strong{font-size:24px}.pipeline{font-size:13px}table{font-size:11px}th,td{padding:7px 4px}}"""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Synthetic data engineering demo</title><style>{style}</style></head><body><main>"
            "<header><p class='eyebrow'>UFC Forecast / Engineering portfolio</p><h1>A small pipeline you can inspect.</h1>"
            "<p>Immutable raw bytes, a normalized relational store, explicit data-quality gaps and reproducible analytics. Built offline with the production parser.</p></header>"
            f"<p class='notice'><strong>{esc(NOTICE)}</strong></p><p class='pipeline'>CSV fixtures → hash-verified raw capture → SQLite dimensions &amp; facts → quality gates → earlier-fight analytics</p>"
            f"<section class='metrics'><div><strong>{counts['events']}</strong><span>synthetic cards</span></div><div><strong>{counts['usable_bouts']} / {counts['bouts']}</strong><span>usable paired-stat bouts</span></div><div><strong>{counts['held_bouts']}</strong><span>held bout; raw rows retained</span></div></section>"
            f"<h2>Quality is visible</h2><div class='table-scroll'><table><thead><tr><th>Check</th><th>Status</th><th>Observed behavior</th></tr></thead><tbody>{checks}</tbody></table></div>"
            f"<h2>Card coverage</h2><div class='table-scroll'><table><thead><tr><th>Synthetic card</th><th>Bouts</th><th>Usable</th><th>Held</th></tr></thead><tbody>{events}</tbody></table></div>"
            "<h2>Inspect the deliverables</h2><ul class='links'><li><a href='reports/card-statistics.html'>Earlier-fight sample analytics</a> — target-card statistics are excluded.</li>"
            "<li><a href='reports/quality.json'>Quality report</a> and <a href='reports/coverage.json'>coverage JSON</a> / <a href='reports/coverage.csv'>CSV</a>.</li>"
            "<li><a href='raw/manifest.json'>Raw capture manifest</a> with SHA-256 and source byte counts.</li>"
            "<li><a href='run-manifest.json'>Run manifest</a> with code/input fingerprints and artifact hashes.</li></ul>"
            "<p class='small'>The normalized SQLite database is <code>normalized/statistics.sqlite</code>. Optional MySQL publication is a separate, explicit action; this page makes no claim that a warehouse or scheduler is running.</p>"
            f"<footer><strong>SYNTHETIC DEMO</strong><br><span class='mono'>{esc(run_id)}</span><br>Fixed logical fixture timestamps · No source requests · No winner model fitted · No wagers</footer></main></body></html>")


def _artifact_list(root: Path) -> list[dict[str, Any]]:
    return [{"path": path.relative_to(root).as_posix(), "sha256": _sha(path), "bytes": path.stat().st_size}
            for path in sorted(root.rglob("*")) if path.is_file() and path.name != "run-manifest.json"]


def _verify_replay(output: Path, inputs: dict[str, str], run_id: str) -> dict[str, Any]:
    path = output / "run-manifest.json"
    if output.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("Existing demo output has no verified run manifest; choose a new output directory")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("protocol") != PROTOCOL or manifest.get("run_id") != run_id or manifest.get("inputs") != inputs:
        raise ValueError("Existing demo output uses different inputs or code; choose a new output directory")
    for entry in manifest["artifacts"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid demo artifact location")
        artifact = output / relative
        if (artifact.is_symlink() or not artifact.is_file() or not artifact.resolve().is_relative_to(output.resolve())
                or _sha(artifact) != entry["sha256"]):
            raise ValueError("Saved demo artifact changed; preserve it and choose a new output directory")
    with HistoryStore(output / "normalized/statistics.sqlite"):
        pass
    return manifest


@contextmanager
def _mysql_environment(config: str | Path | None):
    values = {}
    if config is not None:
        path = Path(config).expanduser()
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("MySQL config must be a regular private file readable only by its owner (mode 0600)")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("MySQL config must contain an environment object or flat UFC_MYSQL_* map")
        environment = document.get("environment", document)
        if not isinstance(environment, dict):
            raise ValueError("MySQL config environment must be an object")
        values = {key: value for key, value in environment.items() if key in MYSQL_KEYS}
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("MySQL environment values must be strings")
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_demo(output_dir: str | Path = DEFAULT_OUTPUT, *, fixtures_dir: str | Path | None = None,
             mysql: bool = False, config: str | Path | None = None,
             publisher: Callable[..., dict] | None = None) -> dict[str, Any]:
    """Build a new edition, or verify and reuse the identical existing edition."""
    if config is not None and not mysql:
        raise ValueError("--config is only read with explicit --mysql")
    fixture_dir = Path(fixtures_dir) if fixtures_dir else FIXTURE_DIRECTORY
    fixture, inputs = _fixtures(fixture_dir)
    run_id = "synthetic-demo-" + hashlib.sha256(_json(inputs).encode()).hexdigest()[:24]
    output = Path(output_dir).expanduser()
    reused = output.exists()
    if reused:
        manifest = _verify_replay(output, inputs, run_id)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".synthetic-demo-", dir=output.parent) as temporary:
            staging = Path(temporary) / "edition"
            (staging / "raw").mkdir(parents=True)
            (staging / "reports").mkdir()
            for name in FILES:
                shutil.copyfile(fixture_dir / name, staging / "raw" / name)
            (staging / "raw/manifest.json").write_text(_json(fixture), encoding="utf-8")
            database = staging / "normalized/statistics.sqlite"
            build_history(staging / "raw/manifest.json", database)
            quality = _quality(database, fixture["expected_quality"])
            coverage = export_coverage(database)
            coverage.update(synthetic=True, notice=NOTICE)
            sample = _sample(database, fixture)
            sample_html = render_html(sample).replace("<body>", "<body><p style='margin:0;padding:12px 18px;background:#fff0d9;color:#653b13;font:700 12px/1.5 Arial,sans-serif'>" + html.escape(NOTICE) + "</p>", 1)
            outputs = {"reports/quality.json": _json(quality), "reports/coverage.json": _json(coverage),
                       "reports/coverage.csv": _coverage_csv(coverage), "reports/card-statistics.json": render_json(sample),
                       "reports/card-statistics.html": sample_html, "reports/card-statistics.csv": render_csv(sample),
                       "index.html": _landing(quality, coverage, run_id)}
            for relative, content in outputs.items():
                (staging / relative).write_text(content, encoding="utf-8", newline="")
            manifest = {"schema_version": 1, "protocol": PROTOCOL, "synthetic": True, "notice": NOTICE,
                        "run_id": run_id, "inputs": inputs, "logical_fixture_capture_at_utc": fixture["captured_at_utc"],
                        "timestamp_policy": "fixed_logical_fixture_time_not_wall_clock_execution", "quality_counts": quality["counts"],
                        "artifacts": _artifact_list(staging)}
            (staging / "run-manifest.json").write_text(_json(manifest), encoding="utf-8")
            if output.exists() or output.is_symlink():
                raise FileExistsError("Demo output appeared during processing; no existing edition was replaced")
            staging.rename(output)
    result = {"status": "reused" if reused else "created", "synthetic": True, "run_id": run_id,
              "output_dir": str(output), "quality_counts": manifest["quality_counts"]}
    if mysql:
        if publisher is None:
            from .mysql_warehouse import publish_statistics
            publisher = publish_statistics
        with _mysql_environment(config):
            published = publisher(output / "normalized/statistics.sqlite", run_id=run_id)
        # Keep connector configuration and runtime details out of public logs.
        result["mysql"] = {key: published[key] for key in ("run_id", "status", "idempotent_reuse", "replayed", "source_sha256", "row_counts", "counts") if key in published}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mysql", action="store_true", help="Explicitly publish the verified synthetic SQLite store to the configured MySQL warehouse")
    parser.add_argument("--config", type=Path, help="Private external JSON containing allowed UFC_MYSQL_* environment values")
    args = parser.parse_args(argv)
    try:
        result = run_demo(args.output_dir, mysql=args.mysql, config=args.config)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
