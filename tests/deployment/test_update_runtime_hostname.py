"""Offline execution of the hostname-only update with a private local fixture."""
import contextlib
import io
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/azure-economy/update-runtime-hostname.ps1"
SOURCE = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
MATCH = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", SOURCE, re.DOTALL)
assert MATCH
HOST_CODE = compile(MATCH.group(1), "update-runtime-hostname", "exec")


class HostnameUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hostname-update-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = "eventharbor"
        self.expected = "eh-demos-fixture.westus2.cloudapp.azure.com"
        self.environment = {
            "ECONOMY_PROJECT": self.project, "ECONOMY_EXPECTED_HOSTNAME": self.expected,
            "ECONOMY_PUBLIC_HOSTNAME": "eventharbor.irfanburakozer.com",
        }
        self.folder = self.root / self.project
        self.folder.mkdir(mode=0o700)
        self.file = self.folder / "runtime.env"
        self.original = (f"PUBLIC_HOSTNAME={self.expected}\nACME_EMAIL=operator@example.com\n"
                         "EVENTHARBOR_DATABASE_URL=postgresql+asyncpg://app:PRIVATE_PASSWORD@server/eventharbor?ssl=verify-full\n"
                         "CADDY_IMAGE=caddy@sha256:retained\n").encode()
        self.file.write_bytes(self.original)
        self.file.chmod(0o600)
        self.bad_modes = {}

    def run_host(self, replace_error=False):
        real_path = Path
        real_lstat = Path.lstat
        real_fstat = os.fstat
        real_open = os.open
        output = io.StringIO()
        # Windows does not implement POSIX owner/mode checks or flock. Normalize
        # local fixture metadata only; the actual host code retains these checks.
        def metadata(path):
            value = real_lstat(path)
            fields = list(value)
            desired = self.bad_modes.get(str(path), 0o700 if stat.S_ISDIR(value.st_mode) else 0o600)
            fields[0] = stat.S_IFMT(value.st_mode) | desired
            fields[4] = 0
            return os.stat_result(fields)

        def descriptor_metadata(fd):
            value = real_fstat(fd)
            fields = list(value)
            fields[0] = stat.S_IFMT(value.st_mode) | 0o600
            fields[4] = 0
            return os.stat_result(fields)

        def safe_open(path, flags, mode=0o777):
            if os.name == "nt" and str(path) == str(self.folder) and flags == os.O_RDONLY:
                return real_open(self.file, os.O_RDWR, mode)
            return real_open(path, flags, mode)

        def windows_replace_model(source, destination):
            # Windows cannot rename over an open descriptor as Linux can. Model
            # resulting bytes here; the Linux helper still calls atomic replace.
            real_path(destination).write_bytes(real_path(source).read_bytes())
            real_path(source).unlink()

        fake_fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *args: None)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
            stack.enter_context(patch.dict(sys.modules, {"fcntl": fake_fcntl}))
            stack.enter_context(patch("pathlib.Path", side_effect=lambda value: self.root if value == "/etc" else real_path(value)))
            stack.enter_context(patch.object(Path, "lstat", metadata))
            stack.enter_context(patch.object(os, "fstat", descriptor_metadata))
            stack.enter_context(patch.object(os, "open", safe_open))
            stack.enter_context(patch.object(os, "geteuid", return_value=0, create=True))
            stack.enter_context(patch.object(os, "fchmod", create=True))
            stack.enter_context(patch.object(os, "fchown", create=True))
            stack.enter_context(patch.object(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), create=True))
            stack.enter_context(patch.object(os, "O_DIRECTORY", getattr(os, "O_DIRECTORY", 0), create=True))
            if replace_error:
                stack.enter_context(patch.object(os, "replace", side_effect=OSError("fixture rename failure")))
            elif os.name == "nt":
                stack.enter_context(patch.object(os, "replace", side_effect=windows_replace_model))
            stack.enter_context(contextlib.redirect_stdout(output))
            try:
                exec(HOST_CODE, {})
                failure = None
            except (SystemExit, OSError) as error:
                failure = str(error)
        return output.getvalue(), failure

    def test_only_hostname_changes_and_exact_backup_is_retained(self):
        output, failure = self.run_host()
        self.assertIsNone(failure)
        self.assertEqual(self.file.read_bytes(), self.original.replace(self.expected.encode(), b"eventharbor.irfanburakozer.com", 1))
        self.assertEqual((self.folder / "runtime.env.before-hostname-change").read_bytes(), self.original)
        self.assertFalse((self.folder / ".runtime.env.hostname-next").exists())
        self.assertNotIn("PRIVATE_PASSWORD", output)

    def test_wrong_expected_hostname_does_not_create_backup_or_modify_runtime(self):
        self.environment["ECONOMY_EXPECTED_HOSTNAME"] = "another.westus2.cloudapp.azure.com"
        _, failure = self.run_host()
        self.assertIsNotNone(failure)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse((self.folder / "runtime.env.before-hostname-change").exists())

    def test_existing_backup_and_partial_files_are_not_overwritten(self):
        for name in ("runtime.env.before-hostname-change", ".runtime.env.hostname-next"):
            with self.subTest(name=name):
                saved = self.folder / name
                saved.write_bytes(b"retained prior operation")
                _, failure = self.run_host()
                self.assertIsNotNone(failure)
                self.assertEqual(saved.read_bytes(), b"retained prior operation")
                self.assertEqual(self.file.read_bytes(), self.original)
                saved.unlink()

    def test_loose_runtime_or_directory_permissions_are_rejected(self):
        for path, mode in ((self.file, 0o644), (self.folder, 0o755)):
            with self.subTest(path=path):
                self.bad_modes[str(path)] = mode
                _, failure = self.run_host()
                self.assertIsNotNone(failure)
                self.assertEqual(self.file.read_bytes(), self.original)
                self.bad_modes.clear()

    def test_duplicate_hostname_is_rejected(self):
        value = self.original + f"PUBLIC_HOSTNAME={self.expected}\n".encode()
        self.file.write_bytes(value)
        _, failure = self.run_host()
        self.assertIsNotNone(failure)
        self.assertEqual(self.file.read_bytes(), value)

    def test_failed_atomic_replace_preserves_original_and_backup(self):
        _, failure = self.run_host(replace_error=True)
        self.assertIsNotNone(failure)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertEqual((self.folder / "runtime.env.before-hostname-change").read_bytes(), self.original)

    def test_wrong_final_domain_is_rejected(self):
        self.environment["ECONOMY_PUBLIC_HOSTNAME"] = "pulseexchange.irfanburakozer.com"
        _, failure = self.run_host()
        self.assertIsNotNone(failure)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_both_projects_and_line_endings_are_supported(self):
        self.environment["ECONOMY_PROJECT"] = "pulseexchange"
        self.environment["ECONOMY_PUBLIC_HOSTNAME"] = "pulseexchange.irfanburakozer.com"
        self.folder = self.root / "pulseexchange"
        self.folder.mkdir(mode=0o700)
        self.file = self.folder / "runtime.env"
        self.original = self.original.replace(b"EVENTHARBOR_", b"PULSEEXCHANGE_").replace(b"\n", b"\r\n")
        self.file.write_bytes(self.original)
        self.file.chmod(0o600)
        _, failure = self.run_host()
        self.assertIsNone(failure)
        self.assertEqual(self.file.read_bytes(), self.original.replace(self.expected.encode(), b"pulseexchange.irfanburakozer.com", 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
