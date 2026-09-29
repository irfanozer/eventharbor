#!/usr/bin/env python3
"""Back up and isolate only EventHarbor's existing public-schema objects.

Inspect is read-only. Apply requires an explicit writer freeze, creates one new
database-scoped DDL-capable application role, and never changes database owner,
drops data, or reassigns objects in another database. Verify checks both actual
application logins and rejects cross-database CONNECT in both directions.
"""

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import migrate


DATABASE = "eventharbor"
ROLE = "eventharbor_app"
OTHER_DATABASE = "pulseexchange"
OTHER_ROLE = "pulseexchange_app"
phase = "configuration"

INVENTORY_SQL = """
SELECT json_build_object(
 'database', current_database(), 'current_user', current_user,
 'owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()),
 'schemas', (SELECT coalesce(json_agg(json_build_object('name',nspname,
     'owner',pg_get_userbyid(nspowner)) ORDER BY nspname),'[]'::json)
     FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' ESCAPE '\\' AND nspname<>'information_schema'),
 'extensions', (SELECT coalesce(json_agg(extname ORDER BY extname),'[]'::json) FROM pg_extension),
 'large_objects', (SELECT count(*) FROM pg_largeobject_metadata),
 'relations', (SELECT coalesce(json_agg(json_build_object('oid',c.oid,'name',c.relname,
     'kind',c.relkind,'owner',pg_get_userbyid(c.relowner)) ORDER BY c.oid),'[]'::json)
     FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'),
 'types', (SELECT coalesce(json_agg(json_build_object('oid',t.oid,'name',t.typname,
     'kind',t.typtype,'category',t.typcategory,'relation',t.typrelid,
     'element',t.typelem,'owner',pg_get_userbyid(t.typowner)) ORDER BY t.oid),'[]'::json)
     FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public'),
 'routines', (SELECT coalesce(json_agg(json_build_object('oid',p.oid,'name',p.proname,
     'kind',p.prokind,'security_definer',p.prosecdef,'owner',pg_get_userbyid(p.proowner))
     ORDER BY p.oid),'[]'::json) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
     WHERE n.nspname='public'),
 'role_exists', EXISTS(SELECT 1 FROM pg_roles WHERE rolname='eventharbor_app'));
"""


def validate_inventory(inventory, *, applying=False):
    if inventory["database"] != DATABASE:
        raise migrate.SafeError("Isolation must connect to the existing EventHarbor database.")
    if inventory["extensions"] != ["plpgsql"] or inventory["large_objects"]:
        raise migrate.SafeError("Unexpected extensions or large objects require separate review.")
    if [item["name"] for item in inventory["schemas"]] != ["public"]:
        raise migrate.SafeError("Only the existing public application schema is supported.")
    relations = inventory["relations"]
    if not any(item["name"] == "alembic_version" and item["kind"] == "r" for item in relations):
        raise migrate.SafeError("Expected EventHarbor migration-version table is missing.")
    if any(item["kind"] not in {"r", "p", "S", "v", "m", "i", "I"} for item in relations):
        raise migrate.SafeError("Unsupported public-schema relation requires separate review.")
    relation_ids = {item["oid"] for item in relations if item["kind"] in {"r", "p", "v", "m"}}
    for item in inventory["types"]:
        supported = (item["kind"] == "e" or
                     (item["kind"] == "c" and item["relation"] in relation_ids) or
                     (item["category"] == "A" and item["element"] != 0))
        if not supported:
            raise migrate.SafeError("Only enums and automatically generated relation/array types are supported.")
    if any(item["kind"] not in {"f", "p"} or item["security_definer"] for item in inventory["routines"]):
        raise migrate.SafeError("Aggregates, window functions or security-definer routines require separate review.")
    if applying:
        if inventory["role_exists"]:
            raise migrate.SafeError("Application role already exists. Inspect or verify; never overwrite its password or grants.")
        owners = {inventory["owner"], inventory["current_user"], "pg_database_owner"}
        if any(item["owner"] not in owners for group in ("schemas", "relations", "types", "routines")
               for item in inventory[group]):
            raise migrate.SafeError("Unexpected object ownership requires review before transfer.")


def require_frozen(pg):
    count = pg.json("SELECT count(*) FROM pg_stat_activity WHERE datname='eventharbor' "
                    "AND pid<>pg_backend_pid() AND application_name<>"
                    + migrate.literal("shared-pg-" + pg.run_id) + ";")
    if count:
        raise migrate.SafeError("EventHarbor still has other database sessions. Stop every writer and job first.")


class EventHarborBackup(migrate.PrivateBlob):
    def suffix(self, name):
        if name not in {"eventharbor-before-isolation.dump", "manifest.json"}:
            raise migrate.SafeError("Unexpected EventHarbor backup filename.")
        return "/shared-postgres/" + self.run_id + "/eventharbor-isolation/" + name

    def download_verified(self, path, expected_size, expected_sha256):
        if (type(expected_size) is not int or not 0 < expected_size <= migrate.MAX_DUMP_BYTES
                or not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise migrate.SafeError("EventHarbor backup size or checksum is invalid.")
        digest, length = hashlib.sha256(), 0
        created = False
        try:
            with path.open("xb") as output:
                created = True
                with self.request("GET", self.suffix("eventharbor-before-isolation.dump")) as response:
                    if response.headers.get("Content-Length") != str(expected_size):
                        raise migrate.SafeError("Stored EventHarbor backup has an unexpected length.")
                    while block := response.read(1024 * 1024):
                        length += len(block)
                        if length > expected_size:
                            raise migrate.SafeError("Stored EventHarbor backup exceeds the expected size.")
                        digest.update(block)
                        output.write(block)
            if length != expected_size or digest.hexdigest() != expected_sha256:
                raise migrate.SafeError("Stored EventHarbor backup failed actual-byte checksum verification.")
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise


def backup_before_change(pg, environment, run_id, directory, inventory):
    blob = EventHarborBackup(environment, run_id)
    blob.require_private()
    dump = directory / "eventharbor-before-isolation.dump"
    with pg.snapshot() as snapshot:
        manifest = pg.manifest(snapshot, directory)
        # Retain original owners and ACLs in this recovery backup. Never restore
        # it blindly over the subsequently shared live server.
        migrate.native(["pg_dump", "--no-password", "--format=custom", "--snapshot=" + snapshot,
                        "--file=" + str(dump)], pg.database.env(run_id, readonly=True))
        if not dump.is_file() or not 0 < dump.stat().st_size <= migrate.MAX_DUMP_BYTES:
            raise migrate.SafeError("EventHarbor backup is empty or exceeds the reviewed size limit.")
        require_frozen(pg)
    checksum = blob.upload(dump, "eventharbor-before-isolation.dump")
    downloaded = directory / "verified-eventharbor.dump"
    blob.download_verified(downloaded, dump.stat().st_size, checksum)
    migrate.native(["pg_restore", "--list", str(downloaded)], pg.database.env(run_id, readonly=True))
    record = {"format": 1, "purpose": "eventharbor-before-isolation", "run_id": run_id,
              "host": migrate.TARGET_HOST, "database": DATABASE, "inventory": inventory,
              "dump_sha256": checksum, "dump_bytes": dump.stat().st_size,
              "source_manifest": manifest}
    path = directory / "manifest.json"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    blob.upload(path, "manifest.json")
    # A backup is a precondition, not proof that an untested restore will work.
    with pg.snapshot() as snapshot:
        migrate.require_same(manifest, pg.manifest(snapshot, directory))
    require_frozen(pg)
    return manifest, checksum


def ownership_sql(run_id):
    # Catalog-generated identity arguments are formatted by PostgreSQL itself.
    # No object name or password is interpolated unquoted into this statement.
    return r"""\getenv isolation_password EVENTHARBOR_NEW_PASSWORD
BEGIN;
SET LOCAL createrole_self_grant='set';
SELECT pg_advisory_xact_lock(170917, 170929);
DO $guard$
BEGIN
 IF current_database()<>'eventharbor' OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname='eventharbor_app') THEN
  RAISE EXCEPTION 'Isolation target or new-role precondition failed';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname='eventharbor' AND pid<>pg_backend_pid()
           AND application_name<>current_setting('application_name')) THEN
  RAISE EXCEPTION 'EventHarbor sessions are not drained';
 END IF;
END $guard$;
CREATE ROLE eventharbor_app LOGIN PASSWORD :'isolation_password'
 NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12;
COMMENT ON ROLE eventharbor_app IS """ + migrate.literal("eventharbor-isolation:" + run_id) + r""";
REVOKE CONNECT,TEMPORARY ON DATABASE eventharbor FROM PUBLIC;
GRANT CONNECT,CREATE ON DATABASE eventharbor TO eventharbor_app;
ALTER SCHEMA public OWNER TO eventharbor_app;
REVOKE CREATE ON DATABASE eventharbor FROM eventharbor_app;
DO $ownership$
DECLARE item record; kind text;
BEGIN
 FOR item IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
             WHERE n.nspname='public' AND t.typtype='e' ORDER BY t.oid LOOP
  EXECUTE format('ALTER TYPE public.%I OWNER TO eventharbor_app', item.typname);
 END LOOP;
 FOR item IN SELECT c.oid,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')
             ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END,c.oid LOOP
  kind := CASE item.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                          WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END;
  IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=item.oid)<>'eventharbor_app' THEN
   EXECUTE format('ALTER %s public.%I OWNER TO eventharbor_app', kind, item.relname);
  END IF;
 END LOOP;
 FOR item IN SELECT p.oid,p.proname,p.prokind,pg_get_function_identity_arguments(p.oid) AS args
             FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
             WHERE n.nspname='public' AND p.prokind IN ('f','p') ORDER BY p.oid LOOP
  kind := CASE item.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END;
  EXECUTE format('ALTER %s public.%I(%s) OWNER TO eventharbor_app', kind, item.proname, item.args);
 END LOOP;
END $ownership$;
SET LOCAL ROLE eventharbor_app;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
DO $enum_grants$
DECLARE item record;
BEGIN
 FOR item IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
             WHERE n.nspname='public' AND t.typtype='e' ORDER BY t.oid LOOP
  EXECUTE format('REVOKE ALL ON TYPE public.%I FROM PUBLIC', item.typname);
 END LOOP;
END $enum_grants$;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL ROUTINES IN SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE eventharbor_app REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE eventharbor_app REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE eventharbor_app REVOKE ALL ON ROUTINES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE eventharbor_app REVOKE ALL ON TYPES FROM PUBLIC;
RESET ROLE;
DO $checks$
BEGIN
 IF EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname='eventharbor_app') THEN
  RAISE EXCEPTION 'Application role has unexpected memberships';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_database WHERE datname='eventharbor' AND pg_get_userbyid(datdba)='eventharbor_app') THEN
  RAISE EXCEPTION 'Database owner must remain unchanged';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')
           AND pg_get_userbyid(c.relowner)<>'eventharbor_app') THEN
  RAISE EXCEPTION 'Application object ownership verification failed';
 END IF;
END $checks$;
COMMIT;
"""


def verify_own_login(database, run_id):
    pg = migrate.Pg(database, run_id)
    result = pg.json("SELECT json_build_object('database',current_database(),'role',current_user,"
                     "'migration_rows',(SELECT count(*) FROM public.alembic_version));")
    if result["database"] != database.name or result["role"] != database.user or result["migration_rows"] < 1:
        raise migrate.SafeError("Application positive-login or schema-access verification failed.")


def require_denied_login(database, run_id):
    try:
        result = subprocess.run(migrate.PSQL, input="SELECT 1;", text=True,
                                env=database.env(run_id, readonly=True), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise migrate.SafeError("Cross-database test did not return a definite permission denial.") from None
    if result.returncode == 0 or "permission denied for database" not in result.stderr:
        raise migrate.SafeError("Cross-database login was not rejected specifically for database permission.")


def verify_both(admin, environment, run_id):
    eh_password = migrate.new_password(environment, "EVENTHARBOR_APP_PASSWORD")
    px_password = migrate.new_password(environment, "TARGET_APP_PASSWORD")
    roles = admin.json("SELECT coalesce(json_agg(json_build_object('name',rolname,'superuser',rolsuper,"
                       "'createdb',rolcreatedb,'createrole',rolcreaterole,'replication',rolreplication,"
                       "'bypassrls',rolbypassrls,'limit',rolconnlimit,'memberships',"
                       "(SELECT count(*) FROM pg_auth_members WHERE member=r.oid)) ORDER BY rolname),'[]'::json) "
                       "FROM pg_roles r WHERE rolname IN ('eventharbor_app','pulseexchange_app');")
    if len(roles) != 2 or {item["name"] for item in roles} != {ROLE, OTHER_ROLE}:
        raise migrate.SafeError("Both application roles must exist before isolation verification.")
    for item in roles:
        if any(item[key] for key in ("superuser", "createdb", "createrole", "replication", "bypassrls", "memberships")):
            raise migrate.SafeError("Application role has an elevated attribute or unexpected membership.")
        if item["name"] == ROLE and item["limit"] != 12:
            raise migrate.SafeError("EventHarbor application connection limit does not match the reviewed profile.")
    privileges = admin.json("SELECT json_build_object('eh_to_px',has_database_privilege('eventharbor_app','pulseexchange','CONNECT'),"
                            "'px_to_eh',has_database_privilege('pulseexchange_app','eventharbor','CONNECT')); ")
    if privileges["eh_to_px"] or privileges["px_to_eh"]:
        raise migrate.SafeError("Catalog privileges still permit cross-application database access.")
    eh = replace(admin.database, name=DATABASE, user=ROLE, password=eh_password)
    px = replace(admin.database, name=OTHER_DATABASE, user=OTHER_ROLE, password=px_password)
    verify_own_login(eh, run_id)
    verify_own_login(px, run_id)
    require_denied_login(replace(eh, name=OTHER_DATABASE), run_id)
    require_denied_login(replace(px, name=DATABASE), run_id)
    return {"mode": "Verify", "verified": True, "positive_logins": 2, "denied_cross_database_logins": 2, "changed": False}


def run(mode, environment):
    global phase
    run_id = environment.get("MIGRATION_RUN_ID", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", run_id):
        raise migrate.SafeError("A unique reviewed migration run identifier is required.")
    migrate.require_clients()
    database = replace(migrate.Database.parse(environment.get("TARGET_ADMIN_DATABASE_URL", ""), source=False), name=DATABASE)
    admin = migrate.Pg(database, run_id)
    phase = "EventHarbor inspection"
    admin.require_version()
    if mode == "Verify":
        return verify_both(admin, environment, run_id)
    inventory = admin.json(INVENTORY_SQL)
    validate_inventory(inventory, applying=mode == "Apply")
    if mode == "Inspect":
        return {"mode": mode, "changed": False, "inventory": inventory,
                "planned_role": ROLE, "connection_limit": 12,
                "database_owner_unchanged": True, "ddl_scope": "eventharbor.public"}
    if mode != "Apply" or environment.get("WRITERS_FROZEN") != "true":
        raise migrate.SafeError("Apply requires WRITERS_FROZEN=true after draining every EventHarbor consumer.")
    password = migrate.new_password(environment, "EVENTHARBOR_APP_PASSWORD")
    require_frozen(admin)
    with tempfile.TemporaryDirectory(prefix="eventharbor-isolation-") as temporary:
        directory = Path(temporary)
        phase = "private EventHarbor recovery backup"
        expected, checksum = backup_before_change(admin, environment, run_id, directory, inventory)
        latest = admin.json(INVENTORY_SQL)
        if latest != inventory:
            raise migrate.SafeError("Object inventory changed after the recovery backup. No permission changes were made.")
        validate_inventory(latest, applying=True)
        require_frozen(admin)
        phase = "atomic scoped ownership and grants"
        admin.sql(ownership_sql(run_id), readonly=False, extra_env={"EVENTHARBOR_NEW_PASSWORD": password})
        phase = "post-change data and application login verification"
        app = migrate.Pg(replace(database, user=ROLE, password=password), run_id)
        with app.snapshot() as snapshot:
            migrate.require_same(expected, app.manifest(snapshot, directory))
        verify_own_login(app.database, run_id)
    return {"mode": mode, "changed": True, "role": ROLE, "connection_limit": 12,
            "database_owner_unchanged": True, "data_verified": True,
            "backup_prefix": "shared-postgres/" + run_id + "/eventharbor-isolation/",
            "backup_sha256": checksum, "consumer_credentials_changed": False,
            "backup_download_verified": True, "backup_archive_readable": True, "backup_restore_tested": False,
            "both_app_isolation_verified": False, "next": "Update every EH consumer and run Verify after PX final copy."}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("Inspect", "Apply", "Verify"))
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.mode, os.environ), sort_keys=True))
    except migrate.SafeError as error:
        print("EventHarbor isolation stopped during " + phase + ": " + str(error), file=sys.stderr)
        return 1
    except Exception:
        print("EventHarbor isolation stopped during " + phase + ". Diagnostics withheld. Inspect state before retrying; no automatic rollback or cleanup attempted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
