"""Retrospective revision receipts must stay distinct from local fetch time."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

from ufc_odds_model.publisher_revisions import (
    ACTION_API, ApiReceipt, verify_historical_revision,
)


PAGE_ID = 83648230
REVISION_ID = 1375565079
CUTOFF = '2026-09-19T00:00:00Z'
PUBLISHED = '2026-09-18T15:47:38Z'
FETCHED = '2026-09-26T22:00:00Z'
WIKITEXT = '{{Infobox MMA event|name=UFC 331|date={{start date|2026|9|19}}}}\n== Fight card ==\n'


def _sha1_base36(source: str) -> str:
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
    value = int(hashlib.sha1(source.encode('utf-8')).hexdigest(), 16)
    digits = ''
    while value:
        value, digit = divmod(value, 36)
        digits = alphabet[digit] + digits
    return digits.zfill(31)


def _url(**params: str) -> str:
    return ACTION_API + '?' + urlencode(params)


class PublisherRevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.digest = _sha1_base36(WIKITEXT)
        self.selection_payload = self._payload(False)
        self.content_payload = self._payload(True)
        self.selection_url = _url(
            action='query', prop='revisions', format='json', formatversion='2',
            rvslots='main', pageids=str(PAGE_ID), rvdir='older', rvstart=CUTOFF,
            rvlimit='1', rvprop='ids|timestamp|sha1|slotsha1', maxlag='5',
        )
        self.content_url = _url(
            action='query', prop='revisions', format='json', formatversion='2',
            rvslots='main', revids=str(REVISION_ID),
            rvprop='ids|timestamp|sha1|slotsha1|content|contentmodel', maxlag='5',
        )

    def _payload(self, content: bool) -> dict:
        main: dict = {'sha1': self.digest}
        if content:
            main.update(contentmodel='wikitext', content=WIKITEXT)
        return {
            'batchcomplete': True,
            'query': {'pages': [{
                'pageid': PAGE_ID, 'ns': 0, 'title': 'UFC 331',
                'revisions': [{
                    'revid': REVISION_ID, 'timestamp': PUBLISHED,
                    'sha1': self.digest, 'slots': {'main': main},
                }],
            }]},
        }

    def _receipt(self, payload: dict, name: str, url: str,
                 fetched: str = FETCHED) -> ApiReceipt:
        path = self.dir / f'{name}.json'
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        path.write_bytes(body)
        return ApiReceipt(path, hashlib.sha256(body).hexdigest(), url, fetched)

    def _prove(self, selection: dict | None = None, content: dict | None = None,
               selection_url: str | None = None, content_url: str | None = None,
               selection_fetched: str = FETCHED, content_fetched: str = FETCHED):
        selected = self._receipt(
            self.selection_payload if selection is None else selection,
            'selection', selection_url or self.selection_url, selection_fetched,
        )
        selected_content = self._receipt(
            self.content_payload if content is None else content,
            'content', content_url or self.content_url, content_fetched,
        )
        return verify_historical_revision(
            selected, selected_content, expected_page_id=PAGE_ID,
            cutoff_at_utc=CUTOFF,
        )

    def test_accepts_two_exact_retroactive_receipts_without_backdating_fetch(self) -> None:
        proof = self._prove()
        self.assertEqual(proof.revision_timestamp_utc, PUBLISHED)
        self.assertEqual(proof.content_receipt.fetched_at_utc, FETCHED)
        self.assertEqual(proof.selection_receipt.fetched_at_utc, FETCHED)
        self.assertEqual(proof.cutoff_at_utc, CUTOFF)
        self.assertEqual(proof.page_id, PAGE_ID)
        self.assertEqual(proof.wikitext, WIKITEXT)
        self.assertEqual(proof.wikitext_sha256,
                         hashlib.sha256(WIKITEXT.encode('utf-8')).hexdigest())
        self.assertEqual(proof.source_url,
                         f'https://en.wikipedia.org/w/index.php?oldid={REVISION_ID}')

    def test_tampered_bytes_or_self_reported_content_fail(self) -> None:
        selection = self._receipt(self.selection_payload, 'selection', self.selection_url)
        content = self._receipt(self.content_payload, 'content', self.content_url)
        Path(content.path).write_bytes(Path(content.path).read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            verify_historical_revision(
                selection, content, expected_page_id=PAGE_ID, cutoff_at_utc=CUTOFF,
            )
        altered = deepcopy(self.content_payload)
        altered['query']['pages'][0]['revisions'][0]['slots']['main']['content'] += 'changed'
        with self.assertRaisesRegex(ValueError, 'Publisher SHA-1'):
            self._prove(content=altered)

    def test_selection_request_must_be_latest_at_exact_cutoff(self) -> None:
        for changed in (
            self.selection_url.replace('rvdir=older', 'rvdir=newer'),
            self.selection_url.replace('rvlimit=1', 'rvlimit=2'),
            self.selection_url.replace('rvstart=2026-09-19T00%3A00%3A00Z',
                                       'rvstart=2026-09-20T00%3A00%3A00Z'),
            self.selection_url + '&rvstart=' + CUTOFF,
            self.selection_url.replace('en.wikipedia.org', 'example.org'),
        ):
            with self.subTest(url=changed), self.assertRaises(ValueError):
                self._prove(selection_url=changed)

    def test_at_or_after_cutoff_revision_cannot_be_used(self) -> None:
        selected = deepcopy(self.selection_payload)
        selected['query']['pages'][0]['revisions'][0]['timestamp'] = CUTOFF
        with self.assertRaisesRegex(ValueError, 'not published before'):
            self._prove(selection=selected)

    def test_wrong_content_revision_or_page_cannot_substitute(self) -> None:
        mismatched = deepcopy(self.content_payload)
        mismatched['query']['pages'][0]['revisions'][0]['revid'] += 1
        with self.assertRaisesRegex(ValueError, 'differs from selected'):
            self._prove(content=mismatched)
        mismatched_page = deepcopy(self.content_payload)
        mismatched_page['query']['pages'][0]['pageid'] += 1
        with self.assertRaisesRegex(ValueError, 'page ID/title'):
            self._prove(content=mismatched_page)

    def test_missing_or_ambiguous_selection_is_held(self) -> None:
        absent = deepcopy(self.selection_payload)
        absent['query']['pages'][0]['revisions'] = []
        with self.assertRaisesRegex(ValueError, 'exactly one accessible revision'):
            self._prove(selection=absent)
        doubled = deepcopy(self.selection_payload)
        doubled['query']['pages'][0]['revisions'] *= 2
        with self.assertRaisesRegex(ValueError, 'exactly one accessible revision'):
            self._prove(selection=doubled)

    def test_content_must_follow_selection_and_be_visible_wikitext(self) -> None:
        with self.assertRaisesRegex(ValueError, 'predates the selection'):
            self._prove(content_fetched='2026-09-26T21:59:59Z')
        hidden = deepcopy(self.content_payload)
        hidden['query']['pages'][0]['revisions'][0]['slots']['main'].pop('content')
        with self.assertRaisesRegex(ValueError, 'no main-slot wikitext'):
            self._prove(content=hidden)


if __name__ == '__main__':
    unittest.main()
