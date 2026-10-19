"""Manage only this project's daily 09:00 local-time user LaunchAgent.

Secrets remain in an owner-private JSON file read by pipeline_ops at runtime.
This installer never starts MySQL or changes another LaunchAgent.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from ufc_odds_model.pipeline_ops import DEFAULT_CONFIG, DEFAULT_OPS_ROOT, read_config

LABEL = "com.ufcforecast.statistics-refresh"


def _run(arguments):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=False)


def schedule_plist(project, *, config=None, mysql=None, hour=9, minute=0):
    project = Path(project).expanduser().resolve()
    interpreter = project / ".venv/bin/python"
    if not interpreter.is_file() or not (project / "src/ufc_odds_model/pipeline_ops.py").is_file():
        raise ValueError("Project requires its existing virtual environment and pipeline module")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Schedule time must be a valid local hour and minute")
    selected = Path(config).expanduser().resolve() if config else DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None
    environment, options = read_config(selected) if selected else ({}, {})
    if mysql is None:
        mysql = options.get("mysql", any(k.startswith("UFC_MYSQL_") for k in environment))
    root = Path(options.get("ops_root", DEFAULT_OPS_ROOT)).expanduser()
    if not root.is_absolute():
        root = project / root
    arguments = [str(interpreter), "-m", "ufc_odds_model.pipeline_ops", "refresh", "--ops-root", str(root),
                 "--local-alert", "--no-webhook-alert", "--mysql" if mysql else "--no-mysql"]
    if selected:
        arguments += ["--config", str(selected)]
    return {"Label": LABEL, "ProgramArguments": arguments, "WorkingDirectory": str(project),
            "EnvironmentVariables": {"PYTHONPATH": str(project / "src"), "PYTHONUNBUFFERED": "1"},
            "StartCalendarInterval": {"Hour": hour, "Minute": minute}, "RunAtLoad": False,
            "ProcessType": "Background", "ThrottleInterval": 60,
            "StandardOutPath": str(root / "scheduler.stdout.log"),
            "StandardErrorPath": str(root / "scheduler.stderr.log")}


def _write_plist(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("LaunchAgent path must not be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".ufc-refresh-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def manage_schedule(command, *, project=PROJECT, config=None, mysql=None, hour=9, minute=0,
                    dry_run=False, launch_agents=None, runner=_run):
    """Install/status/uninstall one fixed-label per-user agent; injectable in tests."""
    directory = Path(launch_agents) if launch_agents else Path.home() / "Library/LaunchAgents"
    path = directory / (LABEL + ".plist")
    service = f"gui/{os.getuid()}/{LABEL}"
    if command == "status":
        response = runner(["/bin/launchctl", "print", service])
        return {"label": LABEL, "installed": path.is_file(), "loaded": response.returncode == 0,
                "plist_path": str(path), "schedule": "daily local time"}
    if command == "uninstall":
        if dry_run:
            return {"status": "planned", "action": "uninstall", "label": LABEL}
        if path.is_symlink():
            raise ValueError("LaunchAgent path must not be a symlink")
        runner(["/bin/launchctl", "bootout", service])
        path.unlink(missing_ok=True)
        return {"status": "uninstalled", "label": LABEL}
    if command != "install":
        raise ValueError("Unknown schedule operation")
    definition = schedule_plist(project, config=config, mysql=mysql, hour=hour, minute=minute)
    if dry_run:
        return {"status": "planned", "label": LABEL, "definition": definition}
    root = Path(definition["StandardOutPath"]).parent
    root.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("LaunchAgent path must not be a symlink")
    previous = path.read_bytes() if path.exists() else None
    body = plistlib.dumps(definition)
    runner(["/bin/launchctl", "bootout", service])
    try:
        _write_plist(path, body)
        response = runner(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        if response.returncode != 0:
            raise RuntimeError("launchd rejected the refresh LaunchAgent")
    except BaseException:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _write_plist(path, previous)
            runner(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        raise
    return {"status": "installed", "label": LABEL, "plist_path": str(path),
            "schedule": f"daily {hour:02d}:{minute:02d} local time", "mysql": "--mysql" in definition["ProgramArguments"],
            "alerts": "local Mac failure notification", "run_at_load": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("install", "status", "uninstall"))
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mysql", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--hour", type=int, default=9)
    parser.add_argument("--minute", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = manage_schedule(args.command, project=args.project, config=args.config, mysql=args.mysql,
                                 hour=args.hour, minute=args.minute, dry_run=args.dry_run)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": "Refresh schedule operation failed; configuration values are not logged."}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
