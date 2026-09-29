"""Offline isolation safety tests. No database, identity or Azure requests."""

import copy
from contextlib import contextmanager
import importlib.util
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[2] / "infra/azure-shared-postgres"
for module_name in ("migrate", "isolate_eventharbor"):
    spec = importlib.util.spec_from_file_location(module_name, TOOLS / (module_name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
i = sys.modules["isolate_eventharbor"]
m = i.migrate
ENV = {
    "MIGRATION_RUN_ID": "isolation-offline-20260929",
    "TARGET_ADMIN_DATABASE_URL": f"postgresql+asyncpg://fixtureadmin:fixtureonly@{m.TARGET_HOST}/eventharbor?ssl=require",
    "EVENTHARBOR_APP_PASSWORD": "fixture-event-password-not-real-123456",
    "TARGET_APP_PASSWORD": "fixture-pulse-password-not-real-123456",
    "WRITERS_FROZEN": "true",
    "BACKUP_STORAGE_ACCOUNT": "fixtureprivate", "BACKUP_CONTAINER": "private-backups",
}
INVENTORY = {
    "database": "eventharbor", "current_user": "fixtureadmin", "owner": "fixtureadmin",
    "schemas": [{"name": "public", "owner": "pg_database_owner"}], "extensions": ["plpgsql"],
    "large_objects": 0, "role_exists": False,
    "relations": [{"oid": 1, "name": "alembic_version", "kind": "r", "owner": "fixtureadmin"},
                  {"oid": 2, "name": "quoted\"name", "kind": "i", "owner": "fixtureadmin"}],
    "types": [{"oid": 3, "name": "status", "kind": "e", "category": "E", "relation": 0,
               "element": 0, "owner": "fixtureadmin"}], "routines": [],
}
MANIFEST = {"tables": [], "sequences": [], "alembic_version": ["fixture-revision"]}


class FakePg:
    writes = []
    sessions = 0
    inventory = INVENTORY

    def __init__(self, database, run_id):
        self.database, self.run_id = database, run_id

    def require_version(self):
        pass

    def json(self, query):
        if query == i.INVENTORY_SQL:
            return copy.deepcopy(self.inventory)
        if "pg_stat_activity" in query:
            return self.sessions
        raise AssertionError("Unexpected fake database query")

    def sql(self, sql, **kwargs):
        self.writes.append((sql, kwargs))

    @contextmanager
    def snapshot(self):
        yield "00000003-00000004-1"

    def manifest(self, snapshot, directory):
        return copy.deepcopy(MANIFEST)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        FakePg.writes, FakePg.sessions = [], 0
        FakePg.inventory = INVENTORY

    def test_safe_existing_inventory(self):
        i.validate_inventory(copy.deepcopy(INVENTORY), applying=True)

    def test_unexpected_inventory_shapes_are_blocked(self):
        mutations = (
            lambda x: x.update(database="pulseexchange"),
            lambda x: x.update(extensions=["plpgsql", "unsafe_extension"]),
            lambda x: x.update(large_objects=1),
            lambda x: x["schemas"].append({"name": "unreviewed", "owner": "fixtureadmin"}),
            lambda x: x["relations"][0].update(kind="f"),
            lambda x: x["relations"][0].update(owner="otheruser"),
            lambda x: x["types"][0].update(kind="d"),
            lambda x: x["routines"].append({"kind": "f", "security_definer": True}),
            lambda x: x.update(role_exists=True),
        )
        for mutate in mutations:
            inventory = copy.deepcopy(INVENTORY)
            mutate(inventory)
            with self.subTest(mutation=mutate), self.assertRaises(m.SafeError):
                i.validate_inventory(inventory, applying=True)

    def test_inspect_never_writes_or_requires_password_or_backup(self):
        with patch.object(m, "require_clients"), patch.object(m, "Pg", FakePg), patch.object(i, "backup_before_change") as backup:
            environment = {k: v for k, v in ENV.items() if "PASSWORD" not in k}
            result = i.run("Inspect", environment)
        self.assertFalse(result["changed"])
        self.assertEqual(FakePg.writes, [])
        backup.assert_not_called()

    def test_apply_requires_explicit_freeze(self):
        with patch.object(m, "require_clients"), patch.object(m, "Pg", FakePg), patch.object(i, "backup_before_change") as backup:
            with self.assertRaises(m.SafeError):
                i.run("Apply", {**ENV, "WRITERS_FROZEN": "false"})
        backup.assert_not_called()
        self.assertEqual(FakePg.writes, [])

    def test_active_sessions_block_before_backup_and_writes(self):
        FakePg.sessions = 1
        with patch.object(m, "require_clients"), patch.object(m, "Pg", FakePg), patch.object(i, "backup_before_change") as backup:
            with self.assertRaises(m.SafeError):
                i.run("Apply", ENV)
        backup.assert_not_called()
        self.assertEqual(FakePg.writes, [])

    def test_backup_failure_blocks_all_permission_changes(self):
        with patch.object(m, "require_clients"), patch.object(m, "Pg", FakePg), patch.object(i, "backup_before_change", side_effect=m.SafeError("Private backup failed")):
            with self.assertRaises(m.SafeError):
                i.run("Apply", ENV)
        self.assertEqual(FakePg.writes, [])

    def test_apply_is_one_transaction_password_only_in_environment(self):
        with patch.object(m, "require_clients"), patch.object(m, "Pg", FakePg), patch.object(i, "backup_before_change", return_value=(MANIFEST, "a" * 64)), patch.object(i, "verify_own_login"):
            result = i.run("Apply", ENV)
        self.assertTrue(result["changed"])
        self.assertFalse(result["consumer_credentials_changed"])
        self.assertFalse(result["both_app_isolation_verified"])
        self.assertEqual(len(FakePg.writes), 1)
        sql, options = FakePg.writes[0]
        self.assertIn("BEGIN;", sql)
        self.assertTrue(sql.rstrip().endswith("COMMIT;"))
        self.assertIn("CONNECTION LIMIT 12", sql)
        self.assertIn("NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", sql)
        self.assertLess(sql.index("GRANT CONNECT,CREATE"), sql.index("ALTER SCHEMA public OWNER"))
        self.assertGreater(sql.index("REVOKE CREATE ON DATABASE"), sql.index("ALTER SCHEMA public OWNER"))
        self.assertLess(sql.index("SET LOCAL ROLE eventharbor_app"), sql.index("REVOKE ALL ON SCHEMA public"))
        self.assertIn("n.nspname='public'", sql)
        self.assertIn("format('ALTER %s public.%I", sql)
        for forbidden in ("REASSIGN OWNED", "DROP OWNED", "DROP DATABASE", "ALTER DATABASE", "pulseexchange", ENV["EVENTHARBOR_APP_PASSWORD"]):
            self.assertNotIn(forbidden, sql)
        self.assertEqual(options["extra_env"]["EVENTHARBOR_NEW_PASSWORD"], ENV["EVENTHARBOR_APP_PASSWORD"])

    def test_backup_filename_allowlist_and_separate_prefix(self):
        blob = i.EventHarborBackup(ENV, ENV["MIGRATION_RUN_ID"])
        self.assertTrue(blob.suffix("manifest.json").endswith("/eventharbor-isolation/manifest.json"))
        with self.assertRaises(m.SafeError):
            blob.suffix("../credentials")

    def test_cross_login_needs_actual_permission_denial(self):
        database = m.Database(m.TARGET_HOST, "pulseexchange", "eventharbor_app", "fixture")
        for result in (subprocess.CompletedProcess([], 0, "1", ""),
                       subprocess.CompletedProcess([], 2, "", "connection timed out"),
                       subprocess.CompletedProcess([], 2, "", "password authentication failed")):
            with patch.object(i.subprocess, "run", return_value=result), self.assertRaises(m.SafeError):
                i.require_denied_login(database, "offline-run")
        with patch.object(i.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", 'FATAL: permission denied for database "pulseexchange"')):
            i.require_denied_login(database, "offline-run")

    def test_backup_verifies_downloaded_bytes_not_metadata_only(self):
        blob = i.EventHarborBackup(ENV, ENV["MIGRATION_RUN_ID"])

        @contextmanager
        def response_for(data):
            response = io.BytesIO(data)
            response.headers = {"Content-Length": "4"}
            yield response

        digest = hashlib.sha256(b"safe").hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            good_path = Path(temporary) / "good.dump"
            bad_path = Path(temporary) / "bad.dump"
            with patch.object(blob, "request", side_effect=lambda *a: response_for(b"safe")):
                blob.download_verified(good_path, 4, digest)
            self.assertEqual(good_path.read_bytes(), b"safe")
            with patch.object(blob, "request", side_effect=lambda *a: response_for(b"evil")), self.assertRaises(m.SafeError):
                blob.download_verified(bad_path, 4, digest)
            self.assertFalse(bad_path.exists())
            with self.assertRaises(FileExistsError):
                blob.download_verified(good_path, 4, digest)
            self.assertEqual(good_path.read_bytes(), b"safe")

    def test_verify_both_requires_no_privileges_and_real_logins(self):
        admin = m.Pg(m.Database(m.TARGET_HOST, "eventharbor", "fixtureadmin", "fixture"), "offline-run")
        roles = [{"name": name, "superuser": False, "createdb": False, "createrole": False,
                  "replication": False, "bypassrls": False, "memberships": 0,
                  "limit": 12 if name == "eventharbor_app" else -1}
                 for name in ("eventharbor_app", "pulseexchange_app")]
        with patch.object(admin, "json", side_effect=[roles, {"eh_to_px": False, "px_to_eh": False}]), patch.object(i, "verify_own_login") as own, patch.object(i, "require_denied_login") as denied:
            result = i.verify_both(admin, ENV, "offline-run")
        self.assertEqual(own.call_count, 2)
        self.assertEqual(denied.call_count, 2)
        self.assertTrue(result["verified"])
        with patch.object(admin, "json", side_effect=[roles, {"eh_to_px": True, "px_to_eh": False}]), patch.object(i, "verify_own_login") as own:
            with self.assertRaises(m.SafeError):
                i.verify_both(admin, ENV, "offline-run")
        own.assert_not_called()

    def test_verify_rejects_role_membership(self):
        admin = m.Pg(m.Database(m.TARGET_HOST, "eventharbor", "fixtureadmin", "fixture"), "offline-run")
        roles = [{"name": name, "superuser": False, "createdb": False, "createrole": False,
                  "replication": False, "bypassrls": False, "memberships": 1, "limit": 12}
                 for name in ("eventharbor_app", "pulseexchange_app")]
        with patch.object(admin, "json", return_value=roles), self.assertRaises(m.SafeError):
            i.verify_both(admin, ENV, "offline-run")


if __name__ == "__main__":
    unittest.main()
