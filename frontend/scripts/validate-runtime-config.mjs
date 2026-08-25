import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const frontendRoot = new URL("../", import.meta.url);
const [dockerfile, nginxConfig] = await Promise.all([
  readFile(new URL("Dockerfile", frontendRoot), "utf8"),
  readFile(new URL("nginx.conf", frontendRoot), "utf8"),
]);

assert.match(dockerfile, /^FROM nginxinc\/nginx-unprivileged:[^\s]+ AS runtime$/m);
assert.match(
  dockerfile,
  /^ARG VITE_RECEIVER_LAB_BASE_URL=http:\/\/receiver-lab:8100\/webhooks$/m,
);
assert.match(dockerfile, /^ENV VITE_RECEIVER_LAB_BASE_URL=\$\{VITE_RECEIVER_LAB_BASE_URL\}$/m);
assert.match(dockerfile, /^ENV API_UPSTREAM=http:\/\/api:8000$/m);
assert.match(dockerfile, /\/etc\/nginx\/templates\/default\.conf\.template/);
assert.match(dockerfile, /^USER 101$/m);
assert.match(dockerfile, /^EXPOSE 8080$/m);
assert.match(dockerfile, /http:\/\/127\.0\.0\.1:8080\/healthz/);

assert.match(nginxConfig, /^\s*listen 8080 default_server;$/m);
assert.match(nginxConfig, /^\s*location = \/healthz \{$/m);
assert.match(nginxConfig, /^\s*client_max_body_size 64k;$/m);
assert.match(nginxConfig, /^\s*proxy_pass \$\{API_UPSTREAM\}\/;$/m);
assert.match(nginxConfig, /^map \$request_method \$eventharbor_mutation_key \{$/m);
assert.match(nginxConfig, /limit_req_zone \$binary_remote_addr zone=eventharbor_api:/);
assert.match(nginxConfig, /limit_req_zone \$eventharbor_mutation_key zone=eventharbor_mutations:/);
assert.match(nginxConfig, /^\s*limit_req zone=eventharbor_api burst=40 nodelay;$/m);
assert.match(nginxConfig, /^\s*limit_req zone=eventharbor_mutations burst=10 nodelay;$/m);
assert.match(nginxConfig, /^\s*limit_req_status 429;$/m);
assert.match(nginxConfig, /Content-Security-Policy/);
assert.match(nginxConfig, /Strict-Transport-Security/);
assert.match(nginxConfig, /X-Content-Type-Options/);

console.log("Frontend runtime configuration is valid.");
