"""Immutable, content-addressed copies of imported source payloads."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def retain_snapshot(
    payload: bytes,
    snapshot_dir: str | Path,
    suffix: str = ".json",
    kind: str = "raw",
) -> tuple[Path, str]:
    """Atomically publish bytes once, and reject a changed existing hash path."""
    digest = hashlib.sha256(payload).hexdigest()
    directory = Path(snapshot_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory.resolve() / f"{digest}{suffix}"

    def verify_existing() -> None:
        if (destination.is_symlink() or not destination.is_file()
                or destination.read_bytes() != payload):
            raise ValueError(
                f"Existing {kind} snapshot differs from its content address: {destination}"
            )

    if destination.exists() or destination.is_symlink():
        verify_existing()
        return destination, digest

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".raw-import-", suffix=".tmp", dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A concurrent import cannot replace an existing path.
            os.link(temporary, destination)
        except FileExistsError:
            verify_existing()
    finally:
        temporary.unlink(missing_ok=True)
    return destination, digest
