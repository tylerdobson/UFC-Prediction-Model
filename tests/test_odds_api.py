"""Offline tests for The Odds API adapter."""

import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ufc_odds_model.odds_api import fetch_historical_mma_h2h, fetch_mma_h2h, normalize_h2h


def _event():
    return {
        "id": "mma-123",
        "sport_key": "mma_mixed_martial_arts",
        "commence_time": "2026-10-04T01:00:00Z",
        "home_team": "Fighter A",
        "away_team": "Fighter B",
        "bookmakers": [
            {
                "key": "examplebook",
                "last_update": "2026-10-03T11:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-10-03T12:00:00Z",
                        "outcomes": [
                            {"name": "Fighter A", "price": 1.80},
                            {"name": "Fighter B", "price": 2.10},
                        ],
                    }
                ],
            }
        ],
    }


class FetchMmaH2hTests(unittest.TestCase):
    @patch("ufc_odds_model.odds_api.urlopen")
    def test_fetches_raw_mma_events_with_decimal_h2h_query(self, urlopen):
        raw = [_event()]
        urlopen.return_value = io.BytesIO(json.dumps(raw).encode("utf-8"))

        self.assertEqual(fetch_mma_h2h("test-key", "us,uk"), raw)

        request = urlopen.call_args.args[0]
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(urlparse(request.full_url).path, "/v4/sports/mma_mixed_martial_arts/odds")
        self.assertEqual(query["apiKey"], ["test-key"])
        self.assertEqual(query["regions"], ["us,uk"])
        self.assertEqual(query["markets"], ["h2h"])
        self.assertEqual(query["oddsFormat"], ["decimal"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_missing_key_fails_before_network_call(self, urlopen):
        with self.assertRaisesRegex(ValueError, "key is required"):
            fetch_mma_h2h(" ")
        urlopen.assert_not_called()

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_http_error_reports_status_and_api_message(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://example.test", 401, "Unauthorized", {},
            io.BytesIO(b'{"message": "Invalid API key"}'),
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 401: Invalid API key"):
            fetch_mma_h2h("bad-key")

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_http_error_does_not_expose_key(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://example.test", 401, "Unauthorized", {},
            io.BytesIO(b'{"message": "Invalid key secret-key"}'),
        )
        with self.assertRaises(RuntimeError) as raised:
            fetch_mma_h2h("secret-key")
        self.assertNotIn("secret-key", str(raised.exception))

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_network_error_is_explicit(self, urlopen):
        urlopen.side_effect = URLError("connection refused")
        with self.assertRaisesRegex(RuntimeError, "Could not reach The Odds API"):
            fetch_mma_h2h("test-key")

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_malformed_response_does_not_look_like_empty_event_list(self, urlopen):
        urlopen.return_value = io.BytesIO(b'{"message": "failure"}')
        with self.assertRaisesRegex(RuntimeError, "unexpected events payload"):
            fetch_mma_h2h("test-key")

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_historical_snapshot_uses_provider_timestamp(self, urlopen):
        raw = {"timestamp": "2026-10-03T07:55:00Z", "data": [_event()]}
        urlopen.return_value = io.BytesIO(json.dumps(raw).encode("utf-8"))
        self.assertEqual(fetch_historical_mma_h2h("test-key", "2026-10-03T08:00:00Z"), raw)
        query = parse_qs(urlparse(urlopen.call_args.args[0].full_url).query)
        self.assertEqual(query["date"], ["2026-10-03T08:00:00Z"])
        self.assertEqual(query["markets"], ["h2h"])
        self.assertIn("/v4/historical/sports/mma_mixed_martial_arts/odds", urlopen.call_args.args[0].full_url)

    @patch("ufc_odds_model.odds_api.urlopen")
    def test_historical_future_snapshot_is_rejected(self, urlopen):
        raw = {"timestamp": "2026-10-03T08:05:00Z", "data": []}
        urlopen.return_value = io.BytesIO(json.dumps(raw).encode("utf-8"))
        with self.assertRaisesRegex(RuntimeError, "future snapshot"):
            fetch_historical_mma_h2h("test-key", "2026-10-03T08:00:00Z")


class NormalizeH2hTests(unittest.TestCase):
    def test_flattens_two_fighter_market_with_market_timestamp(self):
        captured = datetime(2026, 10, 3, 8, 30, tzinfo=timezone.utc)
        rows = normalize_h2h([_event()], captured)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {
            "source_event_id": "mma-123",
            "commence_time_utc": "2026-10-04T01:00:00Z",
            "fighter_a": "Fighter A",
            "fighter_b": "Fighter B",
            "bookmaker_key": "examplebook",
            "selection_name": "Fighter A",
            "decimal_odds": 1.8,
            "bookmaker_updated_at": "2026-10-03T12:00:00Z",
            "captured_at_utc": "2026-10-03T08:30:00Z",
        })
        self.assertEqual(rows[1]["selection_name"], "Fighter B")
        self.assertEqual(rows[1]["decimal_odds"], 2.1)

    def test_skips_invalid_or_three_way_markets(self):
        event = _event()
        market = event["bookmakers"][0]["markets"][0]
        market["outcomes"][0]["price"] = 1.0
        self.assertEqual(normalize_h2h([event], "2026-10-03T08:30:00Z"), [])

        market["outcomes"][0]["price"] = 1.8
        market["outcomes"].append({"name": "Draw", "price": 20.0})
        self.assertEqual(normalize_h2h([event], "2026-10-03T08:30:00Z"), [])

    def test_rejects_nonfinite_and_oversized_prices(self):
        event = _event()
        market = event["bookmakers"][0]["markets"][0]
        for bad_price in (float("inf"), float("nan"), True, 10**1000):
            market["outcomes"][0]["price"] = bad_price
            self.assertEqual(normalize_h2h([event], "2026-10-03T08:30:00Z"), [])

    def test_falls_back_to_bookmaker_timestamp(self):
        event = _event()
        del event["bookmakers"][0]["markets"][0]["last_update"]
        rows = normalize_h2h([event], "2026-10-03T08:30:00Z")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["bookmaker_updated_at"], "2026-10-03T11:00:00Z")

    def test_rejects_naive_capture_time(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            normalize_h2h([], datetime(2026, 10, 3, 8, 30))


if __name__ == "__main__":
    unittest.main()
