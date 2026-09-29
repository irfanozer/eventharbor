#!/usr/bin/env python3
"""Guarded copies from either original app into the approved new shared server.

The interim old-server helper remains unchanged. FinalCopy accepts only an empty
foundation database owned by portfolio_admin and an absent application role.
There is no overwrite, cleanup, database drop, consumer update, or DNS cutover.
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

import migrate as m


# Pinned to the reviewed new foundation resource. The operator must independently
# match the successful ARM deployment output before creating or starting a job.
APPROVED_TARGET_HOST = "psql-demos-economy-56sjj5b3bl7ro.postgres.database.azure.com"
FOUNDATION_OWNER = "portfolio_admin"
SOURCES = {
    "eventharbor": "psql-eventharbor-prod-dodczqz7eaeps.postgres.database.azure.com",
    "pulseexchange": "psql-pulseexchange-prod-kj25jhmdlk6ik.postgres.database.azure.com",
}
phase = "configuration"
USER_NAMESPACE = "n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' AND n.nspname<>'information_schema'"


def parse_database(value, project, *, source):
    if project not in SOURCES or not APPROVED_TARGET_HOST:
        raise m.SafeError("Project and exact deployed target host must be approved before using this helper.")
    try:
        url = urlsplit(value)
        host = SOURCES[project] if source else APPROVED_TARGET_HOST
        names = {project} if source else {"postgres", project}
        query = parse_qs(url.query, keep_blank_values=True)
        if (url.scheme not in {"postgresql", "postgresql+asyncpg"} or url.hostname != host
                or url.port not in {None, 5432} or url.path.removeprefix("/") not in names
                or not url.username or not url.password or url.fragment or set(query) - {"ssl"}
                or ("ssl" in query and query["ssl"] not in [["require"], ["verify-full"]])):
            raise ValueError()
        user, password = unquote(url.username), unquote(url.password)
        if any(char in user + password for char in "\0\r\n") or (not source and user != FOUNDATION_OWNER):
            raise ValueError()
        return m.Database(host, project if source else "postgres", user, password)
    except (ValueError, TypeError):
        raise m.SafeError("Database URL does not match the fixed project, server, administrator and TLS scope.") from None


class EconomyPg(m.Pg):
    def require_frozen(self):
        count = self.json("SELECT count(*) FROM pg_stat_activity WHERE datname=" + m.literal(self.database.name)
                          + " AND pid<>pg_backend_pid() AND application_name IS DISTINCT FROM " + m.literal("shared-pg-" + self.run_id) + ";")
        if count:
            raise m.SafeError("Source still has other sessions. Freeze all APIs, workers and scheduled/manual jobs first.")

    def empty_foundation(self, database, role, locale):
        if database not in SOURCES or role != database + "_app":
            raise m.SafeError("Only the exact two foundation database/application-role pairs are supported.")
        target = EconomyPg(replace(self.database, name=database), self.run_id)
        target.require_version()
        metadata = target.json("SELECT json_build_object('database',current_database(),'user',current_user,"
                               "'owner',(SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()),"
                               "'schemas',(SELECT coalesce(json_agg(nspname ORDER BY nspname),'[]'::json) FROM pg_namespace n WHERE " + USER_NAMESPACE + "),"
                               "'public_owner',(SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'),"
                               "'public_owner_access',(SELECT pg_has_role(current_user,nspowner,'USAGE') FROM pg_namespace WHERE nspname='public'),"
                               "'relations',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE " + USER_NAMESPACE + "),"
                               "'types',(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE " + USER_NAMESPACE + "),"
                               "'routines',(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE " + USER_NAMESPACE + "),"
                               "'extensions',(SELECT coalesce(json_agg(extname ORDER BY extname),'[]'::json) FROM pg_extension),"
                               "'large_objects',(SELECT count(*) FROM pg_largeobject_metadata),"
                               "'event_triggers',(SELECT count(*) FROM pg_event_trigger),"
                               "'role_exists',EXISTS(SELECT 1 FROM pg_roles WHERE rolname=" + m.literal(role) + "));")
        if (metadata["database"] != database or metadata["user"] != FOUNDATION_OWNER
                or metadata["owner"] != FOUNDATION_OWNER or metadata["schemas"] != ["public"]
                or metadata["public_owner"] not in {FOUNDATION_OWNER, "pg_database_owner", "azure_pg_admin"}
                or metadata.get("public_owner_access") is not True
                or metadata["extensions"] != ["plpgsql"] or metadata["role_exists"]
                or any(metadata[key] for key in ("relations", "types", "routines", "large_objects", "event_triggers"))):
            raise m.SafeError("Destination is not an untouched empty foundation database with the exact owner and absent app role.")
        if target.locale() != locale:
            raise m.SafeError("The empty foundation database locale/encoding differs from the source. Do not overwrite or recreate it automatically.")
        active = target.json("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid();")
        if active:
            raise m.SafeError("The empty destination has other sessions. Do not initialize or start the application yet.")
        return target

    def claim_empty_foundation(self, database, role, password, locale):
        target = self.empty_foundation(database, role, locale)
        sql = "\\getenv migration_password MIGRATION_NEW_PASSWORD\nBEGIN;\nSET LOCAL createrole_self_grant='set';\n"
        sql += "SELECT pg_advisory_xact_lock(170929, " + ("1" if database == "eventharbor" else "2") + ");\n"
        sql += "DO $guard$ BEGIN\nIF current_database()<>" + m.literal(database) + " OR current_user<>'portfolio_admin' "
        sql += "OR (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database())<>'portfolio_admin' "
        sql += "OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname=" + m.literal(role) + ") "
        sql += "OR (SELECT count(*) FROM pg_namespace n WHERE " + USER_NAMESPACE + ")<>1 "
        sql += "OR NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public') "
        sql += "OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public' AND (pg_get_userbyid(nspowner) NOT IN ('portfolio_admin','pg_database_owner','azure_pg_admin') OR NOT pg_has_role(current_user,nspowner,'USAGE'))) "
        for catalog, alias, namespace in (("pg_class", "c", "relnamespace"), ("pg_type", "t", "typnamespace"), ("pg_proc", "p", "pronamespace")):
            sql += "OR EXISTS(SELECT 1 FROM " + catalog + " " + alias + " JOIN pg_namespace n ON n.oid=" + alias + "." + namespace + " WHERE " + USER_NAMESPACE + ") "
        sql += "OR EXISTS(SELECT 1 FROM pg_extension WHERE extname<>'plpgsql') "
        sql += "OR EXISTS(SELECT 1 FROM pg_largeobject_metadata) OR EXISTS(SELECT 1 FROM pg_event_trigger) "
        sql += "OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()) "
        sql += "THEN RAISE EXCEPTION 'Empty foundation target guard failed'; END IF; END $guard$;\n"
        sql += "CREATE ROLE " + m.ident(role) + " LOGIN PASSWORD :'migration_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12;\n"
        sql += "GRANT CREATE ON DATABASE " + m.ident(database) + " TO " + m.ident(role) + ";\n"
        sql += "ALTER SCHEMA public OWNER TO " + m.ident(role) + ";\n"
        sql += "ALTER DATABASE " + m.ident(database) + " OWNER TO " + m.ident(role) + ";\nSET LOCAL ROLE " + m.ident(role) + ";\n"
        sql += "REVOKE CONNECT,TEMPORARY ON DATABASE " + m.ident(database) + " FROM PUBLIC;\n"
        sql += "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
        sql += "ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM PUBLIC;\nALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM PUBLIC;\n"
        sql += "ALTER DEFAULT PRIVILEGES REVOKE ALL ON ROUTINES FROM PUBLIC;\nALTER DEFAULT PRIVILEGES REVOKE ALL ON TYPES FROM PUBLIC;\n"
        sql += "COMMENT ON DATABASE " + m.ident(database) + " IS " + m.literal("economy-copy:" + self.run_id) + ";\nCOMMIT;"
        target.sql(sql, readonly=False, extra_env={"MIGRATION_NEW_PASSWORD": password})


class EconomyBlob(m.PrivateBlob):
    def __init__(self, environment, run_id, project):
        if project not in SOURCES:
            raise m.SafeError("Unknown backup project.")
        self.project = project
        super().__init__(environment, run_id)

    def suffix(self, name):
        # The existing verified downloader supplies pulseexchange.dump. Adapt
        # that fixed internal name to this allowlisted project's dump only.
        if name == "pulseexchange.dump":
            name = self.project + ".dump"
        if name not in {self.project + ".dump", "manifest.json"}:
            raise m.SafeError("Unexpected project backup filename.")
        return "/economy-postgres/" + self.project + "/" + self.run_id + "/" + name


def run(mode, environment):
    global phase
    project = environment.get("MIGRATION_PROJECT", "")
    run_id = environment.get("MIGRATION_RUN_ID", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", run_id) or mode not in {"Inspect", "Rehearse", "FinalCopy", "Verify"}:
        raise m.SafeError("A valid migration mode and unique run identifier are required.")
    source = EconomyPg(parse_database(environment.get("SOURCE_DATABASE_URL", ""), project, source=True), run_id)
    admin = EconomyPg(parse_database(environment.get("TARGET_ADMIN_DATABASE_URL", ""), project, source=False), run_id)
    m.require_clients()
    phase = "approved target inspection"
    admin.require_version()
    final = mode == "FinalCopy" or (mode == "Verify" and environment.get("VERIFY_TARGET", "final") == "final")
    kind = environment.get("VERIFY_TARGET", "final") if mode == "Verify" else "final" if final else "rehearsal"
    if kind not in {"final", "rehearsal"}:
        raise m.SafeError("VERIFY_TARGET must be final or rehearsal.")
    database = project if kind == "final" else project + "_rehearsal"
    role = database + "_app"
    with tempfile.TemporaryDirectory(prefix="economy-copy-") as temporary:
        directory = Path(temporary)
        if mode != "Verify":
            source.require_version()
            locale = source.locale()
        if mode == "Inspect":
            admin.empty_foundation(project, project + "_app", locale)
            with source.snapshot() as snapshot:
                manifest = source.manifest(snapshot, directory)
            return {"mode": mode, "project": project, "source_manifest": manifest,
                    "target_host": APPROVED_TARGET_HOST, "empty_foundation_verified": True, "changed": False}
        password = m.new_password(environment, "TARGET_APP_PASSWORD" if kind == "final" else "REHEARSAL_APP_PASSWORD")
        target = EconomyPg(replace(admin.database, name=database, user=role, password=password), run_id)
        blob = EconomyBlob(environment, run_id, project)
        phase = "private backup validation"
        blob.require_private()
        if mode == "Verify":
            record = blob.manifest()
            identity = {"format": 2, "project": project, "run_id": run_id, "source_host": SOURCES[project],
                        "source_database": project, "target_host": APPROVED_TARGET_HOST, "target_database": database,
                        "mode": "FinalCopy" if final else "Rehearse"}
            if any(record.get(key) != value for key, value in identity.items()):
                raise m.SafeError("Backup manifest does not belong to this exact project, run and approved destination.")
            blob.download_dump(directory / "verified.dump", expected_size=record.get("dump_bytes"), expected_sha256=record.get("dump_sha256"))
            target.require_version()
            with target.snapshot() as snapshot:
                m.require_same(record["source_manifest"], target.manifest(snapshot, directory), sequences=final)
            return {"mode": mode, "project": project, "verified": True, "backup_download_verified": True,
                    "sequences_verified": final, "changed": False}
        if final and environment.get("WRITERS_FROZEN") != "true":
            raise m.SafeError("FinalCopy requires WRITERS_FROZEN=true for this project's complete writer freeze.")
        phase = "destination and source safety checks"
        if final:
            admin.empty_foundation(database, role, locale)
            source.require_frozen()
        else:
            admin.require_absent(database, role)
        dump = directory / (project + ".dump")
        phase = "consistent source backup"
        with source.snapshot() as snapshot:
            expected = source.manifest(snapshot, directory)
            m.native(["pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-acl",
                      "--snapshot=" + snapshot, "--file=" + str(dump)], source.database.env(run_id, readonly=True))
            if not dump.is_file() or not 0 < dump.stat().st_size <= m.MAX_DUMP_BYTES:
                raise m.SafeError("Source backup is empty or exceeds the reviewed size limit.")
            if final:
                source.require_frozen()
                m.require_same(expected, source.manifest(snapshot, directory))
        if final:
            with source.snapshot() as snapshot:
                m.require_same(expected, source.manifest(snapshot, directory))
            source.require_frozen()
        phase = "durable backup round-trip verification"
        checksum = blob.upload(dump, project + ".dump")
        record = {"format": 2, "project": project, "run_id": run_id, "mode": mode,
                  "created_utc": datetime.now(timezone.utc).isoformat(), "source_host": SOURCES[project],
                  "source_database": project, "target_host": APPROVED_TARGET_HOST, "target_database": database,
                  "dump_sha256": checksum, "dump_bytes": dump.stat().st_size, "source_manifest": expected}
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        blob.upload(manifest_path, "manifest.json")
        if blob.manifest() != record:
            raise m.SafeError("Stored manifest differs from the verified local backup record.")
        downloaded = directory / "verified.dump"
        blob.download_dump(downloaded, expected_size=record["dump_bytes"], expected_sha256=checksum)
        phase = "isolated target preparation"
        if final:
            source.require_frozen()
            admin.claim_empty_foundation(database, role, password, locale)
        else:
            admin.create_target(database, role, password, locale)
        phase = "single transaction restore"
        m.native(["pg_restore", "--no-password", "--no-owner", "--no-acl", "--no-tablespaces",
                  "--exit-on-error", "--single-transaction", "--dbname=" + database, str(downloaded)], target.database.env(run_id))
        target.analyze_user_tables()
        with target.snapshot() as snapshot:
            m.require_same(expected, target.manifest(snapshot, directory), sequences=final)
        if final:
            source.require_frozen()
            with source.snapshot() as snapshot:
                m.require_same(expected, source.manifest(snapshot, directory))
        return {"mode": mode, "project": project, "verified": True, "target_database": database,
                "backup_download_verified": True, "sequences_verified": final, "source_modified_by_helper": False,
                "backup_prefix": "economy-postgres/" + project + "/" + run_id + "/", "dump_sha256": checksum,
                "tables": len(expected["tables"]), "rows": sum(item["rows"] for item in expected["tables"]),
                "cutover_performed": False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("Inspect", "Rehearse", "FinalCopy", "Verify"))
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.mode, os.environ), sort_keys=True))
    except m.SafeError as error:
        print("Economy migration stopped during " + phase + ": " + str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Economy migration stopped during " + phase + ". Diagnostics withheld. No automatic cleanup or cutover attempted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
