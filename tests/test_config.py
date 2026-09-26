"""Local secret loading is explicit, private, and never executes file text."""

from __future__ import annotations

import os
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.cli import main
from ufc_odds_model.config import provider_key


class ProviderKeyTests(unittest.TestCase):
    def test_reads_private_dotenv_and_exported_value_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "# Local provider keys\nODDS_API_KEY='file-key'\n"
                "SPORTRADAR_API_KEY=other-file-key\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(provider_key("ODDS_API_KEY", path), "file-key")
                self.assertEqual(os.environ["ODDS_API_KEY"], "file-key")
            with patch.dict(os.environ, {"ODDS_API_KEY": "exported-key"}, clear=True):
                self.assertEqual(provider_key("ODDS_API_KEY", path), "exported-key")

    def test_missing_key_and_unrelated_text_do_not_run_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".env"
            sentinel = root / "should-not-exist"
            path.write_text(
                f"UNRELATED=$(touch {sentinel})\nODDS_API_KEY=\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(provider_key("ODDS_API_KEY", path), "")
            self.assertFalse(sentinel.exists())

    def test_rejects_public_or_ambiguous_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".env"
            path.write_text("ODDS_API_KEY=secret\n", encoding="utf-8")
            path.chmod(0o644)
            with patch.dict(os.environ, {}, clear=True):
                if os.name == "posix":
                    with self.assertRaisesRegex(ValueError, "private") as raised:
                        provider_key("ODDS_API_KEY", path)
                    self.assertNotIn("secret", str(raised.exception))
            path.chmod(0o600)
            path.write_text("ODDS_API_KEY=one\nODDS_API_KEY=two\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    provider_key("ODDS_API_KEY", path)
            alias = root / "link.env"
            alias.symlink_to(path)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    provider_key("ODDS_API_KEY", alias)

    def test_cli_uses_private_dotenv_without_sourcing_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "event.sqlite"
            key_file = root / ".env"
            key_file.write_text("ODDS_API_KEY=test-private-key\n", encoding="utf-8")
            key_file.chmod(0o600)
            former_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--db", str(db_path), "init-db"]), 0)
                    output = io.StringIO()
                    with patch("ufc_odds_model.cli.import_live_odds",
                               return_value={"matched_quotes": 0}) as fetch:
                        with redirect_stdout(output):
                            self.assertEqual(main(["--db", str(db_path), "import-odds"]), 0)
                    self.assertEqual(fetch.call_args.args[1], "test-private-key")
                    self.assertNotIn("test-private-key", output.getvalue())
            finally:
                os.chdir(former_cwd)


if __name__ == "__main__":
    unittest.main()
