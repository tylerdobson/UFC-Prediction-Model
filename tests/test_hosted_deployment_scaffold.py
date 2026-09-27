"""Guard the authenticated host overlay against accidental public bypasses."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _brace_block(text: str, opening: str) -> str:
    start = text.index(opening) + len(opening)
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError(f"unclosed Caddy block: {opening}")


class HostedDeploymentScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        self.overlay = (ROOT / "deploy/compose.hosted.yaml").read_text(encoding="utf-8")

    def test_every_dashboard_path_passes_the_auth_check(self) -> None:
        self.assertIn("app.tylerjamesdobson.com {", self.caddy)
        self.assertNotIn("tylerjamesdobson.com,", self.caddy)
        handles = re.findall(r"^\s+handle(?:\s+([^\{\n]+))?\s*\{", self.caddy, re.M)
        self.assertEqual([item.strip() for item in handles], ["/oauth2/*", ""])
        login = _brace_block(self.caddy, "handle /oauth2/* {")
        guarded = _brace_block(self.caddy, "    handle {")
        self.assertIn("reverse_proxy oauth2-proxy:4180", login)
        self.assertNotIn("dashboard:8501", login)
        self.assertIn("forward_auth oauth2-proxy:4180", guarded)
        self.assertIn("uri /oauth2/auth", guarded)
        self.assertIn("@unauthorized status 401", guarded)
        self.assertIn("reverse_proxy dashboard:8501", guarded)
        self.assertLess(guarded.index("forward_auth"), guarded.index("reverse_proxy dashboard"))
        self.assertEqual(self.caddy.count("reverse_proxy dashboard:8501"), 1)
        self.assertNotIn("handle /_stcore", self.caddy)
        self.assertNotIn("handle_path", self.caddy)

    def test_only_caddy_has_public_ports_and_auth_is_single_user(self) -> None:
        base = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:8501:8501"', base)
        self.assertIn('"0.0.0.0:80:80"', self.overlay)
        self.assertIn('"0.0.0.0:443:443"', self.overlay)
        oauth = self.overlay.split("  oauth2-proxy:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        self.assertNotIn("\n    ports:", oauth)
        self.assertIn("--github-user=${UFC_GITHUB_USER:?", oauth)
        self.assertIn("--trusted-proxy-ip=172.30.62.2/32", oauth)
        self.assertIn("ipv4_address: 172.30.62.2", self.overlay)
        self.assertIn("--cookie-secure=true", oauth)
        self.assertIn("--client-secret-file=", oauth)
        self.assertIn("--cookie-secret-file=", oauth)
        self.assertIn("--redirect-url=https://app.tylerjamesdobson.com/oauth2/callback", oauth)
        self.assertNotIn("ODDS_API_KEY", self.overlay)
        self.assertNotIn("skip-auth-route", oauth)
        self.assertNotIn("--trusted-ip=", oauth)

    def test_runtime_secret_files_are_ignored_and_template_has_no_secrets(self) -> None:
        ignored = (ROOT / "deploy/.gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("hosted.env", ignored)
        self.assertIn("secrets/", ignored)
        docker_ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("deploy", docker_ignored)
        template = (ROOT / "deploy/hosted.env.example").read_text(encoding="utf-8")
        self.assertIn("UFC_GITHUB_OAUTH_CLIENT_ID=REPLACE_", template)
        self.assertNotIn("CLIENT_SECRET=", template)
        self.assertNotIn("ODDS_API_KEY=", template)


if __name__ == "__main__":
    unittest.main()
