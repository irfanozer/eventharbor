#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ "$#" -gt 1 || ( "$#" -eq 1 && "$1" != "--prepare-swap-only" ) ]]; then
    echo "Usage: install.sh [--prepare-swap-only]" >&2
    exit 2
fi
[[ "$EUID" -eq 0 ]] || { echo "Run the installer as root." >&2; exit 1; }
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target="/opt/eventharbor/runtime"
exec 9>/run/lock/eventharbor-runtime.lock
flock -w 900 9 || { echo "Another runtime operation is active." >&2; exit 1; }
# A small RAM VM needs a bounded burst cushion before Docker image pulls.
# This host-wide lock also protects against another project's installer.
exec 8>/run/lock/azure-economy-swap.lock
flock -w 900 8 || { echo "Another swap setup is active." >&2; exit 1; }
python3 - <<'ECONOMY_SWAP_PY'
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

SWAP_DIRECTORY = Path("/var/lib/azure_economy")
SWAP_FILE = SWAP_DIRECTORY / "swapfile"
SWAP_BYTES = 2 * 1024 * 1024 * 1024
RESERVE_BYTES = 8 * 1024 * 1024 * 1024
UNIT = Path("/etc/systemd/system/var-lib-azure_economy-swapfile.swap")
POLICY = Path("/etc/sysctl.d/99-azure-economy-swap.conf")
FSTAB = Path("/etc/fstab")
UNIT_TEXT = """[Unit]
Description=Bounded economy VM swap on the existing OS disk
Before=docker.service

[Swap]
What=/var/lib/azure_economy/swapfile
Options=pri=10
TimeoutSec=30

[Install]
WantedBy=swap.target
"""
POLICY_TEXT = "# Economy VM burst cushion; not additional RAM capacity.\nvm.swappiness=10\n"


def command(*arguments):
    result = subprocess.run(arguments, check=False, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("A swap setup command failed. Existing files were not replaced; inspect the host before retrying.")
    return result.stdout.strip()


def trusted(path, *, directory=False, mode=None):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or (not directory and info.st_nlink != 1) or stat.S_IMODE(info.st_mode) & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise RuntimeError("Swap paths must be root-owned, non-linked and have the reviewed permissions.")
    return info


def check_configuration(path, contents):
    if path.exists() or path.is_symlink():
        trusted(path, mode=0o644)
        if path.read_text(encoding="utf-8") != contents:
            raise RuntimeError("Existing swap configuration differs. Refusing to overwrite it.")


def write_configuration(path, contents):
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())


def allocate():
    # Actual zero writes avoid a sparse swap file. Memory use is one MiB.
    descriptor = os.open(SWAP_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        block = bytes(1024 * 1024)
        for _ in range(SWAP_BYTES // len(block)):
            output.write(block)
        output.flush()
        os.fsync(output.fileno())


def ensure_swap():
    for directory in (Path("/var"), Path("/var/lib"), Path("/etc"),
                      Path("/etc/systemd"), UNIT.parent, POLICY.parent):
        trusted(directory, directory=True)
    trusted(FSTAB)
    for line in FSTAB.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 3 and fields[2] == "swap":
            raise RuntimeError("Existing fstab swap must be reviewed; it was not changed.")
    active = command("swapon", "--show=NAME", "--noheadings", "--raw").splitlines()
    if any(name != str(SWAP_FILE) for name in active):
        raise RuntimeError("Another active swap configuration exists; it was not changed.")
    check_configuration(UNIT, UNIT_TEXT)
    check_configuration(POLICY, POLICY_TEXT)
    # Restrict this first-run implementation to the reviewed non-CoW OS layout.
    if command("findmnt", "--noheadings", "--output", "FSTYPE", "--target", "/var/lib") != "ext4":
        raise RuntimeError("Swap setup requires the reviewed ext4 OS filesystem.")
    if SWAP_DIRECTORY.exists() or SWAP_DIRECTORY.is_symlink():
        trusted(SWAP_DIRECTORY, directory=True, mode=0o700)
    exists = SWAP_FILE.exists() or SWAP_FILE.is_symlink()
    if exists:
        info = trusted(SWAP_FILE, mode=0o600)
        if info.st_size != SWAP_BYTES or command("blkid", "-p", "-s", "TYPE", "-o", "value", str(SWAP_FILE)) != "swap":
            raise RuntimeError("Existing swap file has an unexpected size or signature; no reformat attempted.")
    if shutil.disk_usage("/var/lib").free < RESERVE_BYTES + (0 if exists else SWAP_BYTES):
        raise RuntimeError("Swap setup must leave at least eight GiB free for images, logs and updates.")
    if not exists:
        SWAP_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
        trusted(SWAP_DIRECTORY, directory=True, mode=0o700)
        allocate()
        command("mkswap", "--label", "economy-swap", str(SWAP_FILE))
    write_configuration(UNIT, UNIT_TEXT)
    write_configuration(POLICY, POLICY_TEXT)
    command("sysctl", "--load", str(POLICY))
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "--now", UNIT.name)
    active = command("swapon", "--show=NAME", "--noheadings", "--raw").splitlines()
    if active != [str(SWAP_FILE)] or command("sysctl", "-n", "vm.swappiness") != "10":
        raise RuntimeError("Swap activation or swappiness verification failed.")
    print("Verified two GiB of bounded swap on the existing OS disk; swappiness 10.")


if __name__ == "__main__":
    try:
        ensure_swap()
    except Exception as error:
        # No commands or configuration here contain application secrets.
        print("Swap safety setup stopped: " + (str(error) if isinstance(error, RuntimeError) else "inspect the host; no automatic cleanup was attempted."), file=sys.stderr)
        sys.exit(1)
ECONOMY_SWAP_PY
flock -u 8
if [[ "${1:-}" == "--prepare-swap-only" ]]; then
    echo "Swap preparation complete; installed runtime files and maintenance units were not changed."
    exit 0
fi
install -d -o root -g root -m 0750 "$target"
install -d -o root -g root -m 0700 /etc/eventharbor /var/log/eventharbor
for file in compose.yaml Caddyfile README.md runtime.env.example; do
    if [[ "$source_dir" != "$target" ]]; then
        install -o root -g root -m 0640 "$source_dir/$file" "$target/$file"
    fi
    chown root:root "$target/$file"
    chmod 0640 "$target/$file"
done
if [[ "$source_dir" != "$target" ]]; then
    install -o root -g root -m 0644 "$source_dir/nginx.conf.template" "$target/nginx.conf.template"
fi
chown root:root "$target/nginx.conf.template"
chmod 0644 "$target/nginx.conf.template"
for file in install.sh deploy.sh maintenance.sh validate_env.py; do
    if [[ "$source_dir" != "$target" ]]; then
        install -o root -g root -m 0750 "$source_dir/$file" "$target/$file"
    fi
    chown root:root "$target/$file"
    chmod 0750 "$target/$file"
done
if [[ -f "$source_dir/worker-health.py" && "$source_dir" != "$target" ]]; then
    install -o root -g root -m 0644 "$source_dir/worker-health.py" "$target/worker-health.py"
fi
if [[ -f "$target/worker-health.py" ]]; then
    chown root:root "$target/worker-health.py"
    chmod 0644 "$target/worker-health.py"
fi
for unit in eventharbor-maintenance.service eventharbor-maintenance.timer; do
    if [[ "$source_dir" != "$target" ]]; then
        install -o root -g root -m 0644 "$source_dir/$unit" "$target/$unit"
    fi
    install -o root -g root -m 0644 "$target/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
# deploy.sh enables the timer only after migrations and runtime readiness pass.
echo "Runtime installed. Supply a root-owned 0600 environment file, then run deploy.sh."
