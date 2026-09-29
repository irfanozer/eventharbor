#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$EUID" -eq 0 ]] || { echo "Run maintenance as root." >&2; exit 1; }
exec 9>/run/lock/eventharbor-runtime.lock
# A deployment or another maintenance run takes precedence; the next timer retries.
flock -n 9 || exit 0
runtime=/opt/eventharbor/runtime
current=/etc/eventharbor/current.env
python3 "$runtime/validate_env.py" check "$current"
log=/var/log/eventharbor/maintenance.log
touch "$log"
chmod 0600 "$log"
task=eventharbor-maintenance-task
cleanup() {
    # Also stop the one-shot container if systemd times out its Docker client.
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin docker rm --force "$task" >>"$log" 2>&1 || true
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
# Bound retained diagnostics without logging secrets to the journal.
if [[ "$(stat -c %s "$log")" -gt 5242880 ]]; then
    mv -f "$log" "$log.previous"
    touch "$log"
    chmod 0600 "$log"
fi
if ! env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    docker compose --project-directory "$runtime" --env-file "$current" \
    -f "$runtime/compose.yaml" run --rm --no-deps --name "$task" maintenance >>"$log" 2>&1; then
    echo "Maintenance failed; root can inspect $log." >&2
    exit 1
fi
echo "Maintenance completed."
