"""Run a project-owned MySQL instance using an existing macOS installation.

The system instance is never reconfigured. Credentials live in owner-only files
outside the repository; the application user can access only its own database.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys
import time

LABEL = "com.ufcforecast.mysql"
BASE = Path.home() / "Library/Application Support/UFCForecast"
DEFAULT_STATE = BASE / "mysql"
DEFAULT_CONFIG = BASE / "pipeline-env.json"


def private_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError("Credential destination must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def read_private(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Credential file must be an owner-only regular file")
    return json.loads(path.read_text())


def binary_dir() -> Path:
    found = shutil.which("mysqld")
    candidates = [Path(found).parent] if found else []
    candidates += [Path("/usr/local/mysql/bin"), Path("/opt/homebrew/opt/mysql/bin"), Path("/opt/homebrew/opt/mysql@8.4/bin")]
    for candidate in candidates:
        if (candidate / "mysqld").is_file():
            return candidate.resolve()
    raise ValueError("MySQL server binaries were not found; install MySQL first")


def client(**kwargs):
    try:
        import pymysql
    except ImportError:
        raise ValueError("Install the warehouse extra: uv sync --extra warehouse") from None
    return pymysql.connect(connect_timeout=3, read_timeout=10, write_timeout=10, autocommit=True, **kwargs)


def server_arguments(state: Path, port: int, *, bootstrap: bool = False) -> list[str]:
    binaries = binary_dir()
    args = [str(binaries / "mysqld"), "--no-defaults", f"--basedir={binaries.parent}",
            f"--datadir={state / 'data'}", f"--socket={state / 'mysql.sock'}",
            f"--pid-file={state / 'mysql.pid'}", f"--log-error={state / 'mysql.log'}",
            "--mysqlx=0", "--innodb-buffer-pool-size=64M"]
    args += ["--skip-networking"] if bootstrap else ["--bind-address=127.0.0.1", f"--port={port}"]
    return args


def wait_connection(state: Path, password: str, timeout: float = 25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return client(user="root", password=password, unix_socket=str(state / "mysql.sock"))
        except Exception:
            time.sleep(0.2)
    raise ValueError(f"Project MySQL did not become ready; inspect {state / 'mysql.log'}")


def launch(state: Path, port: int, password: str, *, bootstrap: bool = False) -> None:
    with (state / "process.log").open("ab") as log:
        subprocess.Popen(server_arguments(state, port, bootstrap=bootstrap), stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    connection = wait_connection(state, password)
    connection.close()


def stop_server(state: Path, admin: dict) -> None:
    try:
        connection = client(user="root", password=admin["password"], unix_socket=str(state / "mysql.sock"))
    except Exception:
        if (state / "mysql.pid").exists():
            raise ValueError("Project server has a PID file but cannot be authenticated; inspect its log") from None
        return
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@datadir")
            if Path(cursor.fetchone()[0]).resolve() != (state / "data").resolve():
                raise ValueError("Refusing to stop a server outside the project data directory")
            cursor.execute("SHUTDOWN")
    deadline = time.monotonic() + 20
    while (state / "mysql.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if (state / "mysql.pid").exists():
        raise ValueError("Project MySQL shutdown did not finish")


def setup(state: Path, config: Path, port: int) -> dict:
    if (state / "admin.json").exists():
        admin = read_private(state / "admin.json")
        if admin["port"] != port or Path(admin["config"]) != config:
            raise ValueError("Existing project instance uses different configuration; use its saved port/config")
        if not config.is_file():
            raise ValueError("Project setup is incomplete: application configuration is missing; preserve the data and admin recovery file")
        read_private(config)
        start(state)
        return {"status": "ready", "reused": True, "port": port, "config": str(config)}
    if (state / "data").exists() or config.exists() or config.is_symlink():
        raise ValueError("Setup destinations already exist; existing data and credentials are preserved")
    # Fail before initialization if another process already owns this port.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    binaries = binary_dir()
    result = subprocess.run([str(binaries / "mysqld"), "--no-defaults", "--initialize-insecure",
                             f"--basedir={binaries.parent}", f"--datadir={state / 'data'}",
                             f"--log-error={state / 'mysql.log'}"], capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError(f"MySQL initialization failed; inspect {state / 'mysql.log'}")
    # Initial passwordless root exists only over this private Unix socket.
    launch(state, port, "", bootstrap=True)
    root_password, app_password = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    admin = {"password": root_password, "port": port, "config": str(config)}
    with client(user="root", password="", unix_socket=str(state / "mysql.sock")) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (root_password,))
            # Save recovery credentials immediately after the password changes.
            private_json(state / "admin.json", admin)
            cursor.execute("CREATE DATABASE ufc_warehouse CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
            for host in ("127.0.0.1", "localhost"):
                cursor.execute("CREATE USER %s@%s IDENTIFIED BY %s", ("ufc_pipeline", host, app_password))
                cursor.execute("GRANT ALL PRIVILEGES ON ufc_warehouse.* TO %s@%s", ("ufc_pipeline", host))
    environment = {"UFC_MYSQL_HOST": "127.0.0.1", "UFC_MYSQL_PORT": str(port),
                   "UFC_MYSQL_USER": "ufc_pipeline", "UFC_MYSQL_PASSWORD": app_password,
                   "UFC_MYSQL_DATABASE": "ufc_warehouse"}
    private_json(config, {"environment": environment, "options": {"mysql": True}})
    stop_server(state, admin)
    launch(state, port, root_password)
    return {"status": "ready", "reused": False, "port": port, "database": "ufc_warehouse", "config": str(config)}


def start(state: Path) -> dict:
    admin = read_private(state / "admin.json")
    try:
        connection = client(user="root", password=admin["password"], unix_socket=str(state / "mysql.sock"))
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT @@datadir, @@port")
                data_dir, port = cursor.fetchone()
                if Path(data_dir).resolve() != (state / "data").resolve() or port != admin["port"]:
                    raise ValueError("Unexpected server on project socket")
        return {"status": "ready", "port": port}
    except ValueError:
        raise
    except Exception:
        pass
    launch(state, admin["port"], admin["password"])
    return {"status": "ready", "port": admin["port"]}


def service(state: Path, *, install: bool) -> dict:
    if sys.platform != "darwin":
        raise ValueError("The local service installer supports macOS launchd")
    admin = read_private(state / "admin.json")
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if install:
        plist = {"Label": LABEL, "ProgramArguments": server_arguments(state, admin["port"]),
                 "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60,
                 "StandardOutPath": str(state / "service.stdout.log"),
                 "StandardErrorPath": str(state / "service.stderr.log")}
        if target.exists():
            if plistlib.loads(target.read_bytes()) != plist:
                raise ValueError("Existing launch agent differs; uninstall it before changing configuration")
            if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode == 0:
                return {"status": "installed", "label": LABEL, "plist": str(target)}
        stop_server(state, admin)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps(plist))
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True, capture_output=True)
        connection = wait_connection(state, admin["password"])
        connection.close()
        return {"status": "installed", "label": LABEL, "plist": str(target)}
    if target.exists():
        saved = plistlib.loads(target.read_bytes())
        if saved.get("Label") != LABEL or saved.get("ProgramArguments") != server_arguments(state, admin["port"]):
            raise ValueError("Refusing to remove a launch agent with a different configuration")
        subprocess.run(["launchctl", "bootout", domain, str(target)], capture_output=True)
        target.unlink()
    return {"status": "uninstalled", "label": LABEL}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "start", "stop", "status", "install-service", "uninstall-service"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=3307)
    args = parser.parse_args(argv)
    state, config = args.state_dir.expanduser().resolve(), args.config.expanduser().absolute()
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("Use an unprivileged TCP port between 1024 and 65535")
        if args.command == "setup":
            result = setup(state, config, args.port)
        elif args.command == "start":
            result = start(state)
        elif args.command == "stop":
            stop_server(state, read_private(state / "admin.json"))
            result = {"status": "stopped"}
        elif args.command == "status":
            admin = read_private(state / "admin.json")
            with client(user="root", password=admin["password"], unix_socket=str(state / "mysql.sock")) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT @@version, @@port, @@datadir")
                    version, port, datadir = cursor.fetchone()
            if Path(datadir).resolve() != (state / "data").resolve():
                raise ValueError("Unexpected server data directory")
            result = {"status": "ready", "version": version, "host": "127.0.0.1", "port": port, "data_directory": datadir}
        else:
            result = service(state, install=args.command == "install-service")
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        # Never echo database exceptions: authentication/SQL text can contain secrets.
        message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}; inspect the project MySQL log"
        print(f"Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
