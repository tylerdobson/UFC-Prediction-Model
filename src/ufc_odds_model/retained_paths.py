"""Locate an unchanged retained file after copying the repository.

The recorded name remains immutable. Callers must still verify its original
SHA-256. Only a missing absolute path under data/raw can be rebased; an
existing or linked recorded file is never bypassed.
"""
from pathlib import Path


def retained_path(recorded, *, reference=None):
    path = Path(recorded).expanduser()
    if not path.is_absolute() or path.exists() or path.is_symlink():
        return path
    parts = path.parts
    offsets = [i for i in range(len(parts)-1) if parts[i:i+2] == ("data", "raw")]
    if not offsets:
        return path
    root = Path.cwd()
    if reference is not None:
        location = Path(reference).resolve()
        root = next((p.parent for p in location.parents if p.name == "data"), root)
    return root.joinpath(*parts[offsets[-1]:])
