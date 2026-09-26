"""CLI schema upgrades require an explicit, verified pre-migration backup."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.cli import main


OLD_MIGRATIONS = (
    "001_initial.sql",
    "002_provider_status.sql",
    "003_fighter_external_ids.sql",
    "004_paper_bets.sql",
)


class ExplicitMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "ufc.sqlite"

    def _old_database(self) -> None:
        migrations = files("ufc_odds_model").joinpath("sql/migrations")
        with closing(db.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in OLD_MIGRATIONS:
                connection.executescript(migrations.joinpath(name).read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
                    (name,),
                )
            connection.execute("INSERT INTO fighters(fighter_id, canonical_name) VALUES ('fa', 'Fighter Alpha')")
            connection.commit()

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(["--db", str(self.database), *arguments])
        return status, output.getvalue(), errors.getvalue()

    def _applied(self, database: Path | None = None) -> tuple[str, ...]:
        with sqlite3.connect(database or self.database) as connection:
            return tuple(row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ))

    def test_normal_commands_and_init_db_refuse_pending_migrations(self) -> None:
        self._old_database()
        original = self.database.read_bytes()
        for command in ("list-events", "seed-demo", "init-db"):
            status, _, errors = self._run(command)
            self.assertEqual(status, 1, command)
            self.assertIn("migrate --backup", errors)
            self.assertEqual(self._applied(), OLD_MIGRATIONS)
            self.assertEqual(self.database.read_bytes(), original)

    def test_verified_backup_precedes_upgrade_and_preserves_old_data(self) -> None:
        self._old_database()
        backup = self.root / "backups" / "before-upgrade.sqlite"
        status, output, errors = self._run("migrate", "--backup", str(backup))
        self.assertEqual(status, 0, errors)
        self.assertTrue(backup.is_file())
        self.assertIn(f"Verified pre-migration backup: {backup}", output)
        expected_count = len(list(
            files("ufc_odds_model").joinpath("sql/migrations").iterdir()
        ))
        self.assertIn(f"Applied {expected_count - len(OLD_MIGRATIONS)} migration(s)", output)
        self.assertEqual(self._applied(backup), OLD_MIGRATIONS)
        with closing(db.connect(self.database)) as connection:
            db.require_current_schema(connection)
            self.assertEqual(connection.execute(
                "SELECT canonical_name FROM fighters WHERE fighter_id = 'fa'"
            ).fetchone()[0], "Fighter Alpha")
        self.assertEqual(len(self._applied()), expected_count)
        self.assertEqual(self._run("list-events")[0], 0)

    def test_backup_creation_failure_leaves_old_schema_intact(self) -> None:
        self._old_database()
        backup = self.root / "existing.sqlite"
        backup.write_bytes(b"do not replace")
        original = self.database.read_bytes()
        status, _, errors = self._run("migrate", "--backup", str(backup))
        self.assertEqual(status, 1)
        self.assertIn("Destination already exists", errors)
        self.assertEqual(backup.read_bytes(), b"do not replace")
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(self._applied(), OLD_MIGRATIONS)

    def test_backup_verification_failure_blocks_sql(self) -> None:
        self._old_database()
        backup = self.root / "pre-migration.sqlite"
        with patch("ufc_odds_model.cli.verify_backup", side_effect=RuntimeError("verification failed")):
            status, _, errors = self._run("migrate", "--backup", str(backup))
        self.assertEqual(status, 1)
        self.assertIn("verification failed", errors)
        self.assertTrue(backup.is_file())
        self.assertEqual(self._applied(), OLD_MIGRATIONS)

    def test_new_database_needs_explicit_initialization(self) -> None:
        self.assertEqual(self._run("list-events")[0], 1)
        self.assertFalse(self.database.exists())
        self.assertEqual(self._run("init-db")[0], 0)
        with closing(db.connect(self.database)) as connection:
            db.require_current_schema(connection)
        backup = self.root / "unused.sqlite"
        status, output, _ = self._run("migrate", "--backup", str(backup))
        self.assertEqual(status, 0)
        self.assertIn("already current", output)
        self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
