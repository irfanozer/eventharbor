"""Offline regression checks. Never connect to PostgreSQL, Azure or an identity endpoint."""

import ast
import copy
from contextlib import contextmanager
import importlib.util
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("shared_postgres_migrate", ROOT / "infra/azure-shared-postgres/migrate.py")
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)

SOURCE_URL = f"postgresql+asyncpg://oldadmin:private%24source@{m.SOURCE_HOST}/pulseexchange?ssl=require"
TARGET_URL = f"postgresql+asyncpg://oldadmin:private%24target@{m.TARGET_HOST}/eventharbor"
ENV = {"SOURCE_DATABASE_URL": SOURCE_URL, "TARGET_ADMIN_DATABASE_URL": TARGET_URL,
       "TARGET_APP_PASSWORD": "private-final-password-fixture-12345",
       "REHEARSAL_APP_PASSWORD": "private-rehearsal-password-fixture-12345",
       "MIGRATION_RUN_ID": "offline-test-20260929", "WRITERS_FROZEN": "true",
       "BACKUP_STORAGE_ACCOUNT": "privatefixture", "BACKUP_CONTAINER": "private-backups"}
MANIFEST = {"tables": [{"schema": "public", "name": "alembic_version", "kind": "r",
                         "columns": [["version_num", "character varying(32)", True]],
                         "rows": 1, "sha256": "a" * 64}],
            "sequences": [{"schema": "public", "name": "market_sequence", "last_value": 3, "is_called": True}],
            "alembic_version": ["20260827_0002"]}


class CredentialsAndSqlTests(unittest.TestCase):
    def test_scalar_boolean_json_requires_postgres_json_encoding(self):
        pg = m.Pg(m.Database.parse(SOURCE_URL, source=True), "offline-run")
        for result, expected in ((b"true\n", True), (b"false\n", False)):
            with patch.object(pg, "sql", return_value=result):
                self.assertIs(pg.json("SELECT to_json(true);"), expected)
        for result in (b"t\n", b"f\n"):
            with patch.object(pg, "sql", return_value=result), self.assertRaises(m.SafeError):
                pg.json("SELECT true;")

    def test_integration_scalar_boolean_projections_are_json_encoded(self):
        fixture = ast.parse((ROOT / "tests/shared_postgres/integration_sql.py").read_text(encoding="utf-8"))
        projections = []
        for node in ast.walk(fixture):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "json" and node.args):
                continue
            prefix = node.args[0]
            while isinstance(prefix, ast.BinOp) and isinstance(prefix.op, ast.Add):
                prefix = prefix.left
            if isinstance(prefix, ast.Constant) and isinstance(prefix.value, str):
                projections.append(prefix.value.strip())
        for boolean in ("rolsuper", "EXISTS(", "has_database_privilege(", "has_schema_privilege("):
            self.assertGreaterEqual(sum(value.startswith("SELECT to_json(" + boolean) for value in projections), 1)
            self.assertFalse(any(value.startswith("SELECT " + boolean) for value in projections))

    def test_fixed_endpoints_and_tls_without_password_arguments(self):
        db = m.Database.parse(SOURCE_URL, source=True)
        self.assertEqual(db.password, "private$source")
        with patch.dict(m.os.environ, {"PGHOSTADDR": "evil", "PGSERVICE": "evil"}):
            env = db.env("offline-run", readonly=True)
        self.assertNotIn("PGHOSTADDR", env)
        self.assertNotIn("PGSERVICE", env)
        self.assertEqual(env["PGSSLMODE"], "verify-full")
        self.assertEqual(env["PGSSLROOTCERT"], m.CERTIFICATES)
        self.assertIn("default_transaction_read_only=on", env["PGOPTIONS"])
        for url in (SOURCE_URL.replace(m.SOURCE_HOST, m.TARGET_HOST),
                    SOURCE_URL.replace("/pulseexchange?", "/eventharbor?"),
                    SOURCE_URL.replace("ssl=require", "ssl=disable"),
                    SOURCE_URL + "&ssl=verify-full", SOURCE_URL + "#unsafe"):
            with self.subTest(url=url), self.assertRaises(m.SafeError):
                m.Database.parse(url, source=True)

    def test_native_errors_never_include_raw_diagnostics(self):
        failures = [subprocess.CompletedProcess([], 1, "private credential", "private credential"),
                    subprocess.CompletedProcess([], 0, "", "private warning")]
        for result in failures:
            with patch.object(m.subprocess, "run", return_value=result), self.assertRaises(m.SafeError) as raised:
                m.native(["psql"], {})
            self.assertNotIn("private", str(raised.exception))

    def test_statistics_update_targets_only_explicit_owned_user_tables(self):
        pg = m.Pg(m.Database(m.TARGET_HOST, "pulseexchange", "pulseexchange_app", "fixture"), "offline-run")
        tables = [{"schema": "public", "name": "alembic_version", "owner": "pulseexchange_app"},
                  {"schema": 'quoted"schema', "name": 'quoted"table', "owner": "pulseexchange_app"}]
        with patch.object(pg, "json", return_value=tables) as inventory, patch.object(pg, "sql") as execute:
            pg.analyze_user_tables()
        self.assertIn(m.USER_SCHEMA, inventory.call_args.args[0])
        self.assertIn("c.relkind IN ('r','m')", inventory.call_args.args[0])
        execute.assert_called_once_with('ANALYZE "public"."alembic_version";\nANALYZE "quoted""schema"."quoted""table";', readonly=False)

    def test_statistics_update_refuses_wrong_owner_or_empty_inventory_before_write(self):
        pg = m.Pg(m.Database(m.TARGET_HOST, "pulseexchange", "pulseexchange_app", "fixture"), "offline-run")
        for tables in ([], [{"schema": "public", "name": "unexpected", "owner": "another_role"}]):
            with self.subTest(tables=tables), patch.object(pg, "json", return_value=tables), patch.object(pg, "sql") as execute:
                with self.assertRaises(m.SafeError):
                    pg.analyze_user_tables()
                execute.assert_not_called()

    def test_all_copy_paths_use_scoped_analysis_and_complete_both_fixtures(self):
        for relative in ("infra/azure-shared-postgres/migrate.py", "infra/azure-shared-postgres/economy_migrate.py",
                         "tests/shared_postgres/integration_sql.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn('.sql("ANALYZE;"', source)
            self.assertIn(".analyze_user_tables()", source)
        source = (ROOT / "tests/shared_postgres/integration_sql.py").read_text(encoding="utf-8")
        self.assertLess(source.index("self.assertEqual(len(final_apps), 2"), source.index("for app in final_apps:"))
        self.assertGreater(source.index("final_apps.append(final_app)"), source.index('self.assertEqual(final_app.json("SELECT rolconnlimit'))

    def test_major_version_is_enforced_for_all_clients(self):
        with patch.object(m, "native", return_value=b"pg_dump (PostgreSQL) 17.6\n") as native:
            m.require_clients()
        self.assertEqual(native.call_count, 3)
        with patch.object(m, "native", return_value=b"pg_dump (PostgreSQL) 16.9\n"), self.assertRaises(m.SafeError):
            m.require_clients()

    def test_creation_is_new_only_password_in_environment_and_no_global_reassign(self):
        pg = m.Pg(m.Database.parse(TARGET_URL, source=False), "offline-run")
        with patch.object(pg, "json", return_value=0), patch.object(m, "native") as native:
            pg.create_target("pulseexchange", "pulseexchange_app", ENV["TARGET_APP_PASSWORD"],
                             {"collate": "en_US.utf8", "ctype": "en_US.utf8"})
        statements = [call.kwargs["sql"] for call in native.call_args_list]
        self.assertIn("SET LOCAL createrole_self_grant='set'", statements[0])
        self.assertIn("BEGIN;", statements[0])
        self.assertIn("PASSWORD :'migration_password'", statements[0])
        self.assertIn("NOCREATEDB NOCREATEROLE", statements[0])
        self.assertIn("FROM PUBLIC", statements[2])
        self.assertIn('SET LOCAL ROLE "pulseexchange_app";', statements[2])
        self.assertLess(statements[2].index("SET LOCAL ROLE"), statements[2].index("REVOKE CONNECT"))
        for call in native.call_args_list:
            self.assertNotIn(ENV["TARGET_APP_PASSWORD"], str(call.args[0]) + call.kwargs["sql"])
            self.assertNotIn("REASSIGN OWNED", call.kwargs["sql"])
            self.assertNotIn("DROP DATABASE", call.kwargs["sql"])

    def test_existing_target_role_or_database_always_refused(self):
        pg = m.Pg(m.Database.parse(TARGET_URL, source=False), "offline-run")
        with patch.object(pg, "json", return_value=1), patch.object(pg, "sql") as sql, self.assertRaises(m.SafeError):
            pg.create_target("pulseexchange", "pulseexchange_app", "secret", {})
        sql.assert_not_called()

    def test_manifest_uses_imported_snapshot_fixed_order_and_no_raw_row_result(self):
        pg = m.Pg(m.Database.parse(SOURCE_URL, source=True), "offline-run")
        replies = [0, [{"schema": "public", "name": "alembic_version", "kind": "r"}],
                   [["version_num", "text", True]], [], ["revision"]]
        queries = []

        def fake_sql(query, **options):
            queries.append((query, options))
            options["output"].write(b"{\"version_num\": \"revision\"}\n")

        with tempfile.TemporaryDirectory() as temporary, patch.object(pg, "json", side_effect=replies), patch.object(pg, "sql", side_effect=fake_sql):
            actual = pg.manifest("00000001-00000001-1", Path(temporary))
            self.assertFalse((Path(temporary) / "rows.private").exists())
        self.assertEqual(actual["tables"][0]["rows"], 1)
        self.assertEqual(len(actual["tables"][0]["sha256"]), 64)
        self.assertIn('COLLATE "C"', queries[0][0])
        self.assertEqual(queries[0][1]["snapshot"], "00000001-00000001-1")

    def test_sequence_mismatch_never_passes_final_validation(self):
        changed = copy.deepcopy(MANIFEST)
        changed["sequences"][0]["last_value"] += 1
        with self.assertRaises(m.SafeError):
            m.require_same(MANIFEST, changed)
        m.require_same(MANIFEST, changed, sequences=False)
        changed["tables"][0]["sha256"] = "b" * 64
        with self.assertRaises(m.SafeError):
            m.require_same(MANIFEST, changed, sequences=False)


class BlobTests(unittest.TestCase):
    def test_privacy_uses_container_properties_not_metadata(self):
        blob = m.PrivateBlob(ENV, ENV["MIGRATION_RUN_ID"])
        seen = []

        @contextmanager
        def request(method, suffix):
            seen.append((method, suffix))
            yield type("Response", (), {"headers": {}})()

        with patch.object(blob, "request", side_effect=request):
            blob.require_private()
        self.assertEqual(seen, [("GET", "?restype=container")])

    def test_public_backup_is_rejected(self):
        blob = m.PrivateBlob(ENV, ENV["MIGRATION_RUN_ID"])

        @contextmanager
        def request(*args):
            yield type("Response", (), {"headers": {"x-ms-blob-public-access": "blob"}})()

        with patch.object(blob, "request", side_effect=request), self.assertRaises(m.SafeError):
            blob.require_private()

    def test_identity_endpoint_cannot_be_external(self):
        blob = m.PrivateBlob({**ENV, "IDENTITY_ENDPOINT": "https://attacker.example/token",
                              "IDENTITY_HEADER": "private header"}, ENV["MIGRATION_RUN_ID"])
        with patch.object(m, "urlopen") as request, self.assertRaises(m.SafeError):
            blob.token()
        request.assert_not_called()

    def test_upload_is_non_overwriting_and_checksum_verified(self):
        blob = m.PrivateBlob(ENV, ENV["MIGRATION_RUN_ID"])
        put_headers = {}

        @contextmanager
        def request(method, suffix, **options):
            if method == "PUT":
                put_headers.update(options["headers"])
                self.assertEqual(options["data"].read(), b"private dump fixture")
            yield type("Response", (), {"headers": put_headers})()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dump"
            path.write_bytes(b"private dump fixture")
            with patch.object(blob, "request", side_effect=request):
                digest = blob.upload(path, "pulseexchange.dump")
        self.assertEqual(put_headers["If-None-Match"], "*")
        self.assertEqual(put_headers["x-ms-meta-sha256"], digest)
        self.assertIn("Content-MD5", put_headers)

    def test_download_checks_bytes_and_removes_mismatched_partial_copy(self):
        blob = m.PrivateBlob(ENV, ENV["MIGRATION_RUN_ID"])
        original = b"stored dump fixture"
        digest = hashlib.sha256(original).hexdigest()
        for data, header, should_fail in ((original, len(original), False),
                                          (b"x" * len(original), len(original), True),
                                          (original[:-1], len(original), True),
                                          (original + b"extra", len(original), True),
                                          (original, 0, True)):
            with self.subTest(data=data, header=header), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "verified.dump"

                @contextmanager
                def request(method, suffix):
                    self.assertEqual(method, "GET")
                    self.assertTrue(suffix.endswith("/pulseexchange.dump"))
                    response = io.BytesIO(data)
                    response.headers = {"Content-Length": str(header)}
                    yield response

                with patch.object(blob, "request", side_effect=request):
                    if should_fail:
                        with self.assertRaises(m.SafeError):
                            blob.download_dump(path, expected_size=len(original), expected_sha256=digest)
                        self.assertFalse(path.exists())
                    else:
                        blob.download_dump(path, expected_size=len(original), expected_sha256=digest)
                        self.assertEqual(path.read_bytes(), original)

    def test_download_rejects_unbounded_manifest_without_network_access(self):
        blob = m.PrivateBlob(ENV, ENV["MIGRATION_RUN_ID"])
        invalid = ((0, "a" * 64), (m.MAX_DUMP_BYTES + 1, "a" * 64),
                   (True, "a" * 64), (8, "invalid"), (8, None))
        for length, digest in invalid:
            with self.subTest(length=length, digest=digest), patch.object(blob, "request") as request:
                with self.assertRaises(m.SafeError):
                    blob.download_dump(Path("not-created.dump"), expected_size=length, expected_sha256=digest)
                request.assert_not_called()


class PhaseTests(unittest.TestCase):
    def invoke(self, mode, *, extra=None, existing=False, upload_error=False,
               download_error=False, manifest_error=False, drift=False):
        events, saved = [], {}
        case = self

        class FakePg:
            def __init__(self, database, run_id):
                self.database = database

            def require_version(self):
                events.append("version")

            def locale(self):
                return {"encoding": "UTF8", "provider": "c", "collate": "en_US.utf8", "ctype": "en_US.utf8"}

            def require_frozen(self):
                events.append("frozen")

            @contextmanager
            def snapshot(self):
                yield "00000001-00000001-1"

            def manifest(self, *args):
                result = copy.deepcopy(MANIFEST)
                if drift and self.database.host == m.TARGET_HOST:
                    result["tables"][0]["rows"] += 1
                return result

            def require_absent(self, *args):
                events.append("absent")
                if existing:
                    raise m.SafeError("Target exists.")

            def create_target(self, database, role, *args):
                events.append("create")
                case.assertIn((database, role), m.TARGETS.values())

            def analyze_user_tables(self):
                events.append("analyze")

            def json(self, query):
                return [] if "datname IN" in query else {"max_connections": 50, "current_connections": 8}

        class FakeBlob:
            def __init__(self, *args):
                pass

            def require_private(self):
                events.append("private")

            def upload(self, path, name):
                events.append("upload:" + name)
                if upload_error:
                    raise m.SafeError("Upload failed.")
                if name == "manifest.json":
                    saved.update(json.loads(path.read_text()))
                return "a" * 64

            def manifest(self):
                events.append("download:manifest.json")
                if saved:
                    result = copy.deepcopy(saved)
                    if manifest_error:
                        result["dump_bytes"] += 1
                    return result
                kind = (extra or {}).get("VERIFY_TARGET", "final")
                return {"format": 1, "run_id": ENV["MIGRATION_RUN_ID"], "source_host": m.SOURCE_HOST,
                        "source_database": m.SOURCE_DB, "target_host": m.TARGET_HOST,
                        "target_database": m.TARGETS[kind][0], "mode": "FinalCopy" if kind == "final" else "Rehearse",
                        "source_manifest": MANIFEST, "dump_bytes": 12,
                        "dump_sha256": "a" * 64}

            def download_dump(self, path, *, expected_size, expected_sha256):
                events.append("download:pulseexchange.dump")
                if download_error:
                    raise m.SafeError("Downloaded bytes differ.")
                case.assertEqual(expected_size, 12)
                case.assertEqual(expected_sha256, "a" * 64)
                path.write_bytes(b"dump fixture")

        def native(arguments, environment, **options):
            self.assertNotIn(environment["PGPASSWORD"], str(arguments))
            if arguments[0] == "pg_dump":
                events.append("dump")
                self.assertIn("--format=custom", arguments)
                self.assertIn("default_transaction_read_only=on", environment["PGOPTIONS"])
                Path(next(arg.removeprefix("--file=") for arg in arguments if arg.startswith("--file="))).write_bytes(b"dump fixture")
            else:
                self.assertEqual(arguments[0], "pg_restore")
                events.append("restore")
                for option in ("--single-transaction", "--no-owner", "--no-acl", "--exit-on-error", "--no-tablespaces"):
                    self.assertIn(option, arguments)
                self.assertNotIn("--clean", arguments)
                self.assertNotIn("--create", arguments)
                self.assertEqual(Path(arguments[-1]).name, "verified-pulseexchange.dump")
                self.assertEqual(Path(arguments[-1]).read_bytes(), b"dump fixture")
                self.assertIn(environment["PGUSER"], ["pulseexchange_app", "pulseexchange_rehearsal_app"])

        with patch.object(m, "require_clients"), patch.object(m, "Pg", FakePg), patch.object(m, "PrivateBlob", FakeBlob), patch.object(m, "native", side_effect=native):
            try:
                result = m.run(mode, {**ENV, **(extra or {})})
                failure = None
            except m.SafeError as error:
                result, failure = None, error
        return result, failure, events, saved

    def test_final_backup_precedes_all_target_changes(self):
        result, failure, events, saved = self.invoke("FinalCopy")
        self.assertIsNone(failure)
        self.assertTrue(result["verified"])
        self.assertTrue(result["sequences_verified"])
        self.assertTrue(result["backup_download_verified"])
        self.assertFalse(result["cutover_performed"])
        self.assertLess(events.index("upload:manifest.json"), events.index("create"))
        self.assertLess(events.index("download:manifest.json"), events.index("create"))
        self.assertLess(events.index("download:pulseexchange.dump"), events.index("create"))
        self.assertLess(events.index("create"), events.index("restore"))
        self.assertGreaterEqual(events.count("frozen"), 4)
        self.assertEqual(saved["source_manifest"], MANIFEST)
        self.assertNotIn("private", json.dumps(saved))

    def test_no_acknowledgment_no_target_write(self):
        _, failure, events, _ = self.invoke("FinalCopy", extra={"WRITERS_FROZEN": "false"})
        self.assertIsNotNone(failure)
        self.assertNotIn("create", events)
        self.assertNotIn("dump", events)

    def test_existing_target_and_failed_backup_stop_without_target_changes(self):
        for options in ({"existing": True}, {"upload_error": True},
                        {"download_error": True}, {"manifest_error": True}):
            with self.subTest(options=options):
                _, failure, events, _ = self.invoke("FinalCopy", **options)
                self.assertIsNotNone(failure)
                self.assertNotIn("create", events)
                self.assertNotIn("restore", events)

    def test_rehearsal_never_targets_final_database(self):
        result, failure, events, saved = self.invoke("Rehearse", extra={"WRITERS_FROZEN": "false"})
        self.assertIsNone(failure)
        self.assertEqual(result["target_database"], "pulseexchange_rehearsal")
        self.assertEqual(saved["target_database"], "pulseexchange_rehearsal")
        self.assertFalse(result["sequences_verified"])
        self.assertNotIn("frozen", events)

    def test_failed_verification_does_not_claim_success_or_delete(self):
        result, failure, events, _ = self.invoke("FinalCopy", drift=True)
        self.assertIsNone(result)
        self.assertIsNotNone(failure)
        self.assertEqual(events.count("create"), 1)
        self.assertEqual(events.count("restore"), 1)

    def test_independent_verify_has_no_dump_upload_or_create(self):
        for target in ("final", "rehearsal"):
            result, failure, events, _ = self.invoke("Verify", extra={"VERIFY_TARGET": target})
            self.assertIsNone(failure)
            self.assertTrue(result["verified"])
            self.assertTrue(result["backup_download_verified"])
            self.assertIn("download:pulseexchange.dump", events)
            self.assertNotIn("create", events)
            self.assertNotIn("restore", events)
            self.assertNotIn("dump", events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
