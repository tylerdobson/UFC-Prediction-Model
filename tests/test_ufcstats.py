"""Fixture tests for the UFCStats HTML import boundary; no network calls."""

import unittest
from unittest.mock import patch

from ufc_odds_model import ufcstats


EVENT_ID = "1234567890abcdef"
EVENT_URL = f"http://ufcstats.com/event-details/{EVENT_ID}"
BOUT_ID = "abcdef1234567890"
FIGHTER_A_ID = "aaaaaaaaaaaaaaaa"
FIGHTER_B_ID = "bbbbbbbbbbbbbbbb"

EVENT_LIST_HTML = f"""
<table class="b-statistics__table-events"><thead><tr><th>Name/date</th></tr></thead>
<tbody><tr class="b-statistics__table-row"><td>
  <a href="{EVENT_URL}">UFC Test: A vs. B</a>
  <span class="b-statistics__date">September 26, 2026</span>
</td><td>Las Vegas, Nevada, USA</td></tr></tbody></table>
"""


def bout_row(flags: str, method: str, bout_id: str = BOUT_ID) -> str:
    return f"""
    <tr class="b-fight-details__table-row b-fight-details__table-row__hover"
        data-link="http://ufcstats.com/fight-details/{bout_id}">
      <td>{flags}</td>
      <td><p><a href="http://ufcstats.com/fighter-details/{FIGHTER_A_ID}">Fighter A</a></p>
          <p><a href="http://ufcstats.com/fighter-details/{FIGHTER_B_ID}">Fighter B</a></p></td>
      <td></td><td></td><td></td><td></td>
      <td><p>Lightweight<br><img src="belt.png" alt=""></p></td>
      <td><p>{method}</p></td>
      <td></td><td></td>
    </tr>
    """


def event_html(rows: str) -> str:
    return f"""
    <h2><span class="b-content__title-highlight">UFC Test: A vs. B</span></h2>
    <table class="b-fight-details__table b-fight-details__table_type_event-details">
      <thead><tr><th>W/L</th><th>Fighter</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """


class EventListTests(unittest.TestCase):
    def test_completed_event_link_and_date(self):
        with patch.object(ufcstats, "_fetch_html", return_value=EVENT_LIST_HTML) as fetch:
            events = ufcstats.fetch_completed_events()
        fetch.assert_called_once_with(ufcstats.COMPLETED_EVENTS_URL)
        self.assertEqual(events, [{
            "source_event_id": EVENT_ID,
            "source_event_url": EVENT_URL,
            "name": "UFC Test: A vs. B",
            "date": "2026-09-26",
            "location": "Las Vegas, Nevada, USA",
        }])

    def test_upcoming_event_uses_upcoming_endpoint(self):
        with patch.object(ufcstats, "_fetch_html", return_value=EVENT_LIST_HTML) as fetch:
            events = ufcstats.fetch_upcoming_events()
        fetch.assert_called_once_with(ufcstats.UPCOMING_EVENTS_URL)
        self.assertEqual(events[0]["source_event_id"], EVENT_ID)

    def test_missing_table_raises(self):
        with self.assertRaisesRegex(ufcstats.UFCStatsParseError, "event table"):
            ufcstats._parse_event_list("<html>Checking your browser</html>")


class BoutTests(unittest.TestCase):
    def test_completed_win_and_linked_fighter_ids(self):
        flags = f'<p><a class="b-flag" href="http://ufcstats.com/fight-details/{BOUT_ID}">win</a></p>'
        html = event_html(bout_row(flags, "U-DEC"))
        with patch.object(ufcstats, "_fetch_html", return_value=html):
            bout = ufcstats.fetch_event_bouts(EVENT_URL)[0]
        self.assertEqual(bout["source_bout_id"], BOUT_ID)
        self.assertEqual(bout["source_event_id"], EVENT_ID)
        self.assertEqual(bout["fighter_a_source_id"], FIGHTER_A_ID)
        self.assertEqual(bout["fighter_b_source_id"], FIGHTER_B_ID)
        self.assertEqual(bout["winner_source_id"], FIGHTER_A_ID)
        self.assertEqual(bout["winner_name"], "Fighter A")
        self.assertEqual(bout["weight_class"], "Lightweight")
        self.assertEqual(bout["status"], "completed")
        self.assertEqual(bout["outcome"], "win")

    def test_scheduled_row_has_no_result(self):
        html = event_html(bout_row("", ""))
        bout = ufcstats._parse_event_bouts(html, EVENT_URL)[0]
        self.assertEqual(bout["status"], "scheduled")
        self.assertIsNone(bout["outcome"])
        self.assertIsNone(bout["winner_source_id"])
        self.assertIsNone(bout["method"])

    def test_draw_and_no_contest_have_no_winner(self):
        for flag, outcome in (("draw", "draw"), ("nc", "no_contest")):
            with self.subTest(flag=flag):
                flags = f'<p><a class="b-flag">{flag}</a></p>' * 2
                bout = ufcstats._parse_event_bouts(
                    event_html(bout_row(flags, "S-DEC")), EVENT_URL
                )[0]
                self.assertEqual(bout["outcome"], outcome)
                self.assertIsNone(bout["winner_name"])
                self.assertEqual(bout["status"], "completed")

    def test_unrecognized_result_raises(self):
        html = event_html(bout_row('<a class="b-flag">pending</a>', "U-DEC"))
        with self.assertRaisesRegex(ufcstats.UFCStatsParseError, "unrecognized result"):
            ufcstats._parse_event_bouts(html, EVENT_URL)

    def test_fighter_count_must_be_two(self):
        html = event_html(bout_row("", "").replace("Fighter B</a>", "</a>").replace(
            f'<p><a href="http://ufcstats.com/fighter-details/{FIGHTER_B_ID}"></a></p>', ""
        ))
        with self.assertRaisesRegex(ufcstats.UFCStatsParseError, "two linked fighters"):
            ufcstats._parse_event_bouts(html, EVENT_URL)


if __name__ == "__main__":
    unittest.main()
