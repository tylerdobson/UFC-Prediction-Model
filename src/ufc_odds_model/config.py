"""Read local provider credentials without evaluating shell code."""

from __future__ import annotations

import os
import stat
from pathlib import Path


_PROVIDER_KEYS = frozenset({"ODDS_API_KEY", "SPORTRADAR_API_KEY"})


def provider_key(name: str, env_file: str | Path = ".env") -> str:
    """Use an exported key first, then a private .env in the current directory.

    Only allowlisted provider keys are read. The file is parsed as text rather
    than sourced as a shell script, and no secret value appears in errors.
    """
    if name not in _PROVIDER_KEYS:
        raise ValueError("Unknown provider key name")
    exported = os.environ.get(name, "").strip()
    if exported:
        return exported

    path = Path(env_file).expanduser()
    if not path.exists():
        return ""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Provider .env must be a regular file")
    details = path.stat()
    if os.name == "posix" and stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("Provider .env must be private (chmod 600 .env)")
    if details.st_size > 16_384:
        raise ValueError("Provider .env is unexpectedly large")

    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        label, separator, value = line.partition("=")
        label = label.strip()
        if not separator or label not in _PROVIDER_KEYS:
            continue
        if label in found:
            raise ValueError("Provider .env has a duplicate key name")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Provider .env has an unterminated quoted value")
            value = value[1:-1]
        found[label] = value

    key = found.get(name, "").strip()
    if key:
        os.environ[name] = key
    return key
