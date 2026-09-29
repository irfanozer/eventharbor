"""Offline security and compatibility checks for the host-only runtime."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("runtime_validation", ROOT / "validate_env.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

class EnvironmentValidation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.file = Path(self.directory.name) / "input.env"
        self.values = {
            "PUBLIC_HOSTNAME": "demo.eastus.cloudapp.azure.com",
            "ACME_EMAIL": "operator@example.com",
            "CADDY_IMAGE": "caddy:2.11.4-alpine@sha256:" + "a" * 64,
            validator.PREFIX + "_DATABASE_URL": (
                "postgresql+asyncpg://app:password%24value@server.postgres.database.azure.com/"
                + validator.PROJECT + "?ssl=verify-full"
            ),
        }

    def tearDown(self):
        self.directory.cleanup()

    def read(self, suffix=""):
        self.file.write_text(
            "\n".join(f"{key}={value}" for key, value in self.values.items()) + "\n" + suffix,
            encoding="utf-8",
        )
        # Linux file ownership is enforced by the real CLI; only parser tests skip it.
        return validator.read_values(self.file, check_permissions=False)

    def test_accepts_tls_private_server_url_and_azure_pre_cutover_hostname(self):
        self.assertEqual(self.read()["PUBLIC_HOSTNAME"], self.values["PUBLIC_HOSTNAME"])

    def test_rejects_wrong_database_and_non_azure_host(self):
        key = validator.PREFIX + "_DATABASE_URL"
        for url in (
            "postgresql+asyncpg://app:password@localhost/" + validator.PROJECT + "?ssl=require",
            "postgresql+asyncpg://app:password@server.postgres.database.azure.com/another?ssl=require",
        ):
            with self.subTest(url=url):
                self.values[key] = url
                with self.assertRaises(ValueError):
                    self.read()

    def test_rejects_disabled_tls_and_duplicate_ssl_parameters(self):
        key = validator.PREFIX + "_DATABASE_URL"
        original = self.values[key]
        for query in ("ssl=disable", "ssl=require&ssl=disable", "sslmode=require"):
            with self.subTest(query=query):
                self.values[key] = original.split("?")[0] + "?" + query
                with self.assertRaises(ValueError):
                    self.read()

    def test_pulseexchange_requires_explicit_tls(self):
        if validator.PROJECT != "pulseexchange":
            self.skipTest("EventHarbor independently forces verify-full in Compose")
        key = validator.PREFIX + "_DATABASE_URL"
        self.values[key] = self.values[key].split("?")[0]
        with self.assertRaises(ValueError):
            self.read()

    def test_rejects_compose_interpolation_and_shell_syntax(self):
        for value in ("\u0024{SECRET}", "'quoted'", "\u0060command\u0060", "value with spaces"):
            with self.subTest(value=value):
                self.values["ACME_EMAIL"] = value
                with self.assertRaises(ValueError):
                    self.read()

    def test_rejects_environment_guard_override_and_duplicates(self):
        for suffix in ("COMPOSE_FILE=other.yaml\n", "PUBLIC_HOSTNAME=other.example.com\n",
                       validator.PREFIX + "_ENVIRONMENT=local\n"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    self.read(suffix)

    def test_requires_digest_pinning(self):
        validator.validate_image("ghcr.io/example/app-backend@sha256:" + "b" * 64)
        for image in ("ghcr.io/example/app:latest", "ghcr.io/example/app:abcdef",
                      "docker.io/example/app@sha256:" + "b" * 64):
            with self.subTest(image=image):
                with self.assertRaises(ValueError):
                    validator.validate_image(image)

    def test_does_not_accept_caddy_from_untrusted_repository(self):
        self.values["CADDY_IMAGE"] = "ghcr.io/other/caddy@sha256:" + "a" * 64
        with self.assertRaises(ValueError):
            self.read()

    def test_proxy_only_trusts_caddy_and_overwrites_client_identity(self):
        nginx = (ROOT / "nginx.conf.template").read_text()
        caddy = (ROOT / "Caddyfile").read_text()
        self.assertIn("set_real_ip_from 172.29.240.2;", nginx)
        self.assertNotIn("set_real_ip_from 0.0.0.0/0", nginx)
        self.assertIn("header_up X-Forwarded-For {remote_host}", caddy)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", nginx)

if __name__ == "__main__":
    unittest.main()
