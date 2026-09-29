"""Offline security and compatibility checks for the host-only runtime."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("runtime_validation", ROOT / "validate_env.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

class EnvironmentValidation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.file = Path(self.directory.name) / "input.env"
        self.values = {
            "PUBLIC_HOSTNAME": "demo.eastus.cloudapp.azure.com",
            "ACME_EMAIL": "operator@example.com",
            "CADDY_IMAGE": "caddy:2.11.4-alpine@sha256:" + "a" * 64,
            validator.PREFIX + "_DATABASE_URL": (
                "postgresql+asyncpg://app:password%24value@server.postgres.database.azure.com/"
                + validator.PROJECT + "?ssl=verify-full"
            ),
        }

    def tearDown(self):
        self.directory.cleanup()

    def read(self, suffix=""):
        self.file.write_text(
            "\n".join(f"{key}={value}" for key, value in self.values.items()) + "\n" + suffix,
            encoding="utf-8",
        )
        # Linux file ownership is enforced by the real CLI; only parser tests skip it.
        return validator.read_values(self.file, check_permissions=False)

    def test_accepts_tls_private_server_url_and_azure_pre_cutover_hostname(self):
        self.assertEqual(self.read()["PUBLIC_HOSTNAME"], self.values["PUBLIC_HOSTNAME"])

    def test_every_database_consumer_has_system_ca_bundle_and_full_tls_verification(self):
        import json
        import os
        import shutil
        import subprocess
        from urllib.parse import parse_qs, urlsplit

        docker = shutil.which("docker")
        if not docker:
            self.skipTest("Docker Compose required for offline configuration expansion; no containers are started.")
        self.read()
        environment = {
            **os.environ,
            **self.values,
            "BACKEND_IMAGE": "ghcr.io/example/backend@sha256:" + "b" * 64,
            "FRONTEND_IMAGE": "ghcr.io/example/frontend@sha256:" + "c" * 64,
        }
        result = subprocess.run(
            [docker, "compose", "--env-file", str(self.file), "-f", str(ROOT / "compose.yaml"),
             "--profile", "tasks", "config", "--format", "json"],
            env=environment, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, "Offline Compose configuration expansion failed.")
        services = json.loads(result.stdout)["services"]
        database_key = validator.PREFIX + "_DATABASE_URL"
        consumers = {name: service["environment"] for name, service in services.items()
                     if database_key in service.get("environment", {})}
        self.assertEqual(set(consumers), {"api", "worker", "receiver-lab", "migrate", "maintenance"})
        for name, values in consumers.items():
            with self.subTest(service=name):
                self.assertEqual(values["PGSSLROOTCERT"], "/etc/ssl/certs/ca-certificates.crt")
                self.assertEqual(parse_qs(urlsplit(values[database_key]).query), {"ssl": ["verify-full"]})
                self.assertEqual(values["EVENTHARBOR_DATABASE_SSL_MODE"], "verify-full")
        self.assertIn('ssl="verify-full"', (ROOT / "worker-health.py").read_text())
        self.assertIn("?ssl=verify-full", (ROOT / "runtime.env.example").read_text())

    def test_rejects_wrong_database_and_non_azure_host(self):
        key = validator.PREFIX + "_DATABASE_URL"
        for url in (
            "postgresql+asyncpg://app:password@localhost/" + validator.PROJECT + "?ssl=require",
            "postgresql+asyncpg://app:password@server.postgres.database.azure.com/another?ssl=require",
        ):
            with self.subTest(url=url):
                self.values[key] = url
                with self.assertRaises(ValueError):
                    self.read()

    def test_rejects_disabled_tls_and_duplicate_ssl_parameters(self):
        key = validator.PREFIX + "_DATABASE_URL"
        original = self.values[key]
        for query in ("ssl=disable", "ssl=require&ssl=disable", "sslmode=require"):
            with self.subTest(query=query):
                self.values[key] = original.split("?")[0] + "?" + query
                with self.assertRaises(ValueError):
                    self.read()

    def test_pulseexchange_requires_explicit_tls(self):
        if validator.PROJECT != "pulseexchange":
            self.skipTest("EventHarbor independently forces verify-full in Compose")
        key = validator.PREFIX + "_DATABASE_URL"
        self.values[key] = self.values[key].split("?")[0]
        with self.assertRaises(ValueError):
            self.read()

    def test_rejects_compose_interpolation_and_shell_syntax(self):
        for value in ("\u0024{SECRET}", "'quoted'", "\u0060command\u0060", "value with spaces"):
            with self.subTest(value=value):
                self.values["ACME_EMAIL"] = value
                with self.assertRaises(ValueError):
                    self.read()

    def test_rejects_environment_guard_override_and_duplicates(self):
        for suffix in ("COMPOSE_FILE=other.yaml\n", "PUBLIC_HOSTNAME=other.example.com\n",
                       validator.PREFIX + "_ENVIRONMENT=local\n"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    self.read(suffix)

    def test_requires_digest_pinning(self):
        validator.validate_image("ghcr.io/example/app-backend@sha256:" + "b" * 64)
        for image in ("ghcr.io/example/app:latest", "ghcr.io/example/app:abcdef",
                      "docker.io/example/app@sha256:" + "b" * 64):
            with self.subTest(image=image):
                with self.assertRaises(ValueError):
                    validator.validate_image(image)

    def test_does_not_accept_caddy_from_untrusted_repository(self):
        self.values["CADDY_IMAGE"] = "ghcr.io/other/caddy@sha256:" + "a" * 64
        with self.assertRaises(ValueError):
            self.read()

    def test_proxy_only_trusts_caddy_and_overwrites_client_identity(self):
        nginx = (ROOT / "nginx.conf.template").read_text()
        caddy = (ROOT / "Caddyfile").read_text()
        self.assertIn("set_real_ip_from 172.29.240.2;", nginx)
        self.assertNotIn("set_real_ip_from 0.0.0.0/0", nginx)
        self.assertIn("header_up X-Forwarded-For {remote_host}", caddy)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", nginx)

class WorkerHealth(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("worker_health", ROOT / "worker-health.py")
        cls.health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.health)

    async def test_direct_driver_gets_clean_dsn_and_explicit_full_tls_verification(self):
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch

        # URL-encoded credential characters must not be decoded or rewritten.
        url = "postgresql+asyncpg://app:password%3F%23%26%40@server.postgres.database.azure.com:5432/eventharbor?ssl=verify-full"
        connection = SimpleNamespace(execute=AsyncMock(), close=AsyncMock())
        connect = AsyncMock(return_value=connection)
        process = Mock()
        process.exists.return_value = True
        process.read_bytes.return_value = b"python\0-m\0eventharbor.deliveries.worker\0"
        with (patch.dict(os.environ, {"EVENTHARBOR_DATABASE_URL": url}),
              patch.dict(sys.modules, {"asyncpg": SimpleNamespace(connect=connect)}),
              patch.object(self.health, "Path") as path,
              patch.object(self.health.urllib.request, "urlopen") as urlopen):
            path.return_value.glob.return_value = [process]
            await self.health.check()
        connect.assert_awaited_once_with(
            "postgresql://app:password%3F%23%26%40@server.postgres.database.azure.com:5432/eventharbor",
            ssl="verify-full", timeout=3,
        )
        connection.execute.assert_awaited_once_with("SELECT 1")
        connection.close.assert_awaited_once_with(timeout=2)
        urlopen.assert_called_once_with("http://eventharbor-receiver-prod/health", timeout=3)
        urlopen.return_value.close.assert_called_once_with()

    async def test_rejects_missing_weak_duplicate_or_unrecognized_query_parameters(self):
        prefix = "postgresql+asyncpg://app:password@server.postgres.database.azure.com/eventharbor"
        for suffix in ("", "?ssl=require", "?ssl=disable", "?ssl=", "?sslmode=verify-full",
                       "?ssl=verify-full&ssl=verify-full", "?ssl=verify-full&ssl=disable",
                       "?ssl=verify-full&options=anything", "?ssl=verify-full&unexpected",
                       "?ssl=verify-full#fragment"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.health.database_dsn(prefix + suffix)
        with self.assertRaises(ValueError):
            self.health.database_dsn(prefix.replace("postgresql+asyncpg", "postgresql") + "?ssl=verify-full")


class WorkerHealthErrors(unittest.TestCase):
    def test_main_returns_failure_without_printing_database_diagnostics(self):
        import contextlib
        import io
        from unittest.mock import AsyncMock, patch

        spec = importlib.util.spec_from_file_location("worker_health_errors", ROOT / "worker-health.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        output = io.StringIO()
        with (patch.object(health, "check", AsyncMock(side_effect=RuntimeError("secret database diagnostics"))),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(output)):
            self.assertEqual(health.main(), 1)
        self.assertEqual(output.getvalue(), "")


class SwapSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import types
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        code = source.split("python3 - <<'ECONOMY_SWAP_PY'\n", 1)[1].split("\nECONOMY_SWAP_PY", 1)[0]
        cls.swap = types.ModuleType("offline_swap_installer")
        exec(compile(code, str(ROOT / "install.sh") + ":embedded-swap", "exec"), cls.swap.__dict__)

    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.swap = type(self).swap
        self.calls = []
        self.active = []
        self.free = 20 * 1024 ** 3
        self.filesystem = "ext4"
        self.signature = "swap"
        self.swapfile = root / "private" / "swapfile"
        self.unit = root / "bounded.swap"
        self.policy = root / "policy.conf"
        self.fstab = root / "fstab"
        self.fstab.write_text("# fixture only\n")
        values = {"SWAP_DIRECTORY": self.swapfile.parent, "SWAP_FILE": self.swapfile,
                  "SWAP_BYTES": 8192, "UNIT": self.unit, "POLICY": self.policy, "FSTAB": self.fstab}
        for name, value in values.items():
            fixture = patch.object(self.swap, name, value)
            fixture.start()
            self.addCleanup(fixture.stop)

        def command(*args):
            self.calls.append(args)
            if args[0] == "swapon":
                return "\n".join(self.active)
            if args[0] == "findmnt":
                return self.filesystem
            if args[0] == "blkid":
                return self.signature
            if args[:3] == ("systemctl", "enable", "--now"):
                self.active = [str(self.swapfile)]
            if args == ("sysctl", "-n", "vm.swappiness"):
                return "10"
            return ""

        def allocate():
            self.calls.append(("allocate",))
            self.swapfile.write_bytes(bytes(self.swap.SWAP_BYTES))

        def write_config(path, text):
            if not path.exists():
                path.write_text(text)

        for name, value in (("command", command), ("allocate", allocate),
                            ("write_configuration", write_config),
                            ("trusted", lambda path, **kwargs: SimpleNamespace(st_size=path.stat().st_size if path.exists() else 0))):
            fixture = patch.object(self.swap, name, value)
            fixture.start()
            self.addCleanup(fixture.stop)
        fixture = patch.object(self.swap.shutil, "disk_usage", side_effect=lambda path: SimpleNamespace(free=self.free))
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_creates_then_reuses_without_allocating_or_formatting_again(self):
        self.swap.ensure_swap()
        self.assertEqual(self.swapfile.stat().st_size, 8192)
        self.assertEqual(self.policy.read_text(), self.swap.POLICY_TEXT)
        self.calls.clear()
        self.swap.ensure_swap()
        self.assertNotIn(("allocate",), self.calls)
        self.assertFalse(any(call[0] == "mkswap" for call in self.calls))

    def test_other_active_swap_is_not_replaced(self):
        self.active = ["/existing/operator-swap"]
        with self.assertRaises(RuntimeError):
            self.swap.ensure_swap()
        self.assertFalse(self.swapfile.exists())
        self.assertFalse(self.unit.exists())

    def test_existing_fstab_swap_is_not_changed(self):
        text = "/operator-swap none swap sw 0 0\n"
        self.fstab.write_text(text)
        with self.assertRaises(RuntimeError):
            self.swap.ensure_swap()
        self.assertEqual(self.fstab.read_text(), text)
        self.assertEqual(self.calls, [])

    def test_existing_unit_or_policy_is_not_overwritten(self):
        for path in (self.unit, self.policy):
            path.write_text("operator-owned configuration")
            with self.assertRaises(RuntimeError):
                self.swap.ensure_swap()
            self.assertEqual(path.read_text(), "operator-owned configuration")
            self.assertFalse(self.swapfile.exists())
            path.unlink()

    def test_disk_reserve_and_filesystem_are_required(self):
        self.free = self.swap.RESERVE_BYTES + self.swap.SWAP_BYTES - 1
        with self.assertRaises(RuntimeError):
            self.swap.ensure_swap()
        self.assertFalse(self.swapfile.exists())
        self.free = 20 * 1024 ** 3
        self.filesystem = "btrfs"
        with self.assertRaises(RuntimeError):
            self.swap.ensure_swap()
        self.assertFalse(self.swapfile.exists())

    def test_existing_wrong_size_or_signature_is_not_reformatted(self):
        self.swapfile.parent.mkdir()
        self.swapfile.write_bytes(b"existing")
        with self.assertRaises(RuntimeError):
            self.swap.ensure_swap()
        self.assertEqual(self.swapfile.read_bytes(), b"existing")
        self.swapfile.write_bytes(bytes(self.swap.SWAP_BYTES))
        self.signature = "ext4"
        with self.assertRaises(RuntimeError):
            self.swap.ensure_swap()
        self.assertFalse(any(call[0] == "mkswap" for call in self.calls))

    def test_postactivation_failure_is_reported(self):
        from unittest.mock import patch
        original = self.swap.command
        def wrong_policy(*args):
            return "60" if args == ("sysctl", "-n", "vm.swappiness") else original(*args)
        with patch.object(self.swap, "command", side_effect=wrong_policy), self.assertRaises(RuntimeError):
            self.swap.ensure_swap()

    def test_permissions_reject_symlinks_hardlinks_wrong_owners_and_modes(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import stat
        # Compile a fresh module so this test exercises the real permission guard.
        namespace = {"__name__": "permission_test"}
        code = (ROOT / "install.sh").read_text().split("python3 - <<'ECONOMY_SWAP_PY'\n", 1)[1].split("\nECONOMY_SWAP_PY", 1)[0]
        exec(code, namespace)
        valid = dict(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0, st_nlink=1)
        path = Mock()
        path.lstat.return_value = SimpleNamespace(**valid)
        namespace["trusted"](path, mode=0o600)
        for change in ({"st_uid": 1000}, {"st_gid": 1000}, {"st_nlink": 2},
                       {"st_mode": stat.S_IFLNK | 0o600}, {"st_mode": stat.S_IFREG | 0o666}):
            path.lstat.return_value = SimpleNamespace(**{**valid, **change})
            with self.assertRaises(RuntimeError):
                namespace["trusted"](path, mode=0o600)

    def test_swap_only_mode_exits_before_runtime_writes_and_rejects_unknown_args(self):
        import os
        import shutil
        import subprocess
        source = (ROOT / "install.sh").read_text()
        argument_guard = source.split("umask 077\n", 1)[1].split('[[ "$EUID"', 1)[0]
        early_exit = source.split('if [[ "${1:-}" == "--prepare-swap-only" ]]; then', 1)[1].split("\nfi", 1)[0]
        script = 'set -euo pipefail\n' + argument_guard + '\nif [[ "${1:-}" == "--prepare-swap-only" ]]; then' + early_exit + '\nfi\nprintf RUNTIME_INSTALL_REACHED'
        bash = shutil.which("bash")
        if os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
            bash = str(candidate) if candidate.is_file() else None
        if not bash:
            self.skipTest("Bash required for isolated mode parsing; no host setup is executed.")
        result = subprocess.run([bash, "-c", script, "installer", "--prepare-swap-only"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("RUNTIME_INSTALL_REACHED", result.stdout)
        for arguments in (["--unknown"], [""], ["--prepare-swap-only", "extra"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([bash, "-c", script, "installer", *arguments], text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("RUNTIME_INSTALL_REACHED", result.stdout)
        result = subprocess.run([bash, "-c", script, "installer"], text=True, capture_output=True)
        self.assertEqual(result.stdout, "RUNTIME_INSTALL_REACHED")

    def test_creation_is_exclusive_and_bounded_and_never_disables_swap(self):
        import inspect
        code = (ROOT / "install.sh").read_text().split("python3 - <<'ECONOMY_SWAP_PY'\n", 1)[1].split("\nECONOMY_SWAP_PY", 1)[0]
        self.assertIn("SWAP_BYTES = 2 * 1024 * 1024 * 1024", code)
        self.assertIn("os.O_EXCL | os.O_NOFOLLOW", code)
        self.assertIn("os.fchmod(output.fileno(), 0o600)", code)
        self.assertNotIn("swapoff", code)
        self.assertNotIn("unlink(", code)
        self.assertNotIn("truncate(", code)

if __name__ == "__main__":
    unittest.main()
