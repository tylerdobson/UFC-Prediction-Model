"""A reviewed calendar conflict must bind one exact catalog and page revision."""

from __future__ import annotations

import unittest
from dataclasses import replace

from ufc_odds_model.wikipedia_catalog import CatalogEvent, CatalogYear
from ufc_odds_model.wikipedia_date_reconciliation import REVIEWS, reviewed_event
from ufc_odds_model.wikipedia_history import ParsedEvent


class ReviewedDateTests(unittest.TestCase):
    def test_explicit_review_preserves_both_original_dates(self) -> None:
        review = REVIEWS[0]
        entry = CatalogEvent(
            review.year, review.event_number, review.catalog_date, review.title,
            review.title, review.page_id, 987, review.catalog_revision_id,
        )
        year = CatalogYear(
            review.year, 987, review.catalog_revision_id, "2026-09-26T00:00:00Z",
            "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/deed.en",
            "https://en.wikipedia.org/wiki/2015_in_UFC", {}, (entry,), (),
        )
        page = ParsedEvent(
            review.page_id, review.title, review.title, review.page_date,
            review.page_revision_id, "2026-09-26T00:00:00Z", "CC BY-SA 4.0",
            "https://creativecommons.org/licenses/by-sa/4.0/deed.en", (),
        )
        accepted = reviewed_event(review, year, entry, page)
        self.assertEqual(accepted.event_date, review.accepted_date)
        self.assertEqual(page.event_date, review.page_date)
        self.assertNotEqual(review.catalog_date, review.page_date)
        for changed in (
            (replace(year, revision_id=year.revision_id + 1), entry, page),
            (year, replace(entry, event_date=review.page_date), page),
            (year, entry, replace(page, page_id=page.page_id + 1)),
            (year, entry, replace(page, revision_id=page.revision_id + 1)),
            (year, entry, replace(page, event_date=review.catalog_date)),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                ValueError, "Date review evidence changed"
            ):
                reviewed_event(review, *changed)


if __name__ == "__main__":
    unittest.main()
