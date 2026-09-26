"""Boundaries and evidence checks for result cards embedded in summary pages."""

from __future__ import annotations

import unittest

from ufc_odds_model.wikipedia_embedded import (
    embedded_event_id,
    normalize_section_key,
    parse_embedded_event,
    target_from_catalog_review,
)


WIN = """{{MMAevent bout
|Lightweight
|[[Alex Pereira]]
|def.
|[[Jiří Procházka]]
|TKO (punches)
|2
|4:08
|}}
"""


def _payload(event_date: str = "December 15, 2012") -> dict:
    return {
        "id": 36855029,
        "title": "The Ultimate Fighter: Team Carwin vs. Team Nelson",
        "latest": {"id": 1351910152, "timestamp": "2026-01-02T03:04:05Z"},
        "license": {
            "title": "Creative Commons Attribution-Share Alike 4.0",
            "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en",
        },
        "source": (
            "==Episodes==\n"
            "===Results===\n"
            "{{MMAevent bout|Bad|Wrong|def.|Wrong|TKO}}\n"
            "==The Ultimate Fighter 16 Finale==\n"
            "{{Infobox MMA event\n"
            "|name=The Ultimate Fighter: Team Carwin vs. Team Nelson Finale\n"
            f"|date={event_date}\n"
            "}}\n"
            "===Background===\n"
            "===Results===\n"
            "{{MMAevent}}\n"
            f"{WIN}"
            "===Bonus Awards===\n"
            "Awards text.\n"
            "==The Ultimate Fighter 17 Finale==\n"
            "{{Infobox MMA event|name=Other finale|date=April 13, 2013}}\n"
            "===Results===\n"
            "{{MMAevent bout|Bad|Wrong|def.|Wrong|TKO}}\n"
        ),
    }


def _parse(payload: dict | None = None, **overrides: object):
    options: dict = {
        "source_page_title": "The Ultimate Fighter: Team Carwin vs. Team Nelson",
        "target_heading": "The Ultimate Fighter 16 Finale",
        "expected_date": "2012-12-15",
        "expected_page_id": 36855029,
    }
    options.update(overrides)
    return parse_embedded_event(payload or _payload(), **options)


class WikipediaEmbeddedTest(unittest.TestCase):
    def test_parses_only_named_section_and_nested_results(self) -> None:
        event = _parse()
        self.assertEqual(event.source_page_id, 36855029)
        self.assertEqual(event.section_key, "the ultimate fighter 16 finale")
        self.assertEqual(event.section_name, "The Ultimate Fighter 16 Finale")
        self.assertEqual(event.event_date, "2012-12-15")
        self.assertEqual(event.revision_id, 1351910152)
        self.assertIn("by-sa/4.0", event.license_url)
        self.assertEqual(len(event.bouts), 1)
        self.assertEqual(event.bouts[0].fighter_a.name, "Alex Pereira")
        self.assertEqual(event.bouts[0].fighter_b.name, "Jiří Procházka")
        self.assertEqual(
            event.event_id,
            embedded_event_id(36855029, "The Ultimate Fighter 16 Finale"),
        )

    def test_anchor_normalization_is_exact(self) -> None:
        self.assertEqual(
            normalize_section_key("The_Ultimate_Fighter_16_Finale"),
            "the ultimate fighter 16 finale",
        )
        self.assertEqual(_parse(target_heading="The_Ultimate_Fighter_16_Finale").event_date, "2012-12-15")
        with self.assertRaisesRegex(ValueError, "found 0"):
            _parse(target_heading="The Ultimate Fighter 18 Finale")

    def test_wrong_date_page_or_license_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "differs from catalog"):
            _parse(expected_date="2012-12-16")
        with self.assertRaisesRegex(ValueError, "page ID differs"):
            _parse(expected_page_id=123)
        payload = _payload()
        payload["license"] = {"url": "https://example.test/other"}
        with self.assertRaisesRegex(ValueError, "unexpected source license"):
            _parse(payload)

    def test_duplicate_heading_or_results_fails(self) -> None:
        payload = _payload()
        payload["source"] += "\n==The Ultimate Fighter 16 Finale==\n"
        with self.assertRaisesRegex(ValueError, "found 2"):
            _parse(payload)
        payload = _payload()
        payload["source"] = payload["source"].replace(
            "===Bonus Awards===", "===Results===", 1
        )
        with self.assertRaisesRegex(ValueError, "exactly one Results"):
            _parse(payload)

    def test_bout_markup_outside_results_fails(self) -> None:
        payload = _payload()
        payload["source"] = payload["source"].replace(
            "===Background===\n", "===Background===\n" + WIN, 1
        )
        with self.assertRaisesRegex(ValueError, "outside Results"):
            _parse(payload)

    def test_section_missing_infobox_or_bouts_fails(self) -> None:
        payload = _payload()
        payload["source"] = payload["source"].replace(
            "{{Infobox MMA event\n", "{{Other box\n", 1
        )
        with self.assertRaisesRegex(ValueError, "exactly one event infobox"):
            _parse(payload)
        payload = _payload()
        payload["source"] = payload["source"].replace(WIN, "", 1)
        with self.assertRaisesRegex(ValueError, "no bout templates"):
            _parse(payload)

    def test_direct_sibling_results_is_bounded_by_next_level_two_heading(self) -> None:
        payload = _payload()
        payload["source"] = payload["source"].replace(
            "===Results===\n{{MMAevent}}", "==Results==\n{{MMAevent}}", 1
        )
        payload["source"] = payload["source"].replace(
            "===Bonus Awards===\nAwards text.",
            "==Coaches' fight==\nNo MMAevent bouts here.",
            1,
        )
        event = _parse(payload)
        self.assertEqual(len(event.bouts), 1)

    def test_nested_and_sibling_results_is_ambiguous(self) -> None:
        payload = _payload()
        payload["source"] = payload["source"].replace(
            "==The Ultimate Fighter 17 Finale==",
            "==Results==\n==The Ultimate Fighter 17 Finale==",
            1,
        )
        with self.assertRaisesRegex(ValueError, "multiple Results"):
            _parse(payload)

    def test_catalog_review_maps_only_explicit_verified_targets(self) -> None:
        linked = {
            "year": 2013,
            "raw_number": "238",
            "reason": "Event link points to a section, not an event page",
            "raw_event": "[[The Ultimate Fighter: Brazil 2#Finale|UFC on Fuel TV: Nogueira vs. Werdum]]",
            "raw_date": "{{dts|2013|Jun|8}}",
        }
        target = target_from_catalog_review(linked)
        self.assertEqual(target.source_page_title, "The Ultimate Fighter: Brazil 2")
        self.assertEqual(target.target_heading, "The Ultimate Fighter: Brazil 2 Finale")
        self.assertEqual(target.expected_date, "2013-06-08")
        redirect = {
            "year": 2012,
            "raw_number": "207",
            "reason": "canonical_not_event_page",
            "raw_event": "UFC on FX: Johnson vs. McCall",
            "raw_date": "2012-06-08",
        }
        self.assertEqual(
            target_from_catalog_review(redirect).target_heading,
            "UFC on FX: Johnson vs. McCall 2",
        )
        redirect["raw_event"] = "Another event"
        with self.assertRaisesRegex(ValueError, "no reviewed section mapping"):
            target_from_catalog_review(redirect)
        linked["raw_number"] = "–"
        with self.assertRaisesRegex(ValueError, "verified event number"):
            target_from_catalog_review(linked)

    def test_pre_2011_canonical_redirects_require_exact_reviewed_event_and_date(self) -> None:
        reviewed = [
            (2005, 64, "The Ultimate Fighter 2 Finale", "2005-11-05", "The Ultimate Fighter 2", "The Ultimate Fighter 2 Finale"),
            (2005, 57, "The Ultimate Fighter 1 Finale", "2005-04-09", "The Ultimate Fighter 1", "The Ultimate Fighter: Team Couture vs. Team Liddell Finale"),
            (2006, 80, "The Ultimate Fighter 4 Finale", "2006-11-11", "The Ultimate Fighter 4", "The Ultimate Fighter 4 Finale"),
            (2006, 72, "The Ultimate Fighter 3 Finale", "2006-06-24", "The Ultimate Fighter 3", "The Ultimate Fighter 3 Finale"),
            (2007, 101, "The Ultimate Fighter: Team Hughes vs. Team Serra Finale", "2007-12-08", "The Ultimate Fighter: Team Hughes vs. Team Serra", "The Ultimate Fighter 6 Finale"),
            (2007, 93, "The Ultimate Fighter 5 Finale", "2007-06-23", "The Ultimate Fighter 5", "The Ultimate Fighter 5 Finale"),
            (2007, 87, "UFC Fight Night: Stevenson vs. Guillard", "2007-04-05", "2007 in UFC", "UFC Fight Night: Stevenson vs. Guillard"),
            (2008, 121, "The Ultimate Fighter: Team Nogueira vs. Team Mir Finale", "2008-12-13", "The Ultimate Fighter: Team Nogueira vs. Team Mir", "The Ultimate Fighter 8 Finale"),
            (2009, 141, "The Ultimate Fighter: Heavyweights Finale", "2009-12-05", "The Ultimate Fighter: Heavyweights", "The Ultimate Fighter 10 Finale"),
            (2009, 132, "The Ultimate Fighter: United States vs. United Kingdom Finale", "2009-06-20", "The Ultimate Fighter: United States vs. United Kingdom", "The Ultimate Fighter 9 Finale"),
            (2010, 165, "The Ultimate Fighter: Team GSP vs. Team Koscheck Finale", "2010-12-04", "The Ultimate Fighter: Team GSP vs. Team Koscheck", "The Ultimate Fighter 12 Finale"),
            (2010, 154, "The Ultimate Fighter: Team Liddell vs. Team Ortiz Finale", "2010-06-19", "The Ultimate Fighter: Team Liddell vs. Team Ortiz", "The Ultimate Fighter 11 Finale"),
        ]
        for year, number, label, event_date, source_title, heading in reviewed:
            row = {
                "year": year,
                "raw_number": str(number),
                "reason": "canonical_not_event_page",
                "raw_event": label,
                "raw_date": event_date,
            }
            with self.subTest(year=year, event=number):
                target = target_from_catalog_review(row)
                self.assertEqual(target.source_page_title, source_title)
                self.assertEqual(target.target_heading, heading)
                self.assertEqual(target.expected_date, event_date)
                with self.assertRaisesRegex(ValueError, "no reviewed section mapping"):
                    target_from_catalog_review({**row, "raw_date": f"{year}-01-01"})
                with self.assertRaisesRegex(ValueError, "no reviewed section mapping"):
                    target_from_catalog_review({**row, "raw_event": "Unreviewed finale"})

        reviewed_bridge = {
            "year": 2008,
            "raw_number": "111",
            "reason": "canonical_not_event_page",
            "raw_event": "The Ultimate Fighter: Team Rampage vs. Team Forrest Finale",
            "raw_date": "2008-06-21",
        }
        self.assertEqual(
            target_from_catalog_review(reviewed_bridge).target_heading,
            "The Ultimate Fighter 7 Finale",
        )

    def test_reviewed_2008_background_bridge_requires_exact_results_boundary(self) -> None:
        payload = _payload("June 21, 2008")
        payload["id"] = 14625011
        payload["title"] = "The Ultimate Fighter: Team Rampage vs. Team Forrest"
        payload["source"] = payload["source"].replace(
            "The Ultimate Fighter 16 Finale", "The Ultimate Fighter 7 Finale"
        ).replace(
            "===Background===\n===Results===", "==Background==\nBackground text.\n==Results=="
        )
        options = {
            "source_page_title": payload["title"],
            "target_heading": "The Ultimate Fighter 7 Finale",
            "expected_date": "2008-06-21",
            "expected_page_id": payload["id"],
        }
        self.assertEqual(len(parse_embedded_event(payload, **options).bouts), 1)
        altered = dict(payload, source=payload["source"].replace(
            "Background text.\n==Results==", "Background text.\n==Other card==\n==Results=="
        ))
        with self.assertRaisesRegex(ValueError, "not followed by Results"):
            parse_embedded_event(altered, **options)
        altered = dict(payload, source=payload["source"].replace(
            "Background text.\n==Results==", "Background text.\n" + WIN + "==Results=="
        ))
        with self.assertRaisesRegex(ValueError, "another event or bout"):
            parse_embedded_event(altered, **options)


if __name__ == "__main__":
    unittest.main()
