"""Release gate for the private UFC 332 read-only hosted snapshot.

Run after copying the original research evidence and restoring both portable
database bundles. It never writes to either database or places a wager.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ufc_odds_model.dashboard_data import load_dashboard, load_ufc332_forward_research
from ufc_odds_model.deployment import DeploymentError, check_dashboard_deployment


_EVIDENCE_FILES = {
    "card_manifest": "data/raw/ufc332-odds-20260927T1602Z/card-with-odds-manifest.json",
    "odds_manifest": "data/raw/ufc332-odds-20260927T1602Z/odds-intake-manifest.json",
    "card_csv": "data/raw/ufc332-odds-20260927T1602Z/reviewed-card-template.csv",
}


def check_hosted_snapshot(root: Path, original_root: Path) -> dict:
    """Verify the two isolated databases and unchanged Sep 27 forward proofs."""
    operating_db = root / "data/restored-ufc332-pilot/database.sqlite"
    history_db = root / "data/restored-research-history/database.sqlite"
    operating_check = check_dashboard_deployment(root, operating_db)
    history_check = check_dashboard_deployment(root, history_db)

    as_of = datetime.now(timezone.utc)
    operating = load_dashboard(operating_db, as_of=as_of)
    history = load_dashboard(
        history_db,
        as_of=as_of,
        research_report=root / "reports/ufc_research_1993_2026_holdout.json",
    )
    if (operating.get("status") != "available"
            or operating.get("data_origin") != "non_demo"
            or not any(row.get("event_id") == "wikipedia_pilot:83826247"
                       for row in operating.get("upcoming_events") or [])):
        raise ValueError("The operating snapshot lacks the reviewed UFC 332 card.")
    research = history.get("historical_research") or {}
    if (history.get("status") != "available"
            or history.get("data_origin") != "research_only"
            or research.get("receipt_status") != "verified"
            or history.get("research_evaluation", {}).get("status") != "available"):
        raise ValueError("The separate historical research database or holdout is unavailable.")

    forward = load_ufc332_forward_research(
        root / "reports/ufc332-forward-research-20260927T1601Z.json",
        root / "reports/ufc332-forward-research-full-20260927T1601Z.json",
        evidence_root=root,
        original_evidence_root=original_root,
        evidence_files=_EVIDENCE_FILES,
    )
    if (forward.get("status") != "available"
            or forward.get("capture_receipts_verified") is not True
            or len(forward.get("rows") or []) != 8):
        raise ValueError(
            "Saved UFC 332 forward research did not verify: "
            + str(forward.get("reason") or forward.get("status"))
        )
    return {
        "status": "ready_for_private_read_only_review",
        "operating_verified_receipts": operating_check["verified_receipts"],
        "history_verified_receipts": history_check["verified_receipts"],
        "history_events": research["events"],
        "history_results": research["results"],
        "forward_selected_bouts": len(forward["rows"]),
        "forward_capture_receipts_verified": True,
        "alerts_enabled": False,
        "bets_placed_by_app": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path,
                        help="Physical absolute path to the copied checkout")
    parser.add_argument("--original-root", required=True, type=Path,
                        help="Original absolute checkout path embedded in saved research reports")
    args = parser.parse_args()
    try:
        result = check_hosted_snapshot(args.root, args.original_root)
    except (DeploymentError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
