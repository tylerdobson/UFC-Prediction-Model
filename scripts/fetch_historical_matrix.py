"""Save latest completed revisions for every earlier card at each decision.

This retrospectively fetches publisher evidence without changing its actual
download time. It is research-only and never imports into the operating DB.
Run as ``python -m scripts.fetch_historical_matrix`` from the project root.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.fetch_historical_revisions import canonical_utc, save_proof
from scripts.review_historical_pilot import validate_manifest


def required_matrix(events: list[dict]) -> list[tuple[str, int, str, str]]:
    """Return (earlier slug, page ID, target cutoff, target slug) in time order."""
    pairs: list[tuple[str, int, str, str]] = []
    for index, target in enumerate(events):
        for earlier in events[:index]:
            pairs.append((earlier["slug"], earlier["page_id"],
                          target["prefight_cutoff_at_utc"], target["slug"]))
    return pairs


def _already_complete(root: Path, slug: str, cutoff: str) -> bool:
    marker = cutoff.replace(":", "").replace("-", "")
    stem = root / slug / f"result-{marker}"
    return all(Path(f"{stem}.{part}").is_file() for part in (
        "selection.response.json", "selection.receipt.json",
        "content.response.json", "content.receipt.json", "manifest.json",
    ))


def fetch_matrix(
    manifest_path: str | Path,
    output_root: str | Path,
    *,
    max_new_proofs: int | None = None,
    sleep=time.sleep,
    save=save_proof,
) -> dict[str, int]:
    if max_new_proofs is not None and max_new_proofs < 1:
        raise ValueError("max_new_proofs must be positive")
    path = Path(manifest_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Historical pilot manifest must be a regular file")
    events = validate_manifest(json.loads(path.read_bytes()))
    requested_root = Path(output_root).expanduser()
    if requested_root.is_symlink():
        raise ValueError("Historical raw root must not be a symlink")
    root = requested_root.resolve()
    now = datetime.now(timezone.utc)
    pairs = required_matrix(events)
    new, reused = 0, 0
    for slug, page_id, cutoff, target_slug in pairs:
        if canonical_utc(cutoff) > now:
            raise ValueError(f"{target_slug}: historical decision is in the future")
        complete = _already_complete(root, slug, cutoff)
        if not complete and max_new_proofs is not None and new >= max_new_proofs:
            break
        if not complete and new:
            sleep(2.0)
        proof = save(root / slug, "result", page_id, cutoff)
        if proof["cutoff_at_utc"] != cutoff or proof["expected_page_id"] != page_id:
            raise ValueError(f"{slug}: saved proof differs from manifest")
        if complete:
            reused += 1
        else:
            new += 1
        print(json.dumps({"earlier": slug, "target": target_slug,
                          "cutoff_at_utc": cutoff,
                          "revision_id": proof["revision_id"],
                          "revision_timestamp_utc": proof["revision_timestamp_utc"],
                          "cached": complete}), flush=True)
    return {"required_pairs": len(pairs), "new_proofs": new,
            "reused_proofs": reused,
            "unvisited_pairs": len(pairs) - new - reused}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=Path("docs/HISTORICAL_2026_PILOT_CARDS.json"))
    parser.add_argument("--output-root", type=Path,
                        default=Path("data/raw/historical-revisions"))
    parser.add_argument("--max-new-proofs", type=int)
    args = parser.parse_args(argv)
    try:
        result = fetch_matrix(args.manifest, args.output_root,
                              max_new_proofs=args.max_new_proofs)
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"historical matrix fetch stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
