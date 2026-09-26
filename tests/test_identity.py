"""Reviewed cross-provider aliases keep prior fight history attached to one fighter."""

from __future__ import annotations

import sqlite3
import unittest

from ufc_odds_model import db


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        db.init_db(self.connection)
        db.upsert_fighter(self.connection, "ufcstats:abc", "Max Holloway", "ufcstats", "abc")

    def tearDown(self) -> None:
        self.connection.close()

    def test_reviewed_alias_resolves_without_overwriting_primary_identity(self):
        db.link_external_fighter(self.connection, "sportradar", "sr:competitor:42", "ufcstats:abc")
        fighter_id = db.resolve_fighter_id(self.connection, "sportradar", "sr:competitor:42")
        self.assertEqual(fighter_id, "ufcstats:abc")
        db.upsert_fighter(
            self.connection, fighter_id, "Holloway, Max", "sportradar", "sr:competitor:42"
        )
        row = self.connection.execute("SELECT * FROM fighters WHERE fighter_id = ?", (fighter_id,)).fetchone()
        self.assertEqual(row["source"], "ufcstats")
        self.assertEqual(row["source_fighter_id"], "abc")
        self.assertEqual(row["canonical_name"], "Max Holloway")
        self.assertEqual(db.resolve_fighter_id(self.connection, "ufcstats", "abc"), fighter_id)

    def test_conflicting_alias_cannot_silently_merge_two_fighters(self):
        db.upsert_fighter(self.connection, "other", "Another Fighter", "sportradar", "sr:competitor:42")
        with self.assertRaisesRegex(ValueError, "already belongs"):
            db.link_external_fighter(self.connection, "sportradar", "sr:competitor:42", "ufcstats:abc")


if __name__ == "__main__":
    unittest.main()
