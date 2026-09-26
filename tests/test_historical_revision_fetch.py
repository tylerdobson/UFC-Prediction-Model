"""Offline checks for exact, resumable historical revision acquisition."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scripts.fetch_historical_revisions import (
    content_url, parse_selection, save_proof, selection_url,
)


PAGE_ID = 83648230
CUTOFF = "2026-09-18T21:00:00Z"
REVISED = "2026-09-18T15:47:38Z"
WIKITEXT = "{{Infobox MMA event|name=UFC 331|date=2026-09-19}}\n== Fight card ==\n"
SHA1 = hashlib.sha1(WIKITEXT.encode("utf-8")).hexdigest()


def _payload(include_content: bool) -> bytes:
    main = {"sha1": SHA1}
    if include_content:
        main.update({"contentmodel": "wikitext", "content": WIKITEXT})
    revision = {"revid": 1375565079, "timestamp": REVISED,
                "sha1": SHA1, "slots": {"main": main}}
    page = {"pageid": PAGE_ID, "ns": 0, "title": "UFC 331",
            "revisions": [revision]}
    return json.dumps({"batchcomplete": True, "query": {"pages": [page]},
                       **({"continue": {"rvcontinue": "later", "continue": "||"}}
                          if not include_content else {})}).encode("utf-8")


class HistoricalRevisionFetchTests(unittest.TestCase):
    def test_exact_query_contract_and_continuation(self):
        query = parse_qs(urlsplit(selection_url(PAGE_ID, CUTOFF)).query)
        self.assertEqual(query["rvstart"], [CUTOFF])
        self.assertEqual(query["rvdir"], ["older"])
        self.assertEqual(query["rvprop"], ["ids|timestamp|sha1|slotsha1"])
        self.assertEqual(parse_selection(_payload(False), PAGE_ID, CUTOFF)["revision_id"],
                         1375565079)
        content_query = parse_qs(urlsplit(content_url(1375565079)).query)
        self.assertEqual(content_query["revids"], ["1375565079"])
        self.assertEqual(content_query["rvprop"],
                         ["ids|timestamp|sha1|slotsha1|content|contentmodel"])

    def test_saved_receipts_are_resumable_and_tamper_evident(self):
        calls: list[str] = []

        def fake_fetch(url: str) -> tuple[bytes, str]:
            calls.append(url)
            return (_payload("revids" in parse_qs(urlsplit(url).query)),
                    "2026-09-26T23:17:00Z")

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            proof = save_proof(path, "prefight", PAGE_ID, CUTOFF, fetch=fake_fetch)
            self.assertEqual(len(calls), 2)
            self.assertEqual(proof["revision_id"], 1375565079)
            self.assertEqual(proof["selection"]["fetched_at_utc"],
                             "2026-09-26T23:17:00Z")
            self.assertEqual(proof["content"]["fetched_at_utc"],
                             "2026-09-26T23:17:00Z")
            save_proof(path, "prefight", PAGE_ID, CUTOFF, fetch=fake_fetch)
            self.assertEqual(len(calls), 2)
            raw = Path(proof["selection"]["response_path"])
            raw.write_bytes(raw.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "verification"):
                save_proof(path, "prefight", PAGE_ID, CUTOFF, fetch=fake_fetch)


if __name__ == "__main__":
    unittest.main()
