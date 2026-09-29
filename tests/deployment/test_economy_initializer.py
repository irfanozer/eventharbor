"""Offline tests for the embedded host initializer. Never call Azure or psql."""

import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/azure-economy/initialize-runtime.ps1"
SOURCE = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
MATCH = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", SOURCE, re.DOTALL)
assert MATCH, "The embedded initializer must remain discoverable for offline checks."
HOST_CODE = compile(MATCH.group(1), "economy-initializer", "exec")


class InitializerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="economy-initializer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.values = {
            "ECONOMY_PROJECT": "eventharbor",
            "ECONOMY_PGHOST": "new-economy.postgres.database.azure.com",
            "ECONOMY_PGADMIN": "demos_admin",
            "ECONOMY_ADMIN_PASSWORD": "private-admin-fixture",
            "ECONOMY_APP_PASSWORD": "private-app-fixture",
            "ECONOMY_PUBLIC_HOSTNAME": "example.eastus.cloudapp.azure.com",
            "ECONOMY_ACME_EMAIL": "operator@example.com",
        }

    def run_host(self, returncode=0):
        real_path = Path
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.values, clear=True),
            patch("pathlib.Path", side_effect=lambda value: self.root if value == "/etc" else real_path(value)),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], returncode, "", "private diagnostic")) as database,
            # Windows has no O_NOFOLLOW; this test still checks contents and refusal
            # to overwrite. Linux keeps its actual symlink protection flag.
            patch.object(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), create=True),
            contextlib.redirect_stdout(output),
        ):
            try:
                exec(HOST_CODE, {})
                failure = None
            except SystemExit as error:
                failure = str(error)
            return database, output.getvalue(), failure

    def test_both_projects_get_distinct_roles_and_private_tls_urls(self):
        for project in ("eventharbor", "pulseexchange"):
            with self.subTest(project=project):
                self.values["ECONOMY_PROJECT"] = project
                database, output, failure = self.run_host()
                self.assertIsNone(failure)
                self.assertEqual(database.call_count, 1)
                arguments = database.call_args.kwargs
                self.assertEqual(database.call_args.args[0], ["psql", "-X", "-q"])
                self.assertEqual(arguments["env"]["PGSSLMODE"], "verify-full")
                self.assertIn(f"CREATE ROLE {project}_app", arguments["input"])
                self.assertIn("SET LOCAL createrole_self_grant = 'set'", arguments["input"])
                self.assertIn(f"REVOKE CONNECT, TEMPORARY ON DATABASE {project} FROM PUBLIC", arguments["input"])
                self.assertNotIn("private-app-fixture", arguments["input"])
                self.assertNotIn("private-admin-fixture", arguments["input"])
                result = (self.root / project / "runtime.env").read_text()
                self.assertIn(f"{project.upper()}_DATABASE_URL=postgresql+asyncpg://{project}_app:", result)
                self.assertIn(f"/{project}?ssl=verify-full", result)
                self.assertNotIn("private-admin-fixture", result)
                self.assertNotIn("private-app-fixture", output)

    def test_existing_configuration_is_not_overwritten(self):
        folder = self.root / "eventharbor"
        folder.mkdir()
        saved = folder / "runtime.env"
        saved.write_text("existing-private-configuration")
        database, _, failure = self.run_host()
        self.assertIn("already exists", failure)
        database.assert_not_called()
        self.assertEqual(saved.read_text(), "existing-private-configuration")

    def test_failed_database_operation_does_not_write_runtime_file(self):
        database, output, failure = self.run_host(returncode=1)
        self.assertEqual(database.call_count, 1)
        self.assertIn("Database initialization failed", failure)
        self.assertNotIn("private diagnostic", failure + output)
        self.assertFalse((self.root / "eventharbor/runtime.env").exists())

    def test_wrong_project_and_host_stop_before_database_request(self):
        for key, value in (("ECONOMY_PROJECT", "other-project"), ("ECONOMY_PGHOST", "localhost")):
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                database, _, failure = self.run_host()
                self.assertIsNotNone(failure)
                database.assert_not_called()
                self.values[key] = original

    def test_passwords_are_only_protected_run_command_parameters(self):
        self.assertIn("protectedParameters = @(", SOURCE)
        self.assertNotIn("--protected-parameters", SOURCE)
        self.assertIn("$server.tags.costProfile -cne 'economy'", SOURCE)
        self.assertIn("$vm.tags.application -cne $Project", SOURCE)
        self.assertIn("$view.Value.exitCode -ne 0", SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
