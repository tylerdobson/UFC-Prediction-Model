import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import local_mysql


class LocalMySQLTests(unittest.TestCase):
    def test_credentials_are_private_and_cannot_replace_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            local_mysql.private_json(path, {"password": "fixture-only"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(local_mysql.read_private(path), {"password": "fixture-only"})
            with self.assertRaises(FileExistsError):
                local_mysql.private_json(path, {"password": "replacement"})
            self.assertEqual(json.loads(path.read_text())["password"], "fixture-only")

    def test_credentials_refuse_symlinks_and_broad_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.json"
            target.write_text("{}")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                local_mysql.private_json(link, {})
            with self.assertRaises(ValueError):
                local_mysql.read_private(link)
            target.chmod(0o644)
            with self.assertRaises(ValueError):
                local_mysql.read_private(target)

    def test_setup_preserves_existing_data_and_never_initializes_over_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "mysql"
            (state / "data").mkdir(parents=True)
            with patch.object(local_mysql.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "preserved"):
                    local_mysql.setup(state, Path(directory) / "env.json", 3307)
                run.assert_not_called()

    def test_repeated_setup_uses_saved_configuration_without_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "mysql"
            config = Path(directory) / "env.json"
            local_mysql.private_json(state / "admin.json", {"password": "fixture", "port": 3307, "config": str(config)})
            local_mysql.private_json(config, {"environment": {}})
            with patch.object(local_mysql, "start") as start, patch.object(local_mysql.subprocess, "run") as run:
                self.assertTrue(local_mysql.setup(state, config, 3307)["reused"])
                start.assert_called_once_with(state)
                run.assert_not_called()
            with self.assertRaisesRegex(ValueError, "different configuration"):
                local_mysql.setup(state, config, 3308)

    def test_native_server_is_bound_to_project_socket_and_loopback(self):
        with patch.object(local_mysql, "binary_dir", return_value=Path("/installed/mysql/bin")):
            args = local_mysql.server_arguments(Path("/private/project"), 3307)
            self.assertIn("--no-defaults", args)
            self.assertIn("--port=3307", args)
            self.assertIn("--bind-address=127.0.0.1", args)
            bootstrap = local_mysql.server_arguments(Path("/private/project"), 3307, bootstrap=True)
            self.assertIn("--skip-networking", bootstrap)
            self.assertFalse(any(x.startswith("--port=") for x in bootstrap))

    def test_shutdown_refuses_a_different_server_data_directory(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("/unrelated/mysql/data",)
        with patch.object(local_mysql, "client", return_value=connection):
            with self.assertRaisesRegex(ValueError, "Refusing"):
                local_mysql.stop_server(Path("/project/mysql"), {"password": "fixture"})
        self.assertEqual([call.args[0] for call in cursor.execute.call_args_list], ["SELECT @@datadir"])


if __name__ == "__main__":
    unittest.main()
