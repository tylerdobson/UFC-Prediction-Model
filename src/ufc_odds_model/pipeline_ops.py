"""Scheduled, observable data refreshes with a last-known-ready snapshot.

This pipeline captures source data and builds the canonical statistics store.
It never retrains a prediction model or mutates an original research database.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

from . import historical_stats, statistics_workflow

DEFAULT_OPS_ROOT = Path("data/raw/case-study-ops")
DEFAULT_CONFIG = Path.home() / "Library/Application Support/UFCForecast/pipeline-env.json"
CONFIG_OPTIONS = {"ops_root", "archive", "history", "commit", "repository", "mysql", "local_alert", "webhook_alert", "min_retained_fraction", "refresh_rankings"}
PROTOCOL = "ufc-statistics-operations-v1"


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _atomic_bytes(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing a symlink output")
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_json(path, value):
    _atomic_bytes(path, _encoded(value))


def read_config(path=None):
    """Read owner-private JSON without executing shell code or exporting secrets.

    Both {environment:{UFC_MYSQL_*:...},options:{...}} and a flat environment
    object are accepted. The caller applies the returned environment at runtime.
    """
    target = Path(path).expanduser() if path else DEFAULT_CONFIG
    if not target.exists():
        if path:
            raise ValueError("Configuration file is unavailable")
        return {}, {}
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Configuration must be an owner-private regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("Configuration must belong to the current user")
    document = json.loads(target.read_text())
    if not isinstance(document, dict):
        raise ValueError("Configuration must be a JSON object")
    if "environment" in document or "options" in document:
        if set(document) - {"environment", "options"}:
            raise ValueError("Unsupported configuration keys")
        environment, options = document.get("environment", {}), document.get("options", {})
    else:
        environment, options = document, {}
    if not isinstance(environment, dict) or not isinstance(options, dict) or set(options) - CONFIG_OPTIONS:
        raise ValueError("Unsupported configuration shape")
    for key, value in environment.items():
        if (not isinstance(key, str) or not (key.startswith("UFC_MYSQL_") or key == "UFC_PIPELINE_ALERT_WEBHOOK")
                or not isinstance(value, str) or "\x00" in value):
            raise ValueError("Unsupported configuration environment")
    for key, value in options.items():
        if key in {"mysql", "local_alert", "webhook_alert", "refresh_rankings"}:
            if type(value) is not bool:
                raise ValueError("Configuration flags must be boolean")
        elif key == "min_retained_fraction":
            _retained_fraction(value)
        elif value is not None and not isinstance(value, str):
            raise ValueError("Configuration paths and source selectors must be strings")
    return dict(environment), dict(options)


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def _single_run(root, run_id):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root / ".refresh.lock", flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another refresh owns this operations directory") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, _encoded({"run_id": run_id, "pid": os.getpid(), "started_at_utc": _utc()}))
        yield
    finally:
        os.close(descriptor)  # The kernel releases the lock even after a crash.


class _Journal:
    def __init__(self, root):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:10]
        self.path = root / "runs" / self.run_id / "run.json"
        self.value = {"schema_version": 1, "protocol": PROTOCOL, "run_id": self.run_id,
                      "status": "running", "started_at_utc": _utc(), "finished_at_utc": None,
                      "stages": [], "alerts": {}, "model_retraining": False}
        self.save()

    def save(self):
        _atomic_json(self.path, self.value)

    @contextmanager
    def stage(self, name):
        item = {"name": name, "status": "running", "started_at_utc": _utc()}
        self.value["stages"].append(item); self.save()
        started = time.monotonic()
        try:
            yield item
        except Exception as exc:
            item.update(status="failed", error_type=type(exc).__name__)
            raise
        else:
            item["status"] = "succeeded"
        finally:
            item.update(finished_at_utc=_utc(), duration_seconds=round(time.monotonic() - started, 6))
            self.save()


def _verify_archive(path):
    path = Path(path).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ValueError("Archive manifest must be a regular file")
    path = path.resolve()
    manifest = json.loads(path.read_text())
    commit = manifest.get("repository_commit", "")
    if manifest.get("schema_version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Archive must identify an immutable source commit")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("Archive has no source files")
    names = set()
    for item in entries:
        name = item.get("file")
        if not isinstance(name, str) or not name or Path(name).name != name or name in names:
            raise ValueError("Unsafe or duplicate archive file")
        names.add(name)
        source = path.parent / name
        if source.is_symlink() or not source.is_file() or _sha(source) != item.get("sha256"):
            raise ValueError("Archive source bytes do not match their receipt")
    if not set(historical_stats.FILES) <= names:
        raise ValueError("Archive lacks required canonical source files")
    return path, manifest


def _retain_offline_archive(path, sources_root):
    source, manifest = _verify_archive(path)
    fingerprint = _sha(source)
    target = sources_root / ("offline-" + manifest["repository_commit"] + "-" + fingerprint[:16])
    if target.exists():
        retained, _ = _verify_archive(target / "manifest.json")
        if _sha(retained) != fingerprint:
            raise ValueError("Retained offline source manifest changed")
        return retained
    sources_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".capture-", dir=sources_root))
    try:
        for item in manifest["files"]:
            shutil.copyfile(source.parent / item["file"], temporary / item["file"])
        (temporary / "manifest.json").write_bytes(source.read_bytes())
        _verify_archive(temporary / "manifest.json")
        temporary.rename(target)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return target / "manifest.json"


def _coverage_summary(store):
    counts = store.metadata["counts"]
    return {"cards": len(store.event_coverage()), "fighters": len(store.fighter_coverage()),
            "bouts": counts["canonical_archive_bouts"], "usable_paired_bouts": counts.get("usable_paired_bouts", 0),
            "source_round_rows": counts["source_round_rows"],
            "source_gaps": sum(1 for _ in store.connection.execute("SELECT bout_id FROM bouts WHERE stats_status='held'"))}


def _retained_fraction(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Minimum retained fraction must be a number from zero to one")
    return float(value)


def _ready_snapshot(root, pointer_bytes=None):
    """Verify a relocatable pointer and its immutable snapshot before trusting it."""
    if pointer_bytes is None:
        path = root / "latest-ready.json"
        if not path.exists():
            return None
        if path.is_symlink():
            raise ValueError("Ready pointer must not be a symlink")
        pointer_bytes = path.read_bytes()
    pointer = json.loads(pointer_bytes)
    relative = Path(pointer["version"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("versions",):
        raise ValueError("Unsafe ready version path")
    target = root / relative
    if _sha(target / "ready.json") != pointer["version_manifest_sha256"]:
        raise ValueError("Ready pointer manifest changed")
    raw = json.loads((target / "ready.json").read_text())
    ready = _verify_version(target, raw["inputs"])
    if (pointer.get("input_fingerprint") != ready["input_fingerprint"]
            or pointer.get("source_commit") != ready["inputs"]["source_commit"]
            or pointer.get("statistics_path") != (relative / "statistics.sqlite").as_posix()):
        raise ValueError("Ready pointer provenance changed")
    return {"pointer": pointer, "ready": ready}


def _quality_report(observed, previous, minimum):
    """Dataset-level gates do not reject an otherwise valid feed for isolated gaps."""
    checks = [{"check": "nonempty_" + key, "observed": observed[key], "minimum": 1,
               "passed": observed[key] > 0}
              for key in ("cards", "fighters", "bouts", "usable_paired_bouts")]
    if previous:
        checks += [{"check": "retained_" + key, "observed": observed[key], "previous": previous[key],
                    "minimum": previous[key] * minimum,
                    "passed": observed[key] >= previous[key] * minimum}
                   for key in ("cards", "bouts", "usable_paired_bouts")]
    return {"observed_counts": observed, "previous_counts": previous,
            "min_retained_fraction": minimum, "checks": checks,
            "passed": all(item["passed"] for item in checks),
            "individual_quarantined_bouts_are_fatal": False}


def _verify_version(target, inputs):
    if target.is_symlink():
        raise ValueError("Version directory must not be a symlink")
    ready_path = target / "ready.json"
    ready = json.loads(ready_path.read_text())
    if ready.get("status") != "ready" or ready.get("inputs") != inputs:
        raise ValueError("Existing version has different inputs")
    stated = ready.get("manifest_sha256")
    if stated != _digest({k: v for k, v in ready.items() if k != "manifest_sha256"}):
        raise ValueError("Version manifest content changed")
    for name, receipt in ready["artifacts"].items():
        if name not in {"statistics.sqlite", "coverage.json", "coverage.csv"}:
            raise ValueError("Unsafe version artifact path")
        path = target / name
        if path.is_symlink() or not path.is_file() or _sha(path) != receipt["sha256"]:
            raise ValueError("Version artifact bytes changed")
    with historical_stats.HistoryStore(target / "statistics.sqlite") as store:
        for key in ("source_manifest_sha256", "research_database_sha256", "builder_implementation_sha256"):
            if store.metadata.get(key) != inputs[key]:
                raise ValueError("Statistics store provenance does not match the version")
        if _coverage_summary(store) != ready["coverage"]:
            raise ValueError("Version coverage changed")
    return ready


def _build_version(root, manifest_path, manifest, history):
    inputs = {"source_commit": manifest["repository_commit"], "source_manifest_sha256": _sha(manifest_path),
              "research_database_sha256": _sha(history) if history else None,
              "builder_implementation_sha256": _sha(historical_stats.__file__)}
    fingerprint = _digest(inputs)
    relative = Path("versions") / manifest["repository_commit"] / fingerprint[:24]
    target = root / relative
    if target.exists():
        return target, _verify_version(target, inputs), True
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".build-", dir=target.parent))
    try:
        historical_stats.build_history(manifest_path, temporary / "statistics.sqlite", history)
        historical_stats.export_coverage(temporary / "statistics.sqlite", temporary / "coverage.json", temporary / "coverage.csv")
        with historical_stats.HistoryStore(temporary / "statistics.sqlite") as store:
            coverage = _coverage_summary(store)
            logical_digest = store.metadata["store_content_sha256"]
        ready = {"schema_version": 1, "status": "ready", "protocol": PROTOCOL, "inputs": inputs,
                 "input_fingerprint": fingerprint, "created_at_utc": _utc(),
                 "source_captured_at_utc": manifest["captured_at_utc"], "coverage": coverage,
                 "store_content_sha256": logical_digest,
                 "artifacts": {name: {"sha256": _sha(temporary / name), "bytes": (temporary / name).stat().st_size}
                               for name in ("statistics.sqlite", "coverage.json", "coverage.csv")},
                 "research_only": True, "model_retraining": False}
        ready["manifest_sha256"] = _digest(ready)
        _atomic_json(temporary / "ready.json", ready)
        _verify_version(temporary, inputs)
        temporary.rename(target)
        return target, ready, False
    except BaseException:
        shutil.rmtree(temporary)
        raise


def _publish_mysql(path, publication_id):
    from .mysql_warehouse import publish_statistics
    return publish_statistics(Path(path), run_id=publication_id)


def _mysql_receipt(result):
    if not isinstance(result, dict) or result.get("status") not in {"published", "already_published"}:
        raise ValueError("MySQL publication did not complete")
    clean = {"status": result["status"]}
    for key in ("run_id", "active_run_id", "source_sha256", "schema_version", "research_only"):
        if isinstance(result.get(key), (str, int, bool)):
            clean[key] = result[key]
    clean["counts"] = {key: value for key, value in result.get("counts", {}).items() if isinstance(value, int) and not isinstance(value, bool)}
    return clean


def send_failure_alerts(run, *, local=True, webhook=False):
    """Send no credentials, exception messages, source URLs, or config contents."""
    result = {"local": "disabled", "webhook": "disabled"}
    preservation = ('Inspect the MySQL publication receipt; replay reconciles interrupted publication.'
                    if run.get('database_backend') == 'mysql' else 'Last ready local snapshot preserved.')
    message = f"Run {run['run_id']} failed during {run.get('failed_stage', 'configuration')}. {preservation}"
    if local:
        if sys.platform == "darwin":
            script = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run'
            try:
                response = subprocess.run(["/usr/bin/osascript", "-e", script, message, "UFC statistics refresh failed"], capture_output=True, timeout=10, check=False)
                result["local"] = "sent" if response.returncode == 0 else "failed"
            except (OSError, subprocess.TimeoutExpired):
                result["local"] = "failed"
        else:
            result["local"] = "unavailable_on_this_platform"
    if webhook:
        address = os.environ.get("UFC_PIPELINE_ALERT_WEBHOOK", "")
        try:
            parsed = urlsplit(address)
        except ValueError:
            parsed = urlsplit("")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            result["webhook"] = "not_configured"
        else:
            body = _encoded({"event": "ufc_pipeline_failed", "run_id": run["run_id"], "stage": run.get("failed_stage"), "message": message})
            try:
                with urlopen(Request(address, data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=5) as response:
                    result["webhook"] = "sent" if 200 <= response.status < 300 else "failed"
            except Exception:
                result["webhook"] = "failed"
    return result


def refresh_pipeline(*, ops_root=DEFAULT_OPS_ROOT, archive=None, commit=None,
                     repository=statistics_workflow.DEFAULT_REPOSITORY, history=None,
                     mysql=False, local_alert=True, webhook_alert=False, min_retained_fraction=0.9):
    """Execute one refresh and return its persisted, secret-free run record."""
    root = Path(ops_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    journal = _Journal(root)
    started = time.monotonic()
    lock = None
    lock_acquired = False
    try:
        min_retained_fraction = _retained_fraction(min_retained_fraction)
        with journal.stage("lock"):
            lock = _single_run(root, journal.run_id)
            lock.__enter__()
            lock_acquired = True
        pointer_path = root / "latest-ready.json"
        previous_pointer = None
        activated = False
        try:
            with journal.stage("previous_snapshot"):
                if pointer_path.is_symlink():
                    raise ValueError("Ready pointer must not be a symlink")
                previous_pointer = pointer_path.read_bytes() if pointer_path.exists() else None
                previous = _ready_snapshot(root, previous_pointer) if previous_pointer is not None else None
            with journal.stage("source") as stage:
                if archive and commit:
                    raise ValueError("Choose either an offline archive or an upstream commit")
                if archive:
                    manifest_path = _retain_offline_archive(archive, root / "sources")
                else:
                    manifest_path = statistics_workflow.refresh_archive(root / "sources", commit=commit, repository=repository)
                manifest_path, manifest = _verify_archive(manifest_path)
                stage.update(source_commit=manifest["repository_commit"], source_manifest_sha256=_sha(manifest_path),
                             source_captured_at_utc=manifest["captured_at_utc"],
                             access_mode="retained_offline_archive" if archive else "pinned_upstream_refresh")
            with journal.stage("build_verify") as stage:
                target, ready, reused = _build_version(root, manifest_path, manifest, Path(history).expanduser().resolve() if history else None)
                stage.update(reused=reused, version=target.relative_to(root).as_posix(),
                             store_content_sha256=ready["store_content_sha256"], coverage=ready["coverage"])
            with journal.stage("quality") as stage:
                quality = _quality_report(ready["coverage"], previous["ready"]["coverage"] if previous else None,
                                          min_retained_fraction)
                stage.update(quality)
                if not quality["passed"]:
                    raise ValueError("Dataset coverage failed publication quality gates")
            if mysql:
                with journal.stage("mysql") as stage:
                    publication_id = "statistics-" + ready["artifacts"]["statistics.sqlite"]["sha256"][:32]
                    receipt = _mysql_receipt(_publish_mysql(target / "statistics.sqlite", publication_id))
                    stage["publication"] = receipt
            with journal.stage("activate") as stage:
                pointer = {"schema_version": 1, "version": target.relative_to(root).as_posix(),
                           "statistics_path": (target.relative_to(root) / "statistics.sqlite").as_posix(),
                           "source_commit": manifest["repository_commit"], "input_fingerprint": ready["input_fingerprint"],
                           "version_manifest_sha256": _sha(target / "ready.json")}
                unchanged = pointer_path.exists() and json.loads(pointer_path.read_text()) == pointer
                if not unchanged:
                    _atomic_json(pointer_path, pointer)
                    activated = True
                stage["pointer_unchanged"] = unchanged
            journal.value.update(status="succeeded", exit_code=0, version=pointer["version"],
                                 coverage=ready["coverage"], reused_version=reused)
            # Persist the completed run while the lock is still held. A journal
            # write failure after activation rolls the local pointer back.
            journal.value.update(finished_at_utc=_utc(), duration_seconds=round(time.monotonic() - started, 6))
            journal.save()
        except Exception:
            if activated:
                if previous_pointer is None:
                    pointer_path.unlink(missing_ok=True)
                else:
                    _atomic_bytes(pointer_path, previous_pointer)
            raise
    except Exception as exc:
        failed_stage = next((s["name"] for s in reversed(journal.value["stages"]) if s["status"] == "failed"), "configuration")
        journal.value.update(status="failed", exit_code=75 if isinstance(exc, AlreadyRunning) else 1,
                             failed_stage=failed_stage, error_type=type(exc).__name__,
                             error_code="already_running" if isinstance(exc, AlreadyRunning) else failed_stage + "_failed")
        journal.value["alerts"] = send_failure_alerts(journal.value, local=local_alert, webhook=webhook_alert)
        journal.value.update(finished_at_utc=_utc(), duration_seconds=round(time.monotonic() - started, 6))
        journal.save()
    finally:
        if lock_acquired:
            lock.__exit__(None, None, None)
    return journal.value


def _configuration_failure(root, exc):
    journal = _Journal(Path(root).expanduser().resolve())
    journal.value.update(status="failed", exit_code=2, failed_stage="configuration", error_type=type(exc).__name__, error_code="configuration_failed", finished_at_utc=_utc())
    journal.value["alerts"] = send_failure_alerts(journal.value, local=True, webhook=False)
    journal.save()
    return journal.value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="Capture, normalize, verify and publish a data version")
    refresh.add_argument("--ops-root", type=Path)
    refresh.add_argument("--archive", type=Path, help="Retained archive manifest; use --no-refresh-rankings for offline replay")
    refresh.add_argument("--commit", help="Optional full immutable upstream Git SHA")
    refresh.add_argument("--repository")
    history_args = refresh.add_mutually_exclusive_group()
    history_args.add_argument("--history", type=Path, help="Optional read-only MySQL research ID crosswalk")
    history_args.add_argument('--no-history', action='store_true', help='Build canonical identities without a research crosswalk')
    refresh.add_argument("--config", type=Path, help="Owner-private runtime JSON; default external config is auto-detected")
    refresh.add_argument("--mysql", action=argparse.BooleanOptionalAction, default=None)
    refresh.add_argument('--refresh-rankings', action=argparse.BooleanOptionalAction, default=None,
                         help='Refresh official rankings too (default true); disable for offline archive replay')
    refresh.add_argument("--local-alert", action=argparse.BooleanOptionalAction, default=None)
    refresh.add_argument("--webhook-alert", action=argparse.BooleanOptionalAction, default=None,
                         help="Explicit opt-in; URL comes only from UFC_PIPELINE_ALERT_WEBHOOK")
    refresh.add_argument("--min-retained-fraction", type=float,
                         help="Minimum previous card/bout coverage to retain, 0..1; default 0.9")
    status = commands.add_parser("status", help="Show the last ready local snapshot")
    status.add_argument("--ops-root", type=Path, default=DEFAULT_OPS_ROOT)
    args = parser.parse_args(argv)
    if args.command == "status":
        try:
            from .mysql_pipeline import status as mysql_status
            print(json.dumps(mysql_status(), indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"ready": False, "status": "verification_failed", "error_type": type(exc).__name__}))
            return 1
    try:
        environment, options = read_config(args.config)
        os.environ.update(environment)
        for key in CONFIG_OPTIONS:
            value = getattr(args, key)
            if value is not None:
                options[key] = value
        if args.no_history:
            options['history'] = None
        if options.pop('mysql', True):
            from .mysql_pipeline import refresh_pipeline as refresh_mysql
            result = refresh_mysql(**options)
        else:
            # Explicit legacy mode is retained only for offline fixture/replay
            # compatibility. MySQL failure never enters this branch.
            options.pop('refresh_rankings', None)
            result = refresh_pipeline(**options)
    except Exception as exc:
        try:
            result = _configuration_failure(args.ops_root or DEFAULT_OPS_ROOT, exc)
        except Exception:
            result = {"run_id": "unavailable", "status": "failed", "exit_code": 2}
    # Full stage detail is retained in run.json; no exception text or environment
    # values are printed to a launchd log or terminal.
    print(json.dumps({key: result[key] for key in ("run_id", "status", "exit_code")}, sort_keys=True))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
