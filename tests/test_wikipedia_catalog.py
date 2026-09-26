"""Offline checks for the Wikipedia year-page event catalog."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from ufc_odds_model.wikipedia_catalog import enumerate_event_catalog, parse_year_page


def _page(year: int, rows: str, *, prefix: str = "") -> dict:
    return {
        "id": 80000 + year,
        "key": f"{year}_in_UFC",
        "title": f"{year} in UFC",
        "latest": {"id": 90000 + year, "timestamp": "2026-06-17T09:14:04Z"},
        "license": {
            "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en",
            "title": "Creative Commons Attribution-Share Alike 4.0",
        },
        "source": (
            prefix + "\n==Events list==\n{{Main|List of UFC events}}\n"
            '{| class="sortable wikitable"\n'
            '! scope="col" | #\n! scope="col" | Event\n! scope="col" | Date\n'
            '! scope="col" | Venue\n|-\n' + rows + '\n|}\n== References ==\n'
            '{| class="wikitable"\n! Event\n! Date\n|-\n|1\n|[[Not a fight]]\n|{{dts|2011|Jan|1}}\n|}'
        ),
    }


class CatalogParserTests(unittest.TestCase):
    def test_parses_only_event_table_with_nested_sort_and_flags_canceled_row(self) -> None:
        rows = (
            '|193\n|[[UFC 141|UFC 141: Lesnar vs. Overeem]]\n|{{dts|2011|Dec|30}}\n|Venue\n|-\n'
            '|192\n|{{sort|Ultimate Fighter 14|[[The Ultimate Fighter: Team Bisping vs. Team Miller Finale]]}}\n'
            '|{{dts|2011|Dec|3}}\n|Venue\n|-\n'
            '|align=center| &nbsp;–\n|[[UFC 176|UFC 176: Aldo vs. Mendes II]]\n|{{dts|2011|Aug|2}}\n|Venue'
        )
        prefix = '== Title fights ==\n{| class="wikitable"\n! Event\n! Date\n|-\n|1\n|[[False]]\n|{{dts|2011|Jan|1}}\n|}\n'
        result = parse_year_page(_page(2011, rows, prefix=prefix), 2011)
        self.assertEqual([event.page_title for event in result.events], [
            "UFC 141", "The Ultimate Fighter: Team Bisping vs. Team Miller Finale",
        ])
        self.assertEqual([event.event_date for event in result.events], ["2011-12-30", "2011-12-03"])
        self.assertEqual(result.review[0].reason, "non_numeric_event_sequence")
        self.assertEqual(result.page_id, 82011)
        self.assertEqual(result.revision_id, 92011)
        self.assertEqual(result.raw_payload["title"], "2011 in UFC")

    def test_parses_rowspan_plain_date_and_comment_but_skips_bonus_subrow(self) -> None:
        rows = (
            '|rowspan="2"|538\n'
            '|rowspan="2"|[[UFC 254|UFC 254: Khabib vs. Gaethje]]\n'
            '|rowspan="2"|{{dts|2020|Oct|24}}<!-- local date note -->\n'
            '|rowspan="2"|Venue\n|-\n'
            '|[[Fighter A]]\n|Fighter B\n|$50,000\n|-\n'
            '|537\n|[[UFC Fight Night: Ortega vs. The Korean Zombie]]\n'
            '|October 18, 2020\n|Venue'
        )
        result = parse_year_page(_page(2020, rows), 2020)
        self.assertEqual([event.event_number for event in result.events], [538, 537])
        self.assertEqual([event.event_date for event in result.events], ["2020-10-24", "2020-10-18"])
        self.assertEqual(result.review, ())

    def test_section_link_is_reviewed_and_invalid_event_date_is_reviewed(self) -> None:
        rows = (
            '|381\n|[[The Ultimate Fighter: Tournament of Champions#Finale|TUF Finale]]\n'
            '|{{dts|2016|Dec|3}}\n|Venue\n|-\n'
            '|380\n|[[UFC Fight Night 100]]\n|{{dts|2016|Nov|19}}\n|Venue\n|-\n'
            '|379\n|[[UFC Fight Night 99]]\n|{{dts|2017|Nov|19}}\n|Venue'
        )
        result = parse_year_page(_page(2016, rows), 2016)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(len(result.review), 2)
        self.assertIn("section", result.review[0].reason)
        self.assertIn("outside", result.review[1].reason)

    def test_resolves_redirects_to_stable_page_ids_and_reviews_missing_pages(self) -> None:
        page = _page(2018, (
            '|463\n|[[UFC on Fox 31|UFC on Fox: Lee vs. Iaquinta 2]]\n'
            '|{{dts|2018|Dec|15}}\n|Venue\n|-\n'
            '|462\n|[[Nonexistent UFC event]]\n|{{dts|2018|Dec|8}}\n|Venue'
        ))
        urls: list[str] = []

        def fetch(url: str) -> dict:
            urls.append(url)
            if url.endswith("/2018_in_UFC"):
                return page
            titles = parse_qs(urlsplit(url).query)["titles"][0].split("|")
            self.assertEqual(set(titles), {"UFC on Fox 31", "Nonexistent UFC event"})
            return {"query": {
                "redirects": [{"from": "UFC on Fox 31", "to": "UFC on Fox: Lee vs. Iaquinta 2"}],
                "pages": [
                    {"pageid": 57859650, "title": "UFC on Fox: Lee vs. Iaquinta 2", "ns": 0},
                    {"title": "Nonexistent UFC event", "ns": 0, "missing": True},
                ],
            }}

        result = enumerate_event_catalog(2018, 2018, fetch_json=fetch)
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].page_title, "UFC on Fox: Lee vs. Iaquinta 2")
        self.assertEqual(result.events[0].page_id, 57859650)
        self.assertEqual(result.events[0].source_page_id, 82018)
        self.assertEqual(result.review[0].reason, "unresolved_or_disambiguation_page")
        self.assertEqual(result.years[0].raw_payload, page)

    def test_duplicate_page_is_reviewed(self) -> None:
        page = _page(2025, (
            '|758\n|[[UFC 323]]\n|{{dts|2025|Dec|13}}\n|Venue\n|-\n'
            '|757\n|[[UFC 323]]\n|{{dts|2025|Dec|6}}\n|Venue'
        ))
        result = enumerate_event_catalog(2025, 2025, fetch_json=lambda _: page, resolve_titles=False)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.review[0].reason, "duplicate_event_page")

    def test_redirect_to_year_page_is_not_a_standalone_event(self) -> None:
        page = _page(2012, (
            '|207\n|[[UFC on FX: Johnson vs. McCall]]\n|{{dts|2012|Jun|8}}\n|Venue'
        ))

        def fetch(url: str) -> dict:
            if url.endswith("/2012_in_UFC"):
                return page
            return {"query": {
                "redirects": [{"from": "UFC on FX: Johnson vs. McCall", "to": "2012 in UFC"}],
                "pages": [{"pageid": page["id"], "title": "2012 in UFC", "ns": 0}],
            }}

        result = enumerate_event_catalog(2012, 2012, fetch_json=fetch)
        self.assertEqual(result.events, ())
        self.assertEqual(result.review[0].reason, "canonical_not_event_page")

    def test_finale_link_to_season_article_is_reviewed(self) -> None:
        page = _page(2011, (
            '|191\n|[[The Ultimate Fighter: Team Bisping vs. Team Miller|'
            'The Ultimate Fighter: Team Bisping vs. Team Miller Finale]]\n'
            '|{{dts|2011|Dec|3}}\n|Venue'
        ))

        def fetch(url: str) -> dict:
            if url.endswith("/2011_in_UFC"):
                return page
            return {"query": {"pages": [{
                "pageid": 31061251,
                "title": "The Ultimate Fighter: Team Bisping vs. Team Miller",
                "ns": 0,
            }]}}

        result = enumerate_event_catalog(2011, 2011, fetch_json=fetch)
        self.assertEqual(result.events, ())
        self.assertEqual(result.review[0].reason, "canonical_not_event_page")

    def test_rejects_wrong_revision_license_and_range(self) -> None:
        page = _page(2011, '|193\n|[[UFC 141]]\n|{{dts|2011|Dec|30}}\n|Venue')
        bad = dict(page, title="2012 in UFC")
        with self.assertRaisesRegex(ValueError, "Expected 2011 in UFC"):
            parse_year_page(bad, 2011)
        bad = dict(page, license={"url": "https://example.com/unknown"})
        with self.assertRaisesRegex(ValueError, "unexpected source license"):
            parse_year_page(bad, 2011)
        with self.assertRaisesRegex(ValueError, "Year range"):
            enumerate_event_catalog(2025, 2011, fetch_json=lambda _: page)


if __name__ == "__main__":
    unittest.main()
