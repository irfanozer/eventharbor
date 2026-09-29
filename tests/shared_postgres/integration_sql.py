#!/usr/bin/env python3
"""Real PostgreSQL 17 copy checks on a disposable Docker network only.

Run inside the database-tools image with PYTHONPATH=/app, PGHOST=test-db,
PGPORT=5432, PGUSER=postgres, PGDATABASE=fixture_bootstrap and the disposable
server's PGPASSWORD. The PostgreSQL service must set POSTGRES_DB=fixture_bootstrap.
Mount this file read-only and override the image entrypoint with python3.

This is not an Azure integration test. It does not exercise managed identity,
Blob storage, production networking, or Azure-specific administrator behavior.
The surrounding CI job owns container cleanup; this script never drops a DB.
"""

from dataclasses import replace
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


HOST = "test-db"
BOOTSTRAP = "fixture_bootstrap"
SOURCE_ADMIN = "fixture_source_admin"
TARGET_ADMIN = "fixture_target_admin"
CASES = (
    ("fixture_event_source", "eventharbor_rehearsal", "eventharbor_rehearsal_app"),
    ("fixture_pulse_source", "pulseexchange_rehearsal", "pulseexchange_rehearsal_app"),
)
ALLOWED_DATABASES = {BOOTSTRAP, "eventharbor", "pulseexchange", *(name for case in CASES for name in case[:2])}
ALLOWED_USERS = {"postgres", SOURCE_ADMIN, TARGET_ADMIN, "portfolio_admin", "eventharbor_app", "pulseexchange_app", *(case[2] for case in CASES)}
RUN_ID = "offline-container-pg17-copy"


def require_disposable_environment():
    if (os.environ.get("PGHOST") != HOST or os.environ.get("PGDATABASE") != BOOTSTRAP
            or os.environ.get("PGUSER") != "postgres" or os.environ.get("PGPORT", "5432") != "5432"
            or not os.environ.get("PGPASSWORD")
            or any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE",
                                                   "SOURCE_DATABASE_URL", "TARGET_ADMIN_DATABASE_URL"))):
        raise RuntimeError("Refusing integration test outside the explicit disposable test-db/fixture_bootstrap environment.")


class CopySqlIntegration(unittest.TestCase):
    def test_non_superuser_copy_for_both_applications(self):
        require_disposable_environment()
        # The import must resolve to the reviewed tools image, not a test stub.
        import migrate as m
        import economy_migrate as economy

        self.assertEqual(Path(m.__file__).resolve(), Path("/app/migrate.py"))
        self.assertEqual(Path(economy.__file__).resolve(), Path("/app/economy_migrate.py"))
        original_env = m.Database.env
        original_run = subprocess.run

        def fixture_run(arguments, *args, **kwargs):
            result = original_run(arguments, *args, **kwargs)
            if result.returncode and Path(arguments[0]).name == 'psql':
                # Disposable fixture only. Print SQLSTATE, never the SQL input,
                # password environment, row data, or raw server error context.
                stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr or ''
                states = sorted(set(re.findall(r'ERROR:\s+([0-9A-Z]{5})(?:\s|$)', stderr)))
                print('FIXTURE_PSQL_FAILURE exit=' + str(result.returncode) + ' sqlstate=' + ','.join(states or ['unavailable']), flush=True)
            return result

        def fixture_env(database, run_id, *, readonly=False):
            if (database.host != HOST or database.name not in ALLOWED_DATABASES
                    or database.user not in ALLOWED_USERS):
                raise RuntimeError("Fixture connection escaped the fixed host/database/role allowlist.")
            result = original_env(database, run_id, readonly=readonly)
            # Test-process-only TLS override. The production helper is unchanged.
            result["PGSSLMODE"] = "disable"
            result.pop("PGSSLROOTCERT", None)
            return result

        with patch.object(m.Database, "env", fixture_env), patch.object(subprocess, 'run', fixture_run):
            self.run_copy_checks(m, economy)
        self.assertIs(m.Database.env, original_env)

    def run_copy_checks(self, m, economy):
        m.require_clients()
        bootstrap = m.Pg(m.Database(HOST, BOOTSTRAP, "postgres", os.environ["PGPASSWORD"]), RUN_ID)
        bootstrap.require_version()
        self.assertTrue(bootstrap.json("SELECT to_json(rolsuper) FROM pg_roles WHERE rolname=current_user;"))
        existing = bootstrap.json("SELECT (SELECT count(*) FROM pg_database WHERE datname IN "
                                  "('fixture_event_source','fixture_pulse_source','eventharbor_rehearsal','pulseexchange_rehearsal','eventharbor','pulseexchange')) + "
                                  "(SELECT count(*) FROM pg_roles WHERE rolname IN "
                                  "('fixture_source_admin','fixture_target_admin','portfolio_admin','azure_pg_admin','eventharbor_app','pulseexchange_app','eventharbor_rehearsal_app','pulseexchange_rehearsal_app')); ")
        self.assertEqual(existing, 0, "Fixture requires a fresh disposable PostgreSQL container; no existing objects are overwritten.")
        source_password, target_password = secrets.token_hex(24), secrets.token_hex(24)
        bootstrap.sql("\\getenv source_fixture_password FIXTURE_SOURCE_PASSWORD\n"
                      "\\getenv target_fixture_password FIXTURE_TARGET_PASSWORD\nBEGIN;\n"
                      "CREATE ROLE fixture_source_admin LOGIN PASSWORD :'source_fixture_password' "
                      "NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS;\n"
                      "CREATE ROLE fixture_target_admin LOGIN PASSWORD :'target_fixture_password' "
                      "NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS;\n"
                      "CREATE ROLE portfolio_admin LOGIN PASSWORD :'target_fixture_password' "
                      "NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS;\n"
                      "CREATE ROLE azure_pg_admin NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;\n"
                      "GRANT azure_pg_admin TO portfolio_admin WITH INHERIT TRUE;\n"
                      "GRANT pg_read_all_stats TO fixture_source_admin,fixture_target_admin,portfolio_admin;\nCOMMIT;",
                      readonly=False, extra_env={"FIXTURE_SOURCE_PASSWORD": source_password,
                                                 "FIXTURE_TARGET_PASSWORD": target_password})
        source_admin = m.Pg(m.Database(HOST, BOOTSTRAP, SOURCE_ADMIN, source_password), RUN_ID)
        target_admin = m.Pg(m.Database(HOST, BOOTSTRAP, TARGET_ADMIN, target_password), RUN_ID)
        foundation_admin = economy.EconomyPg(m.Database(HOST, BOOTSTRAP, "portfolio_admin", target_password), RUN_ID)
        for admin in (source_admin, target_admin, foundation_admin):
            attributes = admin.json("SELECT json_build_object('superuser',rolsuper,'createdb',rolcreatedb,"
                                    "'createrole',rolcreaterole,'bypassrls',rolbypassrls) FROM pg_roles WHERE rolname=current_user;")
            self.assertEqual(attributes, {"superuser": False, "createdb": True, "createrole": True, "bypassrls": False})

        with tempfile.TemporaryDirectory(prefix="pg17-copy-fixture-") as temporary:
            directory = Path(temporary)
            final_apps = []
            for source_name, target_name, application_role in CASES:
                with self.subTest(target=target_name):
                    source_admin.sql("CREATE DATABASE " + m.ident(source_name)
                                     + " TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C';",
                                     readonly=False)
                    source = m.Pg(replace(source_admin.database, name=source_name), RUN_ID)
                    self.seed(source)
                    application_password = secrets.token_hex(24)
                    target = economy.EconomyPg(m.Database(HOST, target_name, application_role, application_password), RUN_ID)
                    def azure_public_schema():
                        m.Pg(replace(bootstrap.database, name=target_name), RUN_ID).sql(
                            'ALTER SCHEMA public OWNER TO azure_pg_admin;', readonly=False)
                    # Exercise the actual production role/database creation SQL,
                    # including createrole_self_grant=SET without INHERIT.
                    if target_name == 'eventharbor_rehearsal':
                        # Reproduce the already-created Azure rehearsal exactly:
                        # correct retained app/database, inaccessible public CREATE.
                        m.Pg.create_target(foundation_admin, target_name, application_role, application_password, source.locale())
                        azure_public_schema()
                        self.assertFalse(target.json("SELECT to_json(has_schema_privilege(current_user,'public','CREATE'));"))
                        # Reproduce the missing CURRENT-caller CREATE privilege:
                        # CONNECT alone permits login but not schema transfer.
                        target.sql('GRANT CONNECT ON DATABASE ' + m.ident(target_name) + ' TO portfolio_admin;', readonly=False)
                        admin_target = economy.EconomyPg(replace(foundation_admin.database, name=target_name), RUN_ID)
                        try:
                            self.assertFalse(admin_target.json("SELECT to_json(has_database_privilege(current_user,current_database(),'CREATE'));"))
                            with self.assertRaises(m.SafeError):
                                admin_target.sql('ALTER SCHEMA public OWNER TO ' + m.ident(application_role) + ';', readonly=False)
                        finally:
                            target.sql('REVOKE CONNECT ON DATABASE ' + m.ident(target_name) + ' FROM portfolio_admin;', readonly=False)
                        foundation_admin.prepare_rehearsal_schema(target, recovery=True)
                    else:
                        original_create = m.Pg.create_target
                        def create_azure_template(admin, database, role, password, locale):
                            original_create(admin, database, role, password, locale)
                            azure_public_schema()
                        with patch.object(m.Pg, 'create_target', create_azure_template):
                            foundation_admin.create_target(target_name, application_role, application_password, source.locale())
                    membership = foundation_admin.json("SELECT json_build_object('can_set',pg_has_role(current_user,"
                                                   + m.literal(application_role) + ",'SET'),'inherits',pg_has_role(current_user,"
                                                   + m.literal(application_role) + ",'USAGE')); ")
                    self.assertEqual(membership, {"can_set": True, "inherits": False})
                    self.assertEqual(target.json("SELECT to_json(pg_get_userbyid(nspowner)::text) FROM pg_namespace WHERE nspname='public';"), application_role)
                    self.assertFalse(target.json("SELECT to_json(has_database_privilege('portfolio_admin',current_database(),'CONNECT'));"))
                    self.assertFalse(target.json("SELECT to_json(has_database_privilege('portfolio_admin',current_database(),'CREATE'));"))
                    app_attributes = foundation_admin.json("SELECT json_build_object('superuser',rolsuper,'createdb',rolcreatedb,"
                                                       "'createrole',rolcreaterole,'replication',rolreplication,'bypassrls',rolbypassrls,"
                                                       "'memberships',(SELECT count(*) FROM pg_auth_members WHERE member=r.oid)) "
                                                       "FROM pg_roles r WHERE rolname=" + m.literal(application_role) + ";")
                    self.assertEqual(app_attributes, {"superuser": False, "createdb": False, "createrole": False,
                                                     "replication": False, "bypassrls": False, "memberships": 0})
                    target.require_version()
                    dump = directory / (source_name + ".dump")
                    with source.snapshot() as snapshot:
                        expected = source.manifest(snapshot, directory)
                        m.native(["pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-acl",
                                  "--snapshot=" + snapshot, "--file=" + str(dump)],
                                 source.database.env(RUN_ID, readonly=True))
                    self.assertGreater(dump.stat().st_size, 0)
                    m.native(["pg_restore", "--no-password", "--no-owner", "--no-acl", "--no-tablespaces",
                              "--exit-on-error", "--single-transaction", "--dbname=" + target_name, str(dump)],
                             target.database.env(RUN_ID))
                    target.analyze_user_tables()
                    with target.snapshot() as snapshot:
                        restored = target.manifest(snapshot, directory)
                    m.require_same(expected, restored, sequences=True)
                    self.assertEqual(target.json("SELECT count(*) FROM public.fixture_events;"), 3)
                    self.assertEqual(target.json("SELECT last_value FROM public.fixture_events_id_seq;"), 40)
                    target.sql("BEGIN; ALTER TABLE public.fixture_events ADD COLUMN fixture_migration_probe integer; "
                               "UPDATE public.fixture_events SET fixture_migration_probe=1; ROLLBACK;", readonly=False)
                    source_schema = source.json("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                                                "AND table_name='fixture_events' AND column_name='fixture_migration_probe';")
                    self.assertEqual(source_schema, 0)
                    self.assertEqual(target.json("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                                                 "AND table_name='fixture_events' AND column_name='fixture_migration_probe';"), 0)
                    with self.assertRaises(m.SafeError):
                        foundation_admin.create_target(target_name, application_role, application_password, source.locale())
                    with self.assertRaises(m.SafeError):
                        foundation_admin.prepare_rehearsal_schema(target, recovery=True)
                    print("PG17 non-superuser copy, exact manifest, sequence and owner-DDL checks passed: " + target_name)
                    project = target_name.removesuffix("_rehearsal")
                    foundation_admin.sql("CREATE DATABASE " + m.ident(project)
                                         + " TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C';",
                                         readonly=False)
                    foundation_database = economy.EconomyPg(replace(foundation_admin.database, name=project), RUN_ID)
                    # Emulate the observed Azure layout: DB owner is the login,
                    # public schema is owned by its inherited azure_pg_admin role.
                    m.Pg(replace(bootstrap.database, name=project), RUN_ID).sql(
                        "ALTER SCHEMA public OWNER TO azure_pg_admin;", readonly=False)
                    bootstrap.sql("REVOKE azure_pg_admin FROM portfolio_admin;", readonly=False)
                    with self.assertRaises(m.SafeError):
                        foundation_admin.empty_foundation(project, project + "_app", source.locale())
                    bootstrap.sql("GRANT azure_pg_admin TO portfolio_admin WITH INHERIT TRUE;", readonly=False)
                    ownership = foundation_database.json(
                        "SELECT json_build_object('owner',pg_get_userbyid(nspowner),"
                        "'effective_owner',pg_has_role(current_user,nspowner,'USAGE')) "
                        "FROM pg_namespace WHERE nspname='public';")
                    self.assertEqual(ownership, {"owner": "azure_pg_admin", "effective_owner": True})
                    foundation_database.sql("CREATE TABLE public.fixture_refusal_probe(value text); "
                                            "INSERT INTO public.fixture_refusal_probe VALUES ('must remain after refusal');", readonly=False)
                    with self.assertRaises(m.SafeError):
                        foundation_admin.claim_empty_foundation(project, project + "_app", application_password, source.locale())
                    self.assertEqual(foundation_database.json("SELECT count(*) FROM public.fixture_refusal_probe;"), 1)
                    self.assertFalse(foundation_admin.json("SELECT to_json(EXISTS(SELECT 1 FROM pg_roles WHERE rolname=" + m.literal(project + "_app") + "));"))
                    # Explicit cleanup of this test's sole negative-case table,
                    # only on the fixed disposable host. The helper does no cleanup.
                    foundation_database.sql("DROP TABLE public.fixture_refusal_probe;", readonly=False)
                    foundation_admin.claim_empty_foundation(project, project + "_app", application_password, source.locale())
                    final_app = economy.EconomyPg(m.Database(HOST, project, project + "_app", application_password), RUN_ID)
                    self.assertEqual(final_app.json("SELECT to_json(pg_get_userbyid(datdba)::text) FROM pg_database WHERE datname=current_database();"), project + "_app")
                    m.native(["pg_restore", "--no-password", "--no-owner", "--no-acl", "--no-tablespaces",
                              "--exit-on-error", "--single-transaction", "--dbname=" + project, str(dump)], final_app.database.env(RUN_ID))
                    final_app.analyze_user_tables()
                    with final_app.snapshot() as snapshot:
                        m.require_same(expected, final_app.manifest(snapshot, directory), sequences=True)
                    final_app.sql("BEGIN; ALTER TABLE public.fixture_events ADD COLUMN owner_ddl_probe integer; ROLLBACK;", readonly=False)
                    self.assertEqual(final_app.json("SELECT rolconnlimit FROM pg_roles WHERE rolname=current_user;"), 12)
                    with self.assertRaises(m.SafeError):
                        foundation_admin.claim_empty_foundation(project, project + "_app", application_password, source.locale())
                    final_apps.append(final_app)
                    print("PG17 empty-foundation refusal, atomic owner transfer and exact copy passed: " + project)
            self.assertEqual(len(final_apps), 2, "Both complete final-copy fixtures must pass before isolation checks.")
            for app in final_apps:
                other = "pulseexchange" if app.database.name == "eventharbor" else "eventharbor"
                self.assertFalse(app.json("SELECT to_json(has_database_privilege(current_user," + m.literal(other) + ",'CONNECT'));"))
                with self.assertRaises(m.SafeError):
                    economy.EconomyPg(replace(app.database, name=other), RUN_ID).sql("SELECT 1;")
            print("PG17 both final app roles denied cross-database login")

    @staticmethod
    def seed(source):
        source.sql(r"""
CREATE TYPE public.fixture_status AS ENUM ('pending','done');
CREATE TABLE public.alembic_version(version_num varchar(32) PRIMARY KEY);
INSERT INTO public.alembic_version VALUES ('fixture_0001');
CREATE TABLE public.fixture_events(
 id serial PRIMARY KEY, state public.fixture_status NOT NULL,
 payload jsonb NOT NULL, note text NOT NULL);
INSERT INTO public.fixture_events(state,payload,note) VALUES
 ('pending','{"message":"first","n":1}',E'line one\nline two'),
 ('done','{"message":"second","n":2}',E'backslash \\ and tab\t'),
 ('done','{"message":"third","n":3}','quote '' and unicode');
SELECT setval('public.fixture_events_id_seq',40,true);
CREATE INDEX fixture_events_state_idx ON public.fixture_events(state);
CREATE VIEW public.fixture_event_view AS SELECT id,state FROM public.fixture_events;
CREATE FUNCTION public.fixture_echo(value public.fixture_status) RETURNS text
 LANGUAGE sql IMMUTABLE AS $$ SELECT value::text $$;
""", readonly=False)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        require_disposable_environment()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CopySqlIntegration)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("SHARED_POSTGRES_PG17_SQL_INTEGRATION_PASS")
    sys.exit(0 if result.wasSuccessful() else 1)
