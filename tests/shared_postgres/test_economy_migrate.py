"""Offline fixed-target and empty-database guards for the new-server copy helper."""

import copy
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[2] / "infra/azure-shared-postgres"
for name in ("migrate", "economy_migrate"):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
e = sys.modules["economy_migrate"]
m = e.m
LOCALE = {"encoding": "UTF8", "collate": "en_US.utf8", "ctype": "en_US.utf8", "provider": "c"}
PASSWORD = "fixture-only-new-role-password-123456789"
MANIFEST = {"tables": [], "sequences": [], "alembic_version": ["fixture_0001"]}


def metadata(project):
    return {"database": project, "user": "portfolio_admin", "owner": "portfolio_admin",
            "schemas": ["public"], "public_owner": "pg_database_owner", "public_owner_access": True, "relations": 0,
            "types": 0, "routines": 0, "extensions": ["plpgsql"], "large_objects": 0,
            "event_triggers": 0, "role_exists": False}


class EmptyFoundationTests(unittest.TestCase):
    def setUp(self):
        self.admin = e.EconomyPg(m.Database(e.APPROVED_TARGET_HOST, "postgres", "portfolio_admin", "fixture-only"), "fixture-run-20260929")

    def test_exact_sources_and_new_target_only(self):
        for project, host in e.SOURCES.items():
            source = f"postgresql+asyncpg://oldadmin:fixture-only@{host}/{project}?ssl=require"
            target = f"postgresql+asyncpg://portfolio_admin:fixture-only@{e.APPROVED_TARGET_HOST}/{project}?ssl=require"
            self.assertEqual(e.parse_database(source, project, source=True).host, host)
            self.assertEqual(e.parse_database(target, project, source=False).name, "postgres")
            for invalid in (target.replace(e.APPROVED_TARGET_HOST, host), target.replace("portfolio_admin", "other_admin"),
                            target.replace("ssl=require", "ssl=disable"), target + "&ssl=require", target + "#fragment"):
                with self.subTest(project=project), self.assertRaises(m.SafeError):
                    e.parse_database(invalid, project, source=False)
            wrong_project = "pulseexchange" if project == "eventharbor" else "eventharbor"
            with self.assertRaises(m.SafeError):
                e.parse_database(source, wrong_project, source=True)
        with patch.object(e, "APPROVED_TARGET_HOST", None), self.assertRaises(m.SafeError):
            e.parse_database(target, project, source=False)

    def test_both_empty_foundation_targets_pass_without_writes(self):
        for project in e.SOURCES:
            with patch.object(e.EconomyPg, "require_version"), patch.object(e.EconomyPg, "json", side_effect=[metadata(project), 0]), patch.object(e.EconomyPg, "locale", return_value=LOCALE), patch.object(e.EconomyPg, "sql") as sql:
                target = self.admin.empty_foundation(project, project + "_app", LOCALE)
            self.assertEqual(target.database.name, project)
            sql.assert_not_called()

    def test_data_types_routines_extensions_roles_and_wrong_owners_refused(self):
        mutations = ({"relations": 1}, {"types": 1}, {"routines": 1}, {"extensions": ["plpgsql", "other"]},
                     {"role_exists": True}, {"large_objects": 1}, {"event_triggers": 1},
                     {"owner": "other"}, {"user": "other"}, {"public_owner": "other"}, {"public_owner_access": False},
                     {"public_owner": "azure_pg_admin", "public_owner_access": False},
                     {"schemas": ["public", "unexpected"]}, {"database": "pulseexchange"})
        for mutation in mutations:
            value = {**metadata("eventharbor"), **mutation}
            with self.subTest(mutation=mutation), patch.object(e.EconomyPg, "require_version"), patch.object(e.EconomyPg, "json", return_value=value), patch.object(e.EconomyPg, "sql") as sql:
                with self.assertRaises(m.SafeError):
                    self.admin.claim_empty_foundation("eventharbor", "eventharbor_app", PASSWORD, LOCALE)
                sql.assert_not_called()

    def test_exact_azure_public_owner_requires_effective_owner_membership(self):
        for project in e.SOURCES:
            value = {**metadata(project), "public_owner": "azure_pg_admin"}
            with patch.object(e.EconomyPg, "require_version"), patch.object(e.EconomyPg, "json", side_effect=[value, 0]), patch.object(e.EconomyPg, "locale", return_value=LOCALE), patch.object(e.EconomyPg, "sql") as execute:
                self.admin.claim_empty_foundation(project, project + "_app", PASSWORD, LOCALE)
            statement = execute.call_args.args[0]
            self.assertIn("NOT IN ('portfolio_admin','pg_database_owner','azure_pg_admin')", statement)
            self.assertIn("OR NOT pg_has_role(current_user,nspowner,'USAGE')", statement)
            self.assertLess(statement.index("pg_has_role(current_user,nspowner,'USAGE')"), statement.index("CREATE ROLE"))
            self.assertLess(statement.index("GRANT CREATE ON DATABASE"), statement.index("ALTER SCHEMA public OWNER TO"))

    def test_locale_and_active_sessions_refused(self):
        with patch.object(e.EconomyPg, "require_version"), patch.object(e.EconomyPg, "json", return_value=metadata("eventharbor")), patch.object(e.EconomyPg, "locale", return_value={**LOCALE, "collate": "C"}), self.assertRaises(m.SafeError):
            self.admin.empty_foundation("eventharbor", "eventharbor_app", LOCALE)
        with patch.object(e.EconomyPg, "require_version"), patch.object(e.EconomyPg, "json", side_effect=[metadata("eventharbor"), 1]), patch.object(e.EconomyPg, "locale", return_value=LOCALE), self.assertRaises(m.SafeError):
            self.admin.empty_foundation("eventharbor", "eventharbor_app", LOCALE)

    def test_unapproved_database_role_pair_refused(self):
        for database, role in (("postgres", "postgres_app"), ("eventharbor", "pulseexchange_app"), ("eventharbor;bad", "eventharbor_app")):
            with self.assertRaises(m.SafeError):
                self.admin.empty_foundation(database, role, LOCALE)

    def test_claim_is_atomic_and_never_drops_or_overwrites(self):
        for project in e.SOURCES:
            with patch.object(e.EconomyPg, "require_version"), patch.object(e.EconomyPg, "json", side_effect=[metadata(project), 0]), patch.object(e.EconomyPg, "locale", return_value=LOCALE), patch.object(e.EconomyPg, "sql") as sql:
                self.admin.claim_empty_foundation(project, project + "_app", PASSWORD, LOCALE)
            sql.assert_called_once()
            statement = sql.call_args.args[0]
            self.assertIn("BEGIN;", statement)
            self.assertTrue(statement.endswith("COMMIT;"))
            self.assertIn("CREATE ROLE", statement)
            self.assertIn("ALTER DATABASE", statement)
            self.assertIn("SET LOCAL ROLE", statement)
            self.assertIn("CONNECTION LIMIT 12", statement)
            self.assertIn("NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", statement)
            self.assertIn("pg_stat_activity", statement)
            for forbidden in ("DROP ", "TRUNCATE ", "REASSIGN OWNED", "CREATE DATABASE", PASSWORD):
                self.assertNotIn(forbidden, statement)
            self.assertEqual(sql.call_args.kwargs["extra_env"], {"MIGRATION_NEW_PASSWORD": PASSWORD})

    def test_backup_prefixes_separate_projects_and_adapter_download_name(self):
        env = {"BACKUP_STORAGE_ACCOUNT": "fixtureprivate", "BACKUP_CONTAINER": "private-backups"}
        for project in e.SOURCES:
            blob = e.EconomyBlob(env, "fixture-run-20260929", project)
            expected = f"/economy-postgres/{project}/fixture-run-20260929/{project}.dump"
            self.assertEqual(blob.suffix(project + ".dump"), expected)
            self.assertEqual(blob.suffix("pulseexchange.dump"), expected)
            with self.assertRaises(m.SafeError):
                blob.suffix("../secret")

    def test_source_freeze_uses_selected_database(self):
        source = e.EconomyPg(m.Database(e.SOURCES["eventharbor"], "eventharbor", "fixture", "fixture"), "fixture-run")
        with patch.object(source, "json", return_value=0) as query:
            source.require_frozen()
        self.assertIn("datname='eventharbor'", query.call_args.args[0])
        self.assertIn("application_name IS DISTINCT FROM", query.call_args.args[0])
        with patch.object(source, "json", return_value=1), self.assertRaises(m.SafeError):
            source.require_frozen()


class RehearsalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.run_id = 'fixture-run-20260929'
        self.admin = e.EconomyPg(m.Database(e.APPROVED_TARGET_HOST, 'postgres', 'portfolio_admin', 'fixture'), self.run_id)
        self.target = e.EconomyPg(m.Database(e.APPROVED_TARGET_HOST, 'eventharbor_rehearsal', 'eventharbor_rehearsal_app', PASSWORD), self.run_id)
        self.metadata = {'database': self.target.database.name, 'user': self.target.database.user,
                         'owner': self.target.database.user, 'database_comment': 'shared-postgres-helper:' + self.run_id,
                         'role_comment': 'shared-postgres-helper:' + self.run_id, 'login': True,
                         'superuser': False, 'createdb': False, 'createrole': False, 'replication': False,
                         'bypassrls': False, 'memberships': 0, 'schemas': ['public'], 'public_owner': 'azure_pg_admin',
                         'admin_owner': True, 'admin_connect': False, 'relations': 0, 'types': 0, 'routines': 0,
                         'extensions': ['plpgsql'], 'large_objects': 0, 'event_triggers': 0, 'sessions': 0}

    def test_empty_run_marked_azure_rehearsal_guard_and_refusals(self):
        mutations = ({}, {'owner': 'other'}, {'role_comment': 'other'}, {'database_comment': 'other'},
                     {'relations': 1}, {'types': 1}, {'routines': 1}, {'schemas': ['public', 'extra']},
                     {'sessions': 1}, {'memberships': 1}, {'superuser': True}, {'createdb': True},
                     {'createrole': True}, {'replication': True}, {'bypassrls': True}, {'login': False},
                     {'public_owner': self.target.database.user}, {'public_owner': 'pg_database_owner'},
                     {'admin_owner': False}, {'extensions': ['plpgsql', 'other']}, {'large_objects': 1}, {'event_triggers': 1})
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(self.target, 'require_version'), patch.object(self.target, 'json', return_value={**self.metadata, **mutation}), patch.object(self.target, 'sql') as execute:
                if mutation:
                    with self.assertRaises(m.SafeError):
                        self.admin.empty_rehearsal(self.target, recovery=True)
                else:
                    self.assertEqual(self.admin.empty_rehearsal(self.target, recovery=True), self.metadata)
                execute.assert_not_called()

    def test_recovery_limits_temporary_connection_grant_and_always_revokes(self):
        for fails in (False, True):
            calls = []
            def execute(pg, sql, **kwargs):
                calls.append((pg.database.user, sql))
                if fails and 'ALTER SCHEMA' in sql:
                    raise m.SafeError('Fixture schema repair refused')
            with patch.object(self.admin, 'empty_rehearsal', return_value=self.metadata), patch.object(e.EconomyPg, 'sql', execute):
                if fails:
                    with self.assertRaises(m.SafeError):
                        self.admin.prepare_rehearsal_schema(self.target, recovery=True)
                else:
                    self.admin.prepare_rehearsal_schema(self.target, recovery=True)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][0], 'eventharbor_rehearsal_app')
            self.assertIn('GRANT CONNECT ON DATABASE "eventharbor_rehearsal" TO portfolio_admin', calls[0][1])
            self.assertEqual(calls[1][0], 'portfolio_admin')
            self.assertIn("pg_has_role(current_user,'azure_pg_admin','USAGE')", calls[1][1])
            self.assertIn('SET LOCAL ROLE "eventharbor_rehearsal_app"', calls[1][1])
            self.assertIn('REVOKE CONNECT ON DATABASE "eventharbor_rehearsal" FROM portfolio_admin', calls[-1][1])
            for _, sql in calls:
                for forbidden in ('DROP ', 'TRUNCATE ', 'CREATE ROLE', 'PASSWORD'):
                    self.assertNotIn(forbidden, sql)

    def test_existing_admin_access_is_not_revoked(self):
        with patch.object(self.admin, 'empty_rehearsal', return_value={**self.metadata, 'admin_connect': True}), patch.object(e.EconomyPg, 'sql') as execute:
            self.admin.prepare_rehearsal_schema(self.target, recovery=True)
        execute.assert_called_once()

    def test_guard_is_repeated_before_schema_mutation(self):
        guard = self.admin.rehearsal_guard(self.target, 'azure_pg_admin')
        for required in ('pg_database', 'pg_authid', 'pg_auth_members', 'pg_namespace', 'pg_class', 'pg_type', 'pg_proc', 'pg_extension', 'pg_event_trigger', 'pg_stat_activity', 'shared-postgres-helper:' + self.run_id):
            self.assertIn(required, guard)
        self.assertNotIn(PASSWORD, guard)

    def test_recovery_downloads_same_archive_and_never_reads_or_dumps_live_source(self):
        calls = []
        record = {'format': 2, 'project': 'eventharbor', 'run_id': self.run_id, 'mode': 'Rehearse',
                  'source_host': e.SOURCES['eventharbor'], 'source_database': 'eventharbor',
                  'target_host': e.APPROVED_TARGET_HOST, 'target_database': 'eventharbor_rehearsal',
                  'dump_bytes': 7, 'dump_sha256': 'a' * 64, 'source_manifest': copy.deepcopy(MANIFEST)}
        class Blob:
            def __init__(self, *args): pass
            def require_private(self): calls.append('private')
            def manifest(self): return copy.deepcopy(record)
            def download_dump(self, path, **kwargs):
                calls.append('download'); path.write_bytes(b'fixture')
        @contextmanager
        def snapshot(*args): yield '00000003-00000004-1'
        env = {'MIGRATION_PROJECT': 'eventharbor', 'MIGRATION_RUN_ID': self.run_id,
               'SOURCE_DATABASE_URL': f"postgresql://old:fixture@{e.SOURCES['eventharbor']}/eventharbor?ssl=require",
               'TARGET_ADMIN_DATABASE_URL': f'postgresql://portfolio_admin:fixture@{e.APPROVED_TARGET_HOST}/postgres?ssl=require',
               'REHEARSAL_APP_PASSWORD': PASSWORD}
        def version(pg):
            self.assertEqual(pg.database.host, e.APPROVED_TARGET_HOST)
        def native(arguments, environment):
            self.assertEqual(arguments[0], 'pg_restore')
            self.assertIn('--single-transaction', arguments)
            self.assertIn('--dbname=eventharbor_rehearsal', arguments)
            self.assertEqual(environment['PGPASSWORD'], PASSWORD)
            calls.append('restore')
        with patch.object(e, 'EconomyBlob', Blob), patch.object(m, 'require_clients'), patch.object(m, 'native', side_effect=native), patch.object(e.EconomyPg, 'require_version', version), patch.object(e.EconomyPg, 'prepare_rehearsal_schema', side_effect=lambda *args, **kwargs: calls.append('repair')), patch.object(e.EconomyPg, 'analyze_user_tables', side_effect=lambda: calls.append('analyze')), patch.object(e.EconomyPg, 'snapshot', snapshot), patch.object(e.EconomyPg, 'manifest', return_value=MANIFEST):
            result = e.run('RecoverRehearsal', env)
        self.assertEqual(calls, ['private', 'download', 'repair', 'restore', 'analyze'])
        self.assertTrue(result['verified'])
        self.assertTrue(result['changed'])
        self.assertFalse(result['sequences_verified'])
        for key in ('project', 'run_id', 'mode', 'source_host', 'source_database', 'target_host', 'target_database'):
            original = record[key]
            record[key] = 'unrelated'
            calls.clear()
            with self.subTest(wrong_manifest_field=key), patch.object(e, 'EconomyBlob', Blob), patch.object(m, 'require_clients'), patch.object(e.EconomyPg, 'require_version', version), patch.object(e.EconomyPg, 'prepare_rehearsal_schema') as repair:
                with self.assertRaises(m.SafeError):
                    e.run('RecoverRehearsal', env)
                repair.assert_not_called()
                self.assertEqual(calls, ['private'])
            record[key] = original


class PhaseTests(unittest.TestCase):
    def exercise(self, project, *, fail_backup=False, frozen=True):
        calls = []
        saved = {}

        class Pg:
            def __init__(self, database, run_id):
                self.database, self.run_id = database, run_id

            def require_version(self):
                calls.append("version")

            def locale(self):
                return LOCALE

            def empty_foundation(self, *args):
                calls.append("empty-guard")

            def require_absent(self, *args):
                calls.append("absent-guard")

            def require_frozen(self):
                calls.append("frozen")

            @contextmanager
            def snapshot(self):
                yield "00000003-00000004-1"

            def manifest(self, *args):
                return copy.deepcopy(MANIFEST)

            def claim_empty_foundation(self, *args):
                calls.append("claim")

            def create_target(self, *args):
                calls.append("create")

            def analyze_user_tables(self):
                calls.append("analyze")

        class Blob:
            def __init__(self, *args):
                pass

            def require_private(self):
                calls.append("private")

            def upload(self, path, name):
                if fail_backup:
                    raise m.SafeError("Fixture backup failure")
                calls.append("upload-" + name)
                if name == "manifest.json":
                    import json
                    saved.update(json.loads(path.read_text()))
                return "a" * 64

            def manifest(self):
                return copy.deepcopy(saved)

            def download_dump(self, path, **kwargs):
                calls.append("verified-download")
                path.write_bytes(b"fixture")

        def native(arguments, environment):
            if arguments[0] == "pg_dump":
                Path(next(item.removeprefix("--file=") for item in arguments if item.startswith("--file="))).write_bytes(b"fixture")
                calls.append("dump")
            else:
                self.assertEqual(Path(arguments[-1]).name, "verified.dump")
                calls.append("restore")

        env = {"MIGRATION_PROJECT": project, "MIGRATION_RUN_ID": "fixture-run-20260929",
               "SOURCE_DATABASE_URL": f"postgresql://oldadmin:fixture@{e.SOURCES[project]}/{project}?ssl=require",
               "TARGET_ADMIN_DATABASE_URL": f"postgresql://portfolio_admin:fixture@{e.APPROVED_TARGET_HOST}/postgres?ssl=require",
               "TARGET_APP_PASSWORD": PASSWORD, "WRITERS_FROZEN": "true" if frozen else "false"}
        with patch.object(e, "EconomyPg", Pg), patch.object(e, "EconomyBlob", Blob), patch.object(m, "require_clients"), patch.object(m, "native", side_effect=native):
            if fail_backup or not frozen:
                with self.assertRaises(m.SafeError):
                    e.run("FinalCopy", env)
                self.assertNotIn("claim", calls)
                self.assertNotIn("restore", calls)
            else:
                result = e.run("FinalCopy", env)
                self.assertTrue(result["verified"])
                self.assertFalse(result["cutover_performed"])
                self.assertLess(calls.index("verified-download"), calls.index("claim"))
                self.assertLess(calls.index("claim"), calls.index("restore"))
                self.assertNotIn("create", calls)
                self.assertEqual(saved["project"], project)
                self.assertEqual(saved["source_host"], e.SOURCES[project])
        return calls

    def test_both_final_backups_precede_empty_target_claim_and_download_is_restored(self):
        for project in e.SOURCES:
            with self.subTest(project=project):
                self.exercise(project)

    def test_failed_backup_prevents_target_change(self):
        self.exercise("eventharbor", fail_backup=True)

    def test_missing_freeze_prevents_target_change(self):
        self.exercise("pulseexchange", frozen=False)


if __name__ == "__main__":
    unittest.main()
