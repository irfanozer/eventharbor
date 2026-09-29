"""Offline tests for the embedded host initializer. Never call Azure or psql."""

import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import shutil
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
        for project in ('eventharbor', 'pulseexchange'):
            (self.root / project).mkdir()
        self.values = {
            "ECONOMY_PROJECT": "eventharbor",
            "ECONOMY_PGHOST": "psql-demos-economy-56sjj5b3bl7ro.postgres.database.azure.com",
            "ECONOMY_PGADMIN": "portfolio_admin",
            "ECONOMY_ADMIN_PASSWORD": "private-admin-fixture",
            "ECONOMY_APP_PASSWORD": "private-app-fixture-12345678901234567890",
            "ECONOMY_PUBLIC_HOSTNAME": "example.eastus.cloudapp.azure.com",
            "ECONOMY_ACME_EMAIL": "operator@example.com",
        }

    def run_host(self, returncode=0, stderr='', owner_uid=0, error=None):
        real_path = Path
        real_stat = Path.stat
        output = io.StringIO()
        def fixture_stat(path, *args, **kwargs):
            metadata = real_stat(path, *args, **kwargs)
            if path in (self.root / 'eventharbor', self.root / 'pulseexchange'):
                values = list(metadata)
                values[4] = owner_uid
                return os.stat_result(values)
            return metadata
        with (
            patch.dict(os.environ, self.values, clear=True),
            patch("pathlib.Path", side_effect=lambda value: self.root if value == "/etc" else real_path(value)),
            patch.object(real_path, 'stat', autospec=True, side_effect=fixture_stat),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], returncode, "", stderr), side_effect=error) as database,
            patch.object(os, 'geteuid', return_value=0, create=True),
            patch.object(os, 'fchown', create=True),
            patch.object(os, 'fchmod', create=True),
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
                self.assertEqual(database.call_args.args[0], ["psql", "-X", "-q", '--no-password', '--set', 'VERBOSITY=sqlstate'])
                self.assertEqual(arguments["env"]["PGSSLMODE"], "verify-full")
                self.assertEqual(arguments['env']['PGDATABASE'], project)
                self.assertEqual(arguments['env']['ECONOMY_APP_PASSWORD'], self.values['ECONOMY_APP_PASSWORD'])
                self.assertIn(f"CREATE ROLE {project}_app", arguments["input"])
                self.assertIn("SET LOCAL createrole_self_grant = 'set'", arguments["input"])
                self.assertIn(f"REVOKE CONNECT, TEMPORARY ON DATABASE {project} FROM PUBLIC", arguments["input"])
                self.assertIn('NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12', arguments['input'])
                self.assertIn('REVOKE ALL ON SCHEMA public FROM PUBLIC', arguments['input'])
                self.assertLess(arguments['input'].index('DO $guard$'), arguments['input'].index('CREATE ROLE'))
                self.assertLess(arguments['input'].index('GRANT CREATE ON DATABASE'), arguments['input'].index('ALTER SCHEMA public OWNER'))
                self.assertLess(arguments['input'].index('ALTER SCHEMA public OWNER'), arguments['input'].index('ALTER DATABASE'))
                self.assertLess(arguments['input'].index('SET LOCAL ROLE'), arguments['input'].index('REVOKE CONNECT'))
                for guard in ('current_database()', "current_user<>'portfolio_admin'", 'pg_get_userbyid(datdba)', 'pg_roles', 'pg_namespace', 'pg_class', 'pg_type', 'pg_proc', 'pg_extension', 'pg_largeobject_metadata', 'pg_event_trigger', 'pg_stat_activity', "pg_has_role(current_user,nspowner,'USAGE')", 'azure_pg_admin', '170000 AND 179999'):
                    self.assertIn(guard, arguments['input'])
                for forbidden in ('DROP ', 'TRUNCATE ', 'DELETE ', 'UPDATE ', 'CREATE DATABASE'):
                    self.assertNotIn(forbidden, arguments['input'])
                self.assertNotIn("private-app-fixture", arguments["input"])
                self.assertNotIn("private-admin-fixture", arguments["input"])
                result = (self.root / project / "runtime.env").read_text()
                self.assertIn(f"{project.upper()}_DATABASE_URL=postgresql+asyncpg://{project}_app:", result)
                self.assertIn(f"/{project}?ssl=verify-full", result)
                self.assertNotIn("private-admin-fixture", result)
                self.assertNotIn("private-app-fixture", output)

    def test_existing_configuration_is_not_overwritten(self):
        folder = self.root / "eventharbor"
        saved = folder / "runtime.env"
        saved.write_text("existing-private-configuration")
        database, _, failure = self.run_host()
        self.assertIn("already exists", failure)
        database.assert_not_called()
        self.assertEqual(saved.read_text(), "existing-private-configuration")

    def test_failed_database_operation_does_not_write_runtime_file(self):
        database, output, failure = self.run_host(returncode=1, stderr='private diagnostic')
        self.assertEqual(database.call_count, 1)
        self.assertIn("database initialization failed", failure)
        self.assertNotIn("private diagnostic", failure + output)
        self.assertFalse((self.root / "eventharbor/runtime.env").exists())

    def test_wrong_project_and_host_stop_before_database_request(self):
        for key, value in (("ECONOMY_PROJECT", "other-project"), ("ECONOMY_PGHOST", "localhost"), ('ECONOMY_PGADMIN', 'other_admin'), ('ECONOMY_APP_PASSWORD', 'short'), ('ECONOMY_APP_PASSWORD', 'x'*32+'\n'), ('ECONOMY_PUBLIC_HOSTNAME','bad.example#'), ('ECONOMY_ACME_EMAIL','bad $email@example.com')):
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                database, _, failure = self.run_host()
                self.assertIsNotNone(failure)
                database.assert_not_called()
                self.values[key] = original

    def test_warning_or_timeout_never_writes_configuration_or_exposes_diagnostics(self):
        for kwargs in ({'stderr': 'PRIVATE_WARNING'}, {'error': subprocess.TimeoutExpired('psql PRIVATE_COMMAND', 60)}):
            with self.subTest(kwargs=kwargs):
                database, output, failure = self.run_host(**kwargs)
                self.assertEqual(database.call_count, 1)
                self.assertIsNotNone(failure)
                self.assertNotIn('PRIVATE_', output + failure)
                self.assertFalse((self.root / 'eventharbor/runtime.env').exists())

    def test_non_root_owned_folder_is_refused_before_database(self):
        database, _, failure = self.run_host(owner_uid=1000)
        self.assertIsNotNone(failure)
        database.assert_not_called()

    def test_passwords_are_only_protected_run_command_parameters(self):
        self.assertIn("protectedParameters = @(", SOURCE)
        self.assertNotIn("--protected-parameters", SOURCE)
        self.assertIn("$server.tags.costProfile -cne 'economy'", SOURCE)
        self.assertIn("$vm.tags.application -cne $Project", SOURCE)
        self.assertIn("$view.Value.exitCode -ne 0", SOURCE)
        self.assertIn('[SecureString]$AppPassword', SOURCE)
        self.assertIn('SecureStringToBSTR($AppPassword)', SOURCE)
        self.assertIn('GetBytes(32)', SOURCE)
        self.assertNotIn('--password', SOURCE)

    def test_postgres_location_allows_only_canonical_and_observed_azure_spelling(self):
        match = re.search(r"\$server\.location -cnotin @\(([^\n]+)\)", SOURCE)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r"'([^']+)'", match.group(1)), ['westus2', 'West US 2'])
        # Execute the same case-sensitive PowerShell expression without invoking
        # the initializer, Azure CLI, or any credential-handling code.
        pwsh = shutil.which('pwsh')
        bundled = Path(r'C:\Users\I\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe')
        if not pwsh and bundled.is_file():
            pwsh = str(bundled)
        if not pwsh:
            self.skipTest('PowerShell is unavailable; exact allowlist assertion passed.')
        expression = "$allowed=@(" + match.group(1) + "); "
        expression += "foreach($region in @('westus2','West US 2')) {if($region -cnotin $allowed){exit 1}}; "
        expression += "foreach($region in @('West US','West US 3','eastus2','WESTUS2','west us 2',' West US 2','West US 2 ')) {if($region -cin $allowed){exit 2}}"
        result = subprocess.run([pwsh, '-NoProfile', '-Command', expression], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'PostgreSQL region allowlist accepted an unrelated spelling or region.')


if __name__ == "__main__":
    unittest.main(verbosity=2)
