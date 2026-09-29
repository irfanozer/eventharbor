#!/usr/bin/env python3
"""Validate a root-only dotenv file without evaluating or printing its contents."""
import argparse
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import parse_qs, urlsplit

PROJECT = "eventharbor"
PREFIX = "EVENTHARBOR"
IMAGE = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z")
CADDY = re.compile(r"(?:docker\.io/library/)?caddy:[a-z0-9.-]+@sha256:[0-9a-f]{64}\Z")
HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
KEYS = {"PUBLIC_HOSTNAME", "ACME_EMAIL", "CADDY_IMAGE", PREFIX + "_DATABASE_URL"}
IMAGE_KEYS = {"BACKEND_IMAGE", "FRONTEND_IMAGE"}

def read_values(path: Path, *, check_permissions: bool = True) -> dict[str, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("environment input must be a regular file, not a symlink")
    if check_permissions and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("environment input must be owned by root and have mode 0600")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in KEYS | IMAGE_KEYS or key in values:
            raise ValueError("environment input contains an unknown or duplicate key")
        # Raw, single-line values only. Percent-encode reserved URL characters.
        if not value or any(char.isspace() or char in "\"'\x60$#" for char in value):
            raise ValueError("environment values must be nonempty raw values without interpolation")
        values[key] = value
    if not KEYS <= values.keys():
        raise ValueError("a required environment key is missing")
    if not HOST.fullmatch(values["PUBLIC_HOSTNAME"]):
        raise ValueError("PUBLIC_HOSTNAME must be one lowercase DNS hostname")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["ACME_EMAIL"]):
        raise ValueError("ACME_EMAIL must be a contact email address")
    if not CADDY.fullmatch(values["CADDY_IMAGE"]):
        raise ValueError("CADDY_IMAGE must be an official Caddy image pinned by SHA256 digest")
    database = urlsplit(values[PREFIX + "_DATABASE_URL"])
    if (database.scheme != "postgresql+asyncpg" or not database.hostname
            or not database.hostname.endswith(".postgres.database.azure.com")
            or not database.username or not database.password
            or database.path != "/" + PROJECT or database.port not in (None, 5432)
            or database.fragment):
        raise ValueError("database URL must target this application's Azure PostgreSQL database")
    query = parse_qs(database.query, keep_blank_values=True)
    if set(query) - {"ssl"} or ("ssl" in query and query["ssl"] not in (["require"], ["verify-full"])):
        raise ValueError("database URL has unsupported parameters or an unsafe TLS mode")
    if PROJECT == "pulseexchange" and query.get("ssl") not in (["require"], ["verify-full"]):
        raise ValueError("PulseExchange database URL must explicitly enable TLS")
    for key in IMAGE_KEYS & values.keys():
        validate_image(values[key])
    return values

def validate_image(value: str) -> None:
    if not IMAGE.fullmatch(value):
        raise ValueError("application images must be public GHCR references pinned by SHA256 digest")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "check"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("backend", nargs="?")
    parser.add_argument("frontend", nargs="?")
    args = parser.parse_args()
    try:
        values = read_values(args.source)
        if args.mode == "prepare":
            if args.destination is None or args.backend is None or args.frontend is None:
                raise ValueError("prepare requires destination, backend image, and frontend image")
            validate_image(args.backend)
            validate_image(args.frontend)
            values.update(BACKEND_IMAGE=args.backend, FRONTEND_IMAGE=args.frontend)
            # Destination is a root-created mktemp file; never follow a symlink.
            fd = os.open(args.destination, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                os.fchmod(output.fileno(), 0o600)
                for key in sorted(values):
                    output.write(f"{key}={values[key]}\n")
        elif not IMAGE_KEYS <= values.keys():
            raise ValueError("saved deployment is missing its image digests")
    except (OSError, ValueError):
        # Do not include exception strings: a URL parse error can contain secrets.
        print("Runtime environment validation failed. Check the documented keys, permissions, image digests, and database TLS URL.", file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
