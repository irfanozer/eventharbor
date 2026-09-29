#!/usr/bin/env python3
"""Narrow, fail-closed PostgreSQL 17 copy job. No cutover or database deletion."""

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import formatdate
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SOURCE_HOST = "psql-pulseexchange-prod-kj25jhmdlk6ik.postgres.database.azure.com"
TARGET_HOST = "psql-eventharbor-prod-dodczqz7eaeps.postgres.database.azure.com"
SOURCE_DB = "pulseexchange"
TARGETS = {"final": ("pulseexchange", "pulseexchange_app"),
           "rehearsal": ("pulseexchange_rehearsal", "pulseexchange_rehearsal_app")}
CERTIFICATES = "/etc/ssl/certs/ca-certificates.crt"
MAX_DUMP_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
PSQL = ["psql", "-X", "--no-password", "--quiet", "--tuples-only", "--no-align",
        "--set", "ON_ERROR_STOP=1", "--set", "VERBOSITY=sqlstate"]
USER_SCHEMA = "n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' AND n.nspname <> 'information_schema'"
phase = "configuration"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def urlopen(request, *, timeout):
    # Never forward a managed identity token or identity header through redirects.
    return build_opener(NoRedirect()).open(request, timeout=timeout)


class SafeError(Exception):
    """Messages are static and safe for job logs; never wrap raw exceptions."""


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class Database:
    host: str
    name: str
    user: str
    password: str

    @classmethod
    def parse(cls, value, *, source):
        try:
            url = urlsplit(value)
            expected = SOURCE_HOST if source else TARGET_HOST
            allowed_databases = {SOURCE_DB} if source else {"eventharbor", "postgres"}
            query = parse_qs(url.query, keep_blank_values=True)
            if (url.scheme not in {"postgresql", "postgresql+asyncpg"}
                    or url.hostname != expected or url.port not in {None, 5432}
                    or url.fragment or url.path.removeprefix("/") not in allowed_databases
                    or not url.username or not url.password
                    or set(query) - {"ssl"}
                    or ("ssl" in query and query["ssl"] not in [["require"], ["verify-full"]])):
                raise ValueError()
            user, password = unquote(url.username), unquote(url.password)
            if any(char in user + password for char in "\0\r\n"):
                raise ValueError()
            return cls(expected, SOURCE_DB if source else "postgres", user, password)
        except (TypeError, ValueError):
            raise SafeError("Database URL does not match the fixed migration endpoints and safe TLS settings.") from None

    def env(self, run_id, *, readonly=False):
        # Do not inherit other libpq options, service files or application secrets.
        result = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                  "LANG": "C.UTF-8", "PGHOST": self.host,
                  "PGPORT": "5432", "PGDATABASE": self.name, "PGUSER": self.user,
                  "PGPASSWORD": self.password, "PGSSLMODE": "verify-full",
                  "PGSSLROOTCERT": CERTIFICATES, "PGCONNECT_TIMEOUT": "15",
                  "PGCLIENTENCODING": "UTF8", "PGAPPNAME": "shared-pg-" + run_id,
                  "PGOPTIONS": "-c TimeZone=UTC -c DateStyle=ISO -c IntervalStyle=postgres "
                               "-c extra_float_digits=3 -c standard_conforming_strings=on "
                               "-c client_min_messages=warning -c statement_timeout=600000 "
                               "-c lock_timeout=15000"}
        if readonly:
            result["PGOPTIONS"] += " -c default_transaction_read_only=on"
        return result


def native(arguments, environment, *, sql=None, output=None, timeout=600):
    try:
        result = subprocess.run(arguments, input=sql, text=sql is not None,
                                env=environment, stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SafeError("PostgreSQL client failed or timed out; diagnostics are withheld.") from None
    if result.returncode != 0 or result.stderr:
        raise SafeError("PostgreSQL client reported an error or warning (exit code "
                        + str(result.returncode) + "); diagnostics are withheld.")
    return result.stdout


def require_clients():
    for program in ("psql", "pg_dump", "pg_restore"):
        value = native([program, "--version"], {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}, timeout=15)
        if not re.search(rb"\(PostgreSQL\) 17(?:\.|\s)", value):
            raise SafeError("All PostgreSQL client programs must have major version 17.")


class Pg:
    def __init__(self, database, run_id):
        self.database, self.run_id = database, run_id

    def sql(self, query, *, readonly=True, snapshot=None, extra_env=None, output=None):
        environment = self.database.env(self.run_id, readonly=readonly)
        if extra_env:
            environment.update(extra_env)
        if snapshot:
            if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot):
                raise SafeError("Invalid exported snapshot identifier.")
            query = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSET TRANSACTION SNAPSHOT " + literal(snapshot) + ";\n" + query + "\nROLLBACK;"
        return native(PSQL, environment, sql=query, output=output)

    def json(self, query, *, snapshot=None):
        try:
            return json.loads(self.sql(query, snapshot=snapshot))
        except (ValueError, TypeError):
            raise SafeError("PostgreSQL returned an unexpected result; raw output is withheld.") from None

    def require_version(self):
        version = self.json("SELECT current_setting('server_version_num')::integer;")
        if not 170000 <= version < 180000:
            raise SafeError("Both servers must run PostgreSQL major version 17.")

    def require_frozen(self):
        count = self.json("SELECT count(*) FROM pg_stat_activity WHERE datname = "
                         + literal(SOURCE_DB) + " AND pid <> pg_backend_pid() AND application_name <> "
                         + literal("shared-pg-" + self.run_id) + ";")
        if count:
            raise SafeError("Source still has other database sessions. Stop API, processor, maintenance and migration clients first.")

    @contextmanager
    def snapshot(self):
        # One idle read-only transaction keeps the same snapshot available to
        # pg_dump and every manifest query. Never hold a shell or a password arg.
        process = subprocess.Popen(PSQL, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True,
                                   env=self.database.env(self.run_id, readonly=True))
        try:
            process.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                                "SET LOCAL idle_in_transaction_session_timeout = '30min';\n"
                                "SELECT pg_export_snapshot();\n")
            process.stdin.flush()
            values = queue.Queue()
            threading.Thread(target=lambda: values.put(process.stdout.readline()), daemon=True).start()
            try:
                value = values.get(timeout=30).strip()
            except queue.Empty:
                raise SafeError("Timed out exporting a consistent database snapshot.") from None
            if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", value):
                raise SafeError("Could not export a consistent database snapshot.")
            yield value
            if process.poll() is not None:
                raise SafeError("The snapshot keeper ended before validation completed.")
        finally:
            if process.poll() is None:
                try:
                    process.stdin.write("ROLLBACK;\n\\q\n")
                    process.stdin.flush()
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()

    def locale(self):
        value = self.json("SELECT json_build_object('encoding', pg_encoding_to_char(encoding), "
                          "'collate', datcollate, 'ctype', datctype, 'provider', datlocprovider) "
                          "FROM pg_database WHERE datname = current_database();")
        if value["encoding"] != "UTF8" or value["provider"] != "c":
            raise SafeError("This helper only supports the existing UTF8/libc locale; review other locale providers separately.")
        return value

    def manifest(self, snapshot, directory):
        # Unsupported objects are blockers, not silently omitted content.
        unusual = self.json("SELECT (SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql') + "
                            "(SELECT count(*) FROM pg_largeobject_metadata) + "
                            "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE " + USER_SCHEMA + " AND c.relkind='f');", snapshot=snapshot)
        if unusual:
            raise SafeError("Extensions, large objects or foreign tables require a separately reviewed migration.")
        tables = self.json("SELECT coalesce(json_agg(json_build_object('schema', n.nspname, "
                           "'name', c.relname, 'kind', c.relkind) ORDER BY n.nspname,c.relname), '[]'::json) "
                           "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE "
                           + USER_SCHEMA + " AND c.relkind IN ('r','m');", snapshot=snapshot)
        if not any(t["schema"] == "public" and t["name"] == "alembic_version" for t in tables):
            raise SafeError("Expected public.alembic_version is missing.")
        table_values = []
        for table in tables:
            name = ident(table["schema"]) + "." + ident(table["name"])
            columns = self.json("SELECT coalesce(json_agg(json_build_array(a.attname, "
                                "format_type(a.atttypid,a.atttypmod),a.attnotnull) ORDER BY a.attnum),'[]'::json) "
                                "FROM pg_attribute a WHERE a.attrelid=" + literal(name) + "::regclass "
                                "AND a.attnum>0 AND NOT a.attisdropped;", snapshot=snapshot)
            # COPY text escapes embedded line breaks, producing one canonical
            # UTF8 line per row. C collation and session formatting are fixed.
            row_file = directory / "rows.private"
            try:
                with row_file.open("wb") as output:
                    self.sql("COPY (SELECT to_jsonb(t)::text AS payload FROM " + name + " AS t "
                             "ORDER BY (to_jsonb(t)::text) COLLATE \"C\") TO STDOUT;",
                             snapshot=snapshot, output=output)
                digest, count = hashlib.sha256(), 0
                with row_file.open("rb") as rows:
                    for row in rows:
                        digest.update(row)
                        count += 1
                table_values.append({**table, "columns": columns, "rows": count,
                                     "sha256": digest.hexdigest()})
            finally:
                row_file.unlink(missing_ok=True)
        sequences = self.json("SELECT coalesce(json_agg(json_build_array(n.nspname,c.relname) "
                              "ORDER BY n.nspname,c.relname),'[]'::json) FROM pg_class c "
                              "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE " + USER_SCHEMA
                              + " AND c.relkind='S';", snapshot=snapshot)
        sequence_values = []
        for schema, name in sequences:
            value = self.json("SELECT json_build_object('last_value',last_value,'is_called',is_called) FROM "
                              + ident(schema) + "." + ident(name) + ";", snapshot=snapshot)
            sequence_values.append({"schema": schema, "name": name, **value})
        version = self.json("SELECT coalesce(json_agg(version_num ORDER BY version_num),'[]'::json) "
                            "FROM public.alembic_version;", snapshot=snapshot)
        return {"tables": table_values, "sequences": sequence_values, "alembic_version": version}

    def require_absent(self, database, role):
        present = self.json("SELECT (SELECT count(*) FROM pg_database WHERE datname=" + literal(database)
                            + ") + (SELECT count(*) FROM pg_roles WHERE rolname=" + literal(role) + ");")
        if present:
            raise SafeError("Target database or application role already exists. Nothing will be overwritten or dropped.")

    def create_target(self, database, role, password, locale):
        self.require_absent(database, role)
        self.sql("\\getenv migration_password MIGRATION_NEW_PASSWORD\nBEGIN;\n"
                 "SET LOCAL createrole_self_grant='set';\nCREATE ROLE " + ident(role)
                 + " LOGIN PASSWORD :'migration_password' NOSUPERUSER NOCREATEDB NOCREATEROLE "
                 "NOREPLICATION NOBYPASSRLS;\nCOMMENT ON ROLE " + ident(role) + " IS "
                 + literal("shared-postgres-helper:" + self.run_id) + ";\nCOMMIT;",
                 readonly=False, extra_env={"MIGRATION_NEW_PASSWORD": password})
        self.sql("CREATE DATABASE " + ident(database) + " OWNER " + ident(role)
                 + " TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER libc LC_COLLATE "
                 + literal(locale["collate"]) + " LC_CTYPE " + literal(locale["ctype"]) + ";",
                 readonly=False)
        # Self-granted SET membership does not imply INHERIT. Enter the owner
        # role explicitly before its ownership-protected permission/comment work.
        self.sql("BEGIN;\nSET LOCAL ROLE " + ident(role) + ";\nREVOKE CONNECT,TEMPORARY ON DATABASE " + ident(database)
                 + " FROM PUBLIC;\nCOMMENT ON DATABASE " + ident(database) + " IS "
                 + literal("shared-postgres-helper:" + self.run_id) + ";\nCOMMIT;", readonly=False)


class PrivateBlob:
    def __init__(self, environment, run_id):
        self.environment = environment
        self.run_id = run_id
        self.account = environment.get("BACKUP_STORAGE_ACCOUNT", "")
        self.container = environment.get("BACKUP_CONTAINER", "")
        if not re.fullmatch(r"[a-z0-9]{3,24}", self.account) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", self.container):
            raise SafeError("A valid backup storage account and private container are required.")
        self.base = "https://" + self.account + ".blob.core.windows.net/" + self.container

    def token(self):
        endpoint = self.environment.get("IDENTITY_ENDPOINT", "")
        parts = urlsplit(endpoint)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.query or parts.fragment:
            raise SafeError("Expected the Container Apps local managed identity endpoint.")
        header = self.environment.get("IDENTITY_HEADER", "")
        if not header:
            raise SafeError("The Container Apps managed identity header is missing.")
        query = {"resource": "https://storage.azure.com/", "api-version": "2019-08-01"}
        client = self.environment.get("MIGRATION_IDENTITY_CLIENT_ID")
        if client:
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", client):
                raise SafeError("Invalid managed identity client identifier.")
            query["client_id"] = client
        request = Request(endpoint + "?" + urlencode(query), headers={"X-IDENTITY-HEADER": header})
        try:
            with urlopen(request, timeout=30) as response:
                value = json.loads(response.read(65536))["access_token"]
            if not isinstance(value, str) or not value:
                raise ValueError()
            return value
        except Exception:
            raise SafeError("Managed identity token acquisition failed; diagnostics are withheld.") from None

    @contextmanager
    def request(self, method, suffix="", *, data=None, headers=None):
        supplied = {"Authorization": "Bearer " + self.token(), "x-ms-version": "2023-11-03",
                    "x-ms-date": formatdate(usegmt=True)}
        supplied.update(headers or {})
        request = Request(self.base + suffix, data=data, method=method, headers=supplied)
        try:
            response = urlopen(request, timeout=120)
        except Exception:
            raise SafeError("Private backup storage request failed; diagnostics are withheld.") from None
        try:
            yield response
        finally:
            response.close()

    def require_private(self):
        # Container properties, not metadata: only properties returns its public
        # access level. Absence of this header on metadata proves nothing.
        with self.request("GET", "?restype=container") as response:
            if response.headers.get("x-ms-blob-public-access"):
                raise SafeError("Backup container must disable anonymous public access.")

    def suffix(self, name):
        if name not in {"pulseexchange.dump", "manifest.json"}:
            raise SafeError("Unexpected backup filename.")
        return "/shared-postgres/" + self.run_id + "/" + name

    def upload(self, path, name):
        md5, sha256 = hashlib.md5(usedforsecurity=False), hashlib.sha256()
        with path.open("rb") as contents:
            for block in iter(lambda: contents.read(1024 * 1024), b""):
                md5.update(block)
                sha256.update(block)
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Length": str(path.stat().st_size),
                   "Content-MD5": base64.b64encode(md5.digest()).decode(),
                   "Content-Type": "application/octet-stream", "If-None-Match": "*",
                   "x-ms-meta-sha256": sha256.hexdigest()}
        with path.open("rb") as contents:
            with self.request("PUT", self.suffix(name), data=contents, headers=headers):
                pass
        with self.request("HEAD", self.suffix(name)) as response:
            if (response.headers.get("Content-Length") != headers["Content-Length"]
                    or response.headers.get("Content-MD5") != headers["Content-MD5"]
                    or response.headers.get("x-ms-meta-sha256") != sha256.hexdigest()):
                raise SafeError("Uploaded backup length or checksum verification failed.")
        return sha256.hexdigest()

    def manifest(self):
        with self.request("GET", self.suffix("manifest.json")) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise SafeError("Stored backup manifest is too large.")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise SafeError("Stored backup manifest is not valid JSON.") from None

    def download_dump(self, path, *, expected_size, expected_sha256):
        # Compare actual stored bytes with the locally computed digest, not only
        # a writable metadata header. The restore must consume this download.
        if (type(expected_size) is not int or not 0 < expected_size <= MAX_DUMP_BYTES
                or not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise SafeError("Backup manifest has invalid dump size or checksum.")
        created = False
        digest, length = hashlib.sha256(), 0
        try:
            with path.open("xb") as output:
                created = True
                with self.request("GET", self.suffix("pulseexchange.dump")) as response:
                    if response.headers.get("Content-Length") != str(expected_size):
                        raise SafeError("Downloaded backup length does not match its manifest.")
                    while block := response.read(1024 * 1024):
                        length += len(block)
                        if length > expected_size:
                            raise SafeError("Downloaded backup exceeds its expected size.")
                        digest.update(block)
                        output.write(block)
            if length != expected_size or digest.hexdigest() != expected_sha256:
                raise SafeError("Downloaded backup bytes failed length or SHA256 verification.")
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise


def require_same(expected, actual, *, sequences=True):
    keys = ["tables", "alembic_version"] + (["sequences"] if sequences else [])
    if any(expected.get(key) != actual.get(key) for key in keys):
        raise SafeError("Database manifest mismatch. Keep applications stopped and inspect the private backup; no cutover is authorized.")


def new_password(environment, key):
    password = environment.get(key, "")
    if not 32 <= len(password) <= 128 or any(char in password for char in "\0\r\n"):
        raise SafeError("Supply and securely retain a new 32-128 character application password.")
    return password


def run(mode, environment):
    global phase
    run_id = environment.get("MIGRATION_RUN_ID", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", run_id):
        raise SafeError("MIGRATION_RUN_ID must be a unique 8-64 character lowercase identifier.")
    require_clients()
    source = Pg(Database.parse(environment.get("SOURCE_DATABASE_URL", ""), source=True), run_id)
    admin = Pg(Database.parse(environment.get("TARGET_ADMIN_DATABASE_URL", ""), source=False), run_id)
    phase = "server inspection"
    source.require_version()
    admin.require_version()
    locale = source.locale()
    with tempfile.TemporaryDirectory(prefix="shared-postgres-") as temporary:
        directory = Path(temporary)
        if mode == "Inspect":
            with source.snapshot() as snapshot:
                manifest = source.manifest(snapshot, directory)
            targets = admin.json("SELECT coalesce(json_agg(datname ORDER BY datname),'[]'::json) FROM pg_database "
                                 "WHERE datname IN ('pulseexchange','pulseexchange_rehearsal');")
            capacity = admin.json("SELECT json_build_object('max_connections',current_setting('max_connections')::integer,"
                                  "'current_connections',(SELECT count(*) FROM pg_stat_activity));")
            return {"mode": mode, "source_manifest": manifest, "existing_target_databases": targets,
                    "target_connections": capacity, "changed": False}
        target_kind = (environment.get("VERIFY_TARGET", "final") if mode == "Verify"
                       else "rehearsal" if mode == "Rehearse" else "final")
        if target_kind not in TARGETS:
            raise SafeError("VERIFY_TARGET must be final or rehearsal.")
        database, role = TARGETS[target_kind]
        password = new_password(environment, "REHEARSAL_APP_PASSWORD" if target_kind == "rehearsal" else "TARGET_APP_PASSWORD")
        target = Pg(replace(admin.database, name=database, user=role, password=password), run_id)
        blob = PrivateBlob(environment, run_id)
        phase = "private backup validation"
        blob.require_private()
        if mode == "Verify":
            saved = blob.manifest()
            if (saved.get("format") != 1 or saved.get("run_id") != run_id
                    or saved.get("source_host") != SOURCE_HOST or saved.get("source_database") != SOURCE_DB
                    or saved.get("target_host") != TARGET_HOST or saved.get("target_database") != database
                    or saved.get("mode") != ("FinalCopy" if target_kind == "final" else "Rehearse")):
                raise SafeError("Backup manifest does not belong to this exact migration target.")
            phase = "stored backup download verification"
            blob.download_dump(directory / "verified-pulseexchange.dump",
                               expected_size=saved.get("dump_bytes"),
                               expected_sha256=saved.get("dump_sha256"))
            phase = "independent restored data verification"
            target.require_version()
            with target.snapshot() as snapshot:
                restored = target.manifest(snapshot, directory)
            require_same(saved["source_manifest"], restored, sequences=saved["mode"] == "FinalCopy")
            return {"mode": mode, "target_database": database, "verified": True,
                    "sequences_verified": saved["mode"] == "FinalCopy",
                    "backup_download_verified": True, "changed": False}
        final = mode == "FinalCopy"
        if final and environment.get("WRITERS_FROZEN") != "true":
            raise SafeError("FinalCopy requires WRITERS_FROZEN=true after stopping all source writers and scheduled jobs.")
        phase = "empty target and writer freeze checks"
        admin.require_absent(database, role)
        if final:
            source.require_frozen()
        phase = "consistent logical backup"
        dump = directory / "pulseexchange.dump"
        with source.snapshot() as snapshot:
            expected = source.manifest(snapshot, directory)
            native(["pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-acl",
                    "--snapshot=" + snapshot, "--file=" + str(dump)],
                   source.database.env(run_id, readonly=True))
            if not dump.is_file() or not 0 < dump.stat().st_size <= MAX_DUMP_BYTES:
                raise SafeError("Backup is empty or exceeds the reviewed 256 MiB migration limit.")
            if final:
                source.require_frozen()
                require_same(expected, source.manifest(snapshot, directory))
        # A fresh snapshot detects writes committed while the dump was running.
        if final:
            with source.snapshot() as snapshot:
                require_same(expected, source.manifest(snapshot, directory))
            source.require_frozen()
        phase = "durable private backup upload"
        dump_hash = blob.upload(dump, "pulseexchange.dump")
        record = {"format": 1, "mode": mode, "run_id": run_id,
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "source_host": SOURCE_HOST, "source_database": SOURCE_DB,
                  "target_host": TARGET_HOST, "target_database": database,
                  "dump_sha256": dump_hash, "dump_bytes": dump.stat().st_size,
                  "source_manifest": expected}
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        blob.upload(manifest_path, "manifest.json")
        phase = "stored backup round-trip verification"
        if blob.manifest() != record:
            raise SafeError("Stored backup manifest does not match the locally verified copy.")
        downloaded_dump = directory / "verified-pulseexchange.dump"
        blob.download_dump(downloaded_dump, expected_size=record["dump_bytes"],
                           expected_sha256=dump_hash)
        if final:
            source.require_frozen()
        phase = "new target database creation"
        admin.create_target(database, role, password, locale)
        phase = "single transaction restore"
        native(["pg_restore", "--no-password", "--no-owner", "--no-acl", "--no-tablespaces",
                "--exit-on-error", "--single-transaction", "--dbname=" + database, str(downloaded_dump)],
               target.database.env(run_id))
        target.sql("ANALYZE;", readonly=False)
        phase = "restored data verification"
        with target.snapshot() as snapshot:
            restored = target.manifest(snapshot, directory)
        # Sequence reads are not MVCC. Rehearsal proves row/schema copy while live
        # writers may advance sequence values; only frozen final copies compare them.
        require_same(expected, restored, sequences=final)
        if final:
            source.require_frozen()
            with source.snapshot() as snapshot:
                require_same(expected, source.manifest(snapshot, directory))
        return {"mode": mode, "target_database": database, "verified": True,
                "sequences_verified": final, "source_modified_by_helper": False,
                "backup_download_verified": True,
                "tables": len(expected["tables"]), "rows": sum(t["rows"] for t in expected["tables"]),
                "backup_prefix": "shared-postgres/" + run_id + "/", "dump_sha256": dump_hash,
                "cutover_performed": False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("Inspect", "Rehearse", "FinalCopy", "Verify"))
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.mode, os.environ), sort_keys=True))
    except SafeError as error:
        print("Migration stopped during " + phase + ": " + str(error), file=sys.stderr)
        return 1
    except Exception:
        # Native/HTTP/URL/JSON exception text can include credential-bearing input.
        print("Migration stopped during " + phase + ". Diagnostics withheld; no automatic cleanup or cutover attempted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
