# EventHarbor VM runtime

This additive runtime reuses public GHCR images from the existing production image workflow on one x86-64 Ubuntu 24.04 VM. It does not build images or run a local database. Both projects use separate databases and application roles on a shared Azure PostgreSQL Flexible Server B1ms, reached by private networking and TLS.

## Host and bootstrap contract

- Install Docker Engine, the Compose v2 plugin (minimum 2.20), Python 3, util-linux/flock, and curl. Enable Docker at boot.
- Copy this directory's public runtime files to /opt/eventharbor/runtime as root, then run `sudo bash /opt/eventharbor/runtime/install.sh`. The installer sets file ownership/modes and installs the maintenance units. It does not create secret files or start the app.
- The deploy controller's fixed bundle consists of compose.yaml, Caddyfile, nginx.conf.template, README.md, runtime.env.example, validate_env.py, install.sh, deploy.sh, maintenance.sh, eventharbor-maintenance.service, eventharbor-maintenance.timer, and worker-health.py. test_runtime.py is an offline test, not a host requirement.
- All bundle files must use LF line endings. The runtime directory is root:root 0750, scripts 0750, and non-secret configuration 0640. The two possible container-readable bind mounts, nginx.conf.template and worker-health.py, are 0644.
- The NSG allows public TCP 80/443 to Caddy only. SSH is restricted to an operator CIDR. No API, processor, receiver, Docker daemon, or PostgreSQL port is published.
- The Docker edge subnet 172.29.240.0/28 must not overlap the VM VNet or other host routes. Caddy is 172.29.240.2, Nginx is 172.29.240.3. Nginx trusts only that Caddy address for client IP reconstruction. Caddy replaces incoming client identity headers. Existing routes, body/rate limits, and browser headers are preserved in the additive Nginx overlay.
- Private DNS must resolve the Azure PostgreSQL hostname to its private address on this VM. The managed server must disable public access. The application role is restricted to this project's database; do not give it the server admin account. PostgreSQL backup and restore remain managed server operations.

## Secret input and release command

Create /etc/eventharbor/runtime.env from runtime.env.example through a secure operator channel, owned root:root with mode 0600. It is the canonical editable input. Do not put its contents in VM custom data, command arguments, source control, public deployment outputs, or a storage bundle.

The input permits only PUBLIC_HOSTNAME, ACME_EMAIL, CADDY_IMAGE, and EVENTHARBOR_DATABASE_URL. Use raw KEY=value lines with no quoting or interpolation. Percent-encode reserved characters in the username/password. Use the ordinary `<server>.postgres.database.azure.com` hostname, which resolves privately, and the database name eventharbor. The example requests `ssl=verify-full`. EventHarbor also forces verify-full independently in Compose.

Use the VM's Azure DNS hostname in PUBLIC_HOSTNAME for pre-cutover HTTPS. That hostname must resolve publicly to the VM, with ports 80/443 reachable for ACME. Later replace it with the existing custom hostname and redeploy after the DNS cutover plan is approved. The configured hostname is the only accepted browser origin. Caddy certificate data persists in Docker volumes; the configuration contains no ACME private keys.

Resolve the existing GHCR commit tags to their published digest before deployment. Then run as root:

```sh
/opt/eventharbor/runtime/deploy.sh \
  ghcr.io/OWNER/REPOSITORY-backend@sha256:BACKEND_DIGEST \
  ghcr.io/OWNER/REPOSITORY-frontend@sha256:FRONTEND_DIGEST \
  /etc/eventharbor/runtime.env
```

Replace the uppercase placeholders, including OWNER/REPOSITORY, with real lowercase GHCR image names and 64-character digest values. Floating tags and commit tags are rejected. No registry credentials are needed for public images. The Caddy example is pinned to official 2.11.4-alpine index digest sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b, verified against Docker Hub on 2026-09-28. Review and update that digest deliberately for security releases.

Deployment serializes with maintenance using /run/lock/eventharbor-runtime.lock, validates the dotenv input without evaluating it, pulls images, validates Caddy, disables the timer during changes, runs the existing Alembic migration, briefly stops app containers to avoid duplicate memory usage, and waits for internal health checks. Application deployment therefore has a short outage. Image builds stay in CI.

On success, /etc/eventharbor/current.env contains the validated input plus exact release images and becomes the maintenance source. /etc/eventharbor/previous.env retains the preceding release; /etc/eventharbor/failed.env retains the last failed candidate. Every file is root:root 0600. These files are secret configurations, not database backups. Copy them only to an approved encrypted operator backup if recovery requirements need it.

Deployment logs are root-only in /var/log/eventharbor; host commands emit no secret contents. Docker metadata still contains the application connection URL, so root and Docker access are equivalent to secret access. Do not share docker inspect, full Compose config, or raw logs. Logs are bounded: container logs 2 x 5 MiB each; deploy log rotates after 10 MiB and maintenance after 5 MiB, retaining one previous file.

## Maintenance, limits, and recovery

The existing eventharbor.maintenance command removes only terminal event history older than seven days, in bounded batches of 500 with at most 5,000 events per run. Its PostgreSQL advisory lock remains in use. The worker continues delivering active events. The timer runs around 04:00 UTC daily with a five-minute randomized delay and catches up after reboot. A held deployment/maintenance flock skips an overlapping timer invocation. Container jobs retain their existing database-level locks as well. Inspect timer failures with `systemctl status eventharbor-maintenance.service`; root-only logs contain details.

Caddy 64 MiB + Nginx 64 MiB + API 160 MiB + worker 128 MiB + receiver 96 MiB = 512 MiB. One migration, seed, or maintenance container adds at most 128 MiB because jobs and deployment share the lock. This leaves 384 MiB of a 1 GiB VM for Ubuntu, Docker, and overhead at all stated container maxima. These are initial low-traffic demo limits, not measured capacity claims. Do not build images on this VM. A backup agent or other workload needs a separate memory allowance. Watch OOM/restart counts, sustained CPU credits, memory, disk, and PostgreSQL connections in the pre-cutover soak test; do not cut over if the VM cannot retain headroom. A B1s VM offers burstable CPU rather than a sustained full core.

Containers use read-only root filesystems, dropped capabilities, bounded temporary filesystems, PID limits, restart policies, and bounded local logs. Caddy retains only NET_BIND_SERVICE; application images keep their existing non-root users. Jobs are serialized and never enabled as always-running services. Database pools are 2 connections with no overflow per long-running database process, 1 for maintenance.

The existing frontend image embeds http://eventharbor-receiver-prod/webhooks. The receiver therefore has that internal alias and listens on container port 80 without publishing it. Its network namespace permits that unprivileged port while retaining the backend image's non-root user and dropping all capabilities. The worker probe checks process presence, database connectivity, and receiver health. It does not prove that a delivery loop is making progress, because the existing image has no heartbeat. Verify an actual bounded delivery with the existing smoke flow.

Docker restarts containers after process exit. Docker health state alone does not restart a hung process: monitor health failures and restart deliberately after diagnosis. A failed deployment does not reverse schema changes or silently claim rollback. After a migration/start failure, the maintenance timer stays disabled; inspect the protected logs, check schema compatibility, and rerun deploy.sh with a known-good digest pair and the appropriate saved input. previous.env can be passed as the secret input. Restore the managed database only through a reviewed PostgreSQL restore procedure, then update the connection URL if the restore creates a new server.

Before production DNS changes, verify public HTTPS, the project's existing smoke script including a complete delivery, reboot recovery, private database reachability, and managed database restore. Internal Compose readiness is not evidence of browser TLS issuance or an end-to-end cloud deployment.

## Offline validation

Run `python test_runtime.py` here. To parse Compose without a running engine, provide harmless digest-shaped BACKEND_IMAGE and FRONTEND_IMAGE environment values, then run `docker compose --env-file runtime.env.example --profile tasks config --quiet` from this directory. Do not use a real secret file for printed config validation. Bash syntax checks and these offline tests do not verify image architecture, executable imports, production TLS, memory use, or runtime behavior.
