#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$EUID" -eq 0 ]] || { echo "Run the installer as root." >&2; exit 1; }
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target="/opt/eventharbor/runtime"
exec 9>/run/lock/eventharbor-runtime.lock
flock -w 900 9 || { echo "Another runtime operation is active." >&2; exit 1; }
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
