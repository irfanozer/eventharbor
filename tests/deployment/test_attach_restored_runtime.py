"""Execute the embedded attach verifier with local filesystem and psql mocks."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/azure-economy/attach-restored-runtime.ps1"
SOURCE = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
MATCH = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", SOURCE, re.DOTALL)
assert MATCH
HOST_CODE = compile(MATCH.group(1), "attach-restored-runtime", "exec")


class AttachRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="attach-runtime-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for project in ("eventharbor", "pulseexchange"):
            (self.root / project).mkdir()
        self.values = {
            "ECONOMY_PROJECT": "eventharbor",
            "ECONOMY_PGHOST": "psql-demos-economy-example.postgres.database.azure.com",
            "ECONOMY_APP_PASSWORD": "private:@/$#'\\ password",
            "ECONOMY_PUBLIC_HOSTNAME": "eventharbor.example.com",
            "ECONOMY_ACME_EMAIL": "operator@example.com",
        }

    def record(self, project="eventharbor"):
        tables = (("endpoints", "events", "deliveries", "delivery_attempts") if project == "eventharbor"
                  else ("market_commands", "orders", "trades", "market_events", "runtime_heartbeats"))
        return {
            "role": project + "_app", "database": project, "owner": project + "_app",
            "login": True, "superuser": False, "create_database": False, "create_role": False,
            "replication": False, "bypass_rls": False, "memberships": 0,
            "other_connect": False, "tls": True, "versions": ["restored_revision"],
            "table_rows": {table: 3 for table in tables},
        }

    def run_host(self, record=None, returncode=0, raw=None, error=None):
        output = io.StringIO()
        real_path = Path
        stdout = json.dumps(self.record(self.values["ECONOMY_PROJECT"]) if record is None else record) if raw is None else raw
        with (
            patch.dict(os.environ, self.values, clear=True),
            patch("pathlib.Path", side_effect=lambda value: self.root if value == "/etc" else real_path(value)),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], returncode, stdout, "SECRET_DIAGNOSTIC"), side_effect=error) as database,
            patch.object(os, "geteuid", return_value=0, create=True),
            patch.object(os, "fchown", create=True),
            patch.object(os, "fchmod", create=True),
            patch.object(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), create=True),
            contextlib.redirect_stdout(output),
        ):
            try:
                exec(HOST_CODE, {})
                failure = None
            except SystemExit as problem:
                failure = str(problem)
        return database, output.getvalue(), failure

    def test_both_restored_projects_use_only_read_queries_and_safe_urls(self):
        for project in ("eventharbor", "pulseexchange"):
            with self.subTest(project=project):
                self.values["ECONOMY_PROJECT"] = project
                database, output, failure = self.run_host()
                self.assertIsNone(failure)
                self.assertEqual(database.call_count, 1)
                request = database.call_args.kwargs
                self.assertEqual(request["env"]["PGUSER"], project + "_app")
                self.assertEqual(request["env"]["PGDATABASE"], project)
                self.assertEqual(request["env"]["PGSSLMODE"], "verify-full")
                self.assertIn("default_transaction_read_only=on", request["env"]["PGOPTIONS"])
                self.assertIn("BEGIN READ ONLY", request["input"])
                self.assertNotRegex(request["input"], r"\b(?:CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|GRANT|REVOKE)\b")
                self.assertNotIn(self.values["ECONOMY_APP_PASSWORD"], request["input"])
                self.assertNotIn(self.values["ECONOMY_APP_PASSWORD"], str(database.call_args.args))
                content = (self.root / project / "runtime.env").read_text()
                self.assertIn(quote(self.values["ECONOMY_APP_PASSWORD"], safe=""), content)
                self.assertIn(f"/{project}?ssl=verify-full", content)
                self.assertNotIn("SECRET_DIAGNOSTIC", output)

    def test_wrong_identity_privilege_or_isolation_fails(self):
        unsafe = {
            "role": "portfolio_admin", "database": "postgres", "owner": "portfolio_admin",
            "superuser": True, "create_database": True, "create_role": True,
            "replication": True, "bypass_rls": True, "memberships": 1,
            "other_connect": True, "tls": False, "login": False,
        }
        for field, value in unsafe.items():
            with self.subTest(field=field):
                record = self.record()
                record[field] = value
                _, _, failure = self.run_host(record=record)
                self.assertIsNotNone(failure)
                self.assertFalse((self.root / "eventharbor/runtime.env").exists())

    def test_missing_or_empty_migration_and_application_data_fails(self):
        for field, value in (("versions", []), ("versions", ["a", "b"]), ("versions", [""]),
                             ("table_rows", {}), ("table_rows", {name: 0 for name in self.record()["table_rows"]})):
            with self.subTest(field=field, value=value):
                record = self.record()
                record[field] = value
                _, _, failure = self.run_host(record=record)
                self.assertIsNotNone(failure)
                self.assertFalse((self.root / "eventharbor/runtime.env").exists())

    def test_database_failures_never_write_or_print_diagnostics(self):
        for kwargs in ({"returncode": 1}, {"raw": "not json SECRET_DIAGNOSTIC"},
                       {"error": subprocess.TimeoutExpired("psql", 60)}, {"error": FileNotFoundError("psql")}):
            with self.subTest(kwargs=kwargs):
                _, output, failure = self.run_host(**kwargs)
                self.assertIsNotNone(failure)
                self.assertNotIn("SECRET_DIAGNOSTIC", output + failure)
                self.assertFalse((self.root / "eventharbor/runtime.env").exists())

    def test_existing_file_is_not_overwritten_and_no_database_request_occurs(self):
        target = self.root / "eventharbor/runtime.env"
        target.write_text("retained")
        database, _, failure = self.run_host()
        self.assertIn("already exists", failure)
        database.assert_not_called()
        self.assertEqual(target.read_text(), "retained")

    def test_invalid_host_or_environment_stops_before_database_request(self):
        for key, value in (("ECONOMY_PGHOST", "localhost"), ("ECONOMY_ACME_EMAIL", "bad$@example.com"),
                           ("ECONOMY_PUBLIC_HOSTNAME", "example.com\nEVIL=1")):
            with self.subTest(key=key):
                previous = self.values[key]
                self.values[key] = value
                database, _, failure = self.run_host()
                self.assertIsNotNone(failure)
                database.assert_not_called()
                self.values[key] = previous

    def test_root_permissions_and_protected_parameters_are_explicit(self):
        self.assertIn("os.fchmod(output.fileno(), 0o600)", SOURCE)
        self.assertIn("os.fchown(output.fileno(), 0, 0)", SOURCE)
        self.assertIn("os.O_EXCL | os.O_NOFOLLOW", SOURCE)
        self.assertIn("protectedParameters = @(@{ name = 'ECONOMY_APP_PASSWORD'", SOURCE)
        self.assertNotIn("PostgresAdministratorPassword", SOURCE)
        self.assertNotIn("CREATE ROLE", SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
