#!/usr/bin/env bash
# Host-only deployment; images must already exist in public GHCR.
set -euo pipefail
umask 077
[[ "$EUID" -eq 0 ]] || { echo "Run deployment as root." >&2; exit 1; }
[[ "$#" -ge 2 && "$#" -le 3 ]] || {
    echo "Usage: deploy.sh BACKEND_IMAGE@sha256:DIGEST FRONTEND_IMAGE@sha256:DIGEST [SECRET_ENV_FILE]" >&2
    exit 1
}
[[ "$(uname -m)" == x86_64 ]] || { echo "This runtime requires an x86-64 VM." >&2; exit 1; }
runtime=/opt/eventharbor/runtime
state=/etc/eventharbor
source_env="${3:-$state/runtime.env}"
exec 9>/run/lock/eventharbor-runtime.lock
flock -w 900 9 || { echo "Another deployment or maintenance operation is active." >&2; exit 1; }
install -d -o root -g root -m 0700 "$state" /var/log/eventharbor
log=/var/log/eventharbor/deploy.log
touch "$log"
chmod 0600 "$log"
if [[ "$(stat -c %s "$log")" -gt 10485760 ]]; then
    mv -f "$log" "$log.previous"
    touch "$log"
    chmod 0600 "$log"
fi
phase=validation
job_name=eventharbor-deployment-task
candidate="$(mktemp "$state/candidate.XXXXXX.env")"
finish() {
    result=$?
    trap - EXIT
    if [[ "$result" -ne 0 ]]; then
        # Do not leave a migration/seed running after an interrupted Docker client.
        env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin timeout 15 docker rm --force "$job_name" >>"$log" 2>&1 || true
        if [[ -f "$candidate" ]]; then
            mv -f "$candidate" "$state/failed.env"
        fi
        echo "Deployment failed during $phase. Root can inspect $log. No database rollback was attempted." >&2
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 143' HUP INT TERM
python3 "$runtime/validate_env.py" prepare "$source_env" "$candidate" "$1" "$2"
compose() {
    # Prevent inherited COMPOSE_* or application variables overriding validated input.
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        docker compose --project-directory "$runtime" --env-file "$candidate" \
        -f "$runtime/compose.yaml" "$@"
}
run() { "$@" >>"$log" 2>&1; }
version="$(env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin docker compose version --short 2>>"$log")"
version="${version#v}"
[[ "$(printf '%s\n' 2.20.0 "$version" | sort -V | head -n 1)" == 2.20.0 ]] || {
    echo "Docker Compose 2.20.0 or later is required." >&2
    exit 1
}
run compose --profile tasks config --quiet
phase=image-pull
run compose --profile tasks pull --quiet
phase=caddy-validation
run compose run --rm --no-deps --name "$job_name" caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
# Failed updates leave this timer disabled until an operator resolves the release.
run systemctl disable --now eventharbor-maintenance.timer
if [[ -f "$state/current.env" ]]; then
    install -o root -g root -m 0600 "$state/current.env" "$state/previous.env"
fi
phase=migration
run compose run --rm --no-deps --name "$job_name" migrate
phase=application-start
# Avoid transiently doubling application RAM during replacement on a 1 GiB VM.
# This is an intentional brief outage, not a rolling or zero-downtime deployment.
run compose stop frontend api worker receiver-lab
run compose up --detach --wait --wait-timeout 180

phase=record-release
mv -f "$candidate" "$state/current.env"
run systemctl enable --now eventharbor-maintenance.timer
echo "EventHarbor runtime is healthy on its internal probes; maintenance timer enabled."
echo "Verify public HTTPS and the application smoke flow before moving production DNS."
