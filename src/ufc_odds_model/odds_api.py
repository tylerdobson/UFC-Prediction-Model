"""Fetch and normalize live MMA fight-winner quotes from The Odds API.

The API's MMA sport includes promotions other than UFC. Match returned bouts to
an independently verified UFC card before treating their quotes as UFC odds.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_ODDS_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
_HISTORICAL_URL = "https://api.the-odds-api.com/v4/historical/sports/mma_mixed_martial_arts/odds"


def fetch_mma_h2h(api_key: str, regions: str = "us") -> list[dict]:
    """Return the unmodified list of live/upcoming MMA h2h event objects.

    A valid API key is required. Network, HTTP, and malformed response failures
    raise RuntimeError so an ingest job cannot silently record an empty slate.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("The Odds API key is required")
    if not isinstance(regions, str) or not regions.strip():
        raise ValueError("At least one bookmaker region is required")

    query = urlencode(
        {
            "apiKey": api_key.strip(),
            "regions": regions.strip(),
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
    )
    request = Request(f"{_ODDS_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = _api_error_detail(exc, api_key.strip())
        raise RuntimeError(f"The Odds API returned HTTP {exc.code}{detail}") from exc
    except URLError as exc:
        reason = str(exc.reason).replace(api_key.strip(), "[redacted]")
        raise RuntimeError(f"Could not reach The Odds API: {reason}") from exc
    except OSError as exc:
        raise RuntimeError("Could not reach The Odds API") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The Odds API returned invalid JSON") from exc

    if not isinstance(payload, list) or not all(isinstance(event, dict) for event in payload):
        raise RuntimeError("The Odds API returned an unexpected events payload")
    return payload


def fetch_historical_mma_h2h(
    api_key: str, as_of: datetime | str, regions: str = "us"
) -> dict:
    """Fetch the latest available historical snapshot at or before `as_of`.

    Historical odds require a plan with The Odds API's historical access. The
    response timestamp, rather than the requested timestamp, is used when
    recording quotes.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("The Odds API key is required")
    if not isinstance(regions, str) or not regions.strip():
        raise ValueError("At least one bookmaker region is required")
    requested = _utc_timestamp(as_of)
    if requested is None:
        raise ValueError("as_of must be a timezone-aware ISO 8601 timestamp")
    query = urlencode({
        "apiKey": api_key.strip(), "regions": regions.strip(), "markets": "h2h",
        "oddsFormat": "decimal", "dateFormat": "iso", "date": requested,
    })
    request = Request(f"{_HISTORICAL_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = _api_error_detail(exc, api_key.strip())
        raise RuntimeError(f"The Odds API returned HTTP {exc.code}{detail}") from exc
    except URLError as exc:
        reason = str(exc.reason).replace(api_key.strip(), "[redacted]")
        raise RuntimeError(f"Could not reach The Odds API: {reason}") from exc
    except OSError as exc:
        raise RuntimeError("Could not reach The Odds API") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The Odds API returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("The Odds API returned an unexpected historical payload")
    snapshot = _utc_timestamp(payload.get("timestamp"))
    if snapshot is None or snapshot > requested:
        raise RuntimeError("The Odds API returned an invalid or future snapshot timestamp")
    if not all(isinstance(event, dict) for event in payload["data"]):
        raise RuntimeError("The Odds API returned invalid historical events")
    return payload


def normalize_h2h(payload: list[dict], captured_at: datetime | str) -> list[dict]:
    """Flatten valid two-fighter h2h markets to one quote per fighter.

    Timestamps are UTC ISO 8601 strings. A market is discarded if it lacks two
    fighter selections, valid decimal prices, or a quote update timestamp.
    """
    if not isinstance(payload, list):
        raise ValueError("The Odds API payload must be a list of events")
    captured_at_utc = _utc_timestamp(captured_at)
    if captured_at_utc is None:
        raise ValueError("captured_at must be a timezone-aware ISO 8601 timestamp")

    rows: list[dict] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        event_id = _nonempty_text(event.get("id"))
        fighter_a = _nonempty_text(event.get("home_team"))
        fighter_b = _nonempty_text(event.get("away_team"))
        commence_time = _utc_timestamp(event.get("commence_time"))
        if not all((event_id, fighter_a, fighter_b, commence_time)) or fighter_a == fighter_b:
            continue
        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            continue

        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            bookmaker_key = _nonempty_text(bookmaker.get("key"))
            markets = bookmaker.get("markets")
            if not bookmaker_key or not isinstance(markets, list):
                continue
            for market in markets:
                if not isinstance(market, dict) or market.get("key") != "h2h":
                    continue
                updated_at = _utc_timestamp(market.get("last_update"))
                if updated_at is None:
                    updated_at = _utc_timestamp(bookmaker.get("last_update"))
                outcomes = market.get("outcomes")
                if updated_at is None or not isinstance(outcomes, list) or len(outcomes) != 2:
                    continue

                prices: dict[str, float] = {}
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        break
                    name = _nonempty_text(outcome.get("name"))
                    price = outcome.get("price")
                    if name not in (fighter_a, fighter_b) or not _valid_decimal_odds(price):
                        break
                    prices[name] = float(price)
                if set(prices) != {fighter_a, fighter_b}:
                    continue

                for selection_name in (fighter_a, fighter_b):
                    rows.append(
                        {
                            "source_event_id": event_id,
                            "commence_time_utc": commence_time,
                            "fighter_a": fighter_a,
                            "fighter_b": fighter_b,
                            "bookmaker_key": bookmaker_key,
                            "selection_name": selection_name,
                            "decimal_odds": prices[selection_name],
                            "bookmaker_updated_at": updated_at,
                            "captured_at_utc": captured_at_utc,
                        }
                    )
    return rows


def _nonempty_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _valid_decimal_odds(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        price = float(value)
    except OverflowError:
        return False
    return math.isfinite(price) and price > 1


def _utc_timestamp(value: object) -> str | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _api_error_detail(exc: HTTPError, api_key: str) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(message, str) and message.strip():
            return f": {message.strip().replace(api_key, '[redacted]')[:200]}"
    except (OSError, UnicodeError, ValueError):
        pass
    return ""
