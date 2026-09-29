"""Offline regression checks for the additive shared-server deployment profile."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "EVENTHARBOR" if ROOT.name == "eventharbor" else "PULSEEXCHANGE"
PROFILE = ROOT / "infra" / "azure-shared-postgres"
ORIGINAL = ROOT / "infra" / "azure"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() + "\n"


def pool_fields() -> str:
    return (
        "            {\n"
        f"              name: '{PREFIX}_DATABASE_POOL_SIZE'\n"
        "              value: '2'\n"
        "            }\n"
        "            {\n"
        f"              name: '{PREFIX}_DATABASE_MAX_OVERFLOW'\n"
        "              value: '0'\n"
        "            }\n"
    )


def add_pool_fields(source: str) -> str:
    anchor = (
        "            {\n"
        f"              name: '{PREFIX}_DATABASE_URL'\n"
        "              secretRef: 'database-url'\n"
        "            }\n"
    )
    assert source.count(anchor) == 1
    return source.replace(anchor, anchor + pool_fields())


class DeploymentProfileTests(unittest.TestCase):
    def test_apps_have_only_approved_changes(self):
        expected = read(ORIGINAL / "apps.bicep")
        expected = re.sub(
            rf"(name: '{PREFIX}_DATABASE_POOL_SIZE'\n\s+value: )'3'",
            r"\g<1>'2'", expected,
        )
        expected = re.sub(
            rf"(name: '{PREFIX}_DATABASE_MAX_OVERFLOW'\n\s+value: )'[12]'",
            r"\g<1>'0'", expected,
        )
        resource_pair = "cpu: json('0.5')\n            memory: '1Gi'"
        self.assertEqual(expected.count(resource_pair), 1)
        expected = expected.replace(
            resource_pair, "cpu: json('0.25')\n            memory: '0.5Gi'"
        )
        if PREFIX == "PULSEEXCHANGE":
            expected = expected.replace(
                "name: 'PULSEEXCHANGE_PROCESSOR_POLL_INTERVAL_MS'\n              value: '250'",
                "name: 'PULSEEXCHANGE_PROCESSOR_POLL_INTERVAL_MS'\n              value: '500'",
            )
            before, maintenance = expected.split("resource maintenanceJob ", 1)
            expected = before + "resource maintenanceJob " + add_pool_fields(maintenance)
        self.assertEqual(read(PROFILE / "apps.bicep"), expected)

    def test_migration_has_only_explicit_pool_changes(self):
        self.assertEqual(
            read(PROFILE / "migration.bicep"),
            add_pool_fields(read(ORIGINAL / "migration.bicep")),
        )

    def test_every_direct_database_consumer_has_bounded_pool(self):
        consumers = 0
        for filename in ("apps.bicep", "migration.bicep"):
            for block in re.split(r"(?m)^resource ", read(PROFILE / filename)):
                if f"name: '{PREFIX}_DATABASE_URL'" not in block:
                    continue
                consumers += 1
                for setting, value in (("POOL_SIZE", "2"), ("MAX_OVERFLOW", "0")):
                    self.assertRegex(
                        block, rf"name: '{PREFIX}_DATABASE_{setting}'\s+value: '{value}'"
                    )
        self.assertEqual(consumers, 4)

    def test_workflow_only_changes_profile_paths_and_guards(self):
        actual = read(ROOT / ".github/workflows/deploy-shared-postgres.yml")
        self.assertEqual(actual.count("vars.AZURE_SHARED_DEPLOYMENT_ENABLED == 'true'"), 2)
        guard = """      - name: Require only the shared PostgreSQL deployment profile
        env:
          ORIGINAL_DEPLOYMENT_ENABLED: ${{ vars.AZURE_DEPLOYMENT_ENABLED }}
        shell: bash
        run: |
          set -euo pipefail
          if [[ "$ORIGINAL_DEPLOYMENT_ENABLED" == "true" ]]; then
            echo "Set AZURE_DEPLOYMENT_ENABLED to false before enabling the shared PostgreSQL profile." >&2
            exit 1
          fi

"""
        self.assertEqual(actual.count(guard), 2)
        normalized = actual.replace(guard, "")
        normalized = normalized.replace(
            "name: Publish images and deploy shared PostgreSQL",
            "name: Publish images and deploy production",
        )
        normalized = normalized.replace(
            "    if: >-\n"
            "      vars.AZURE_SHARED_DEPLOYMENT_ENABLED == 'true' &&\n"
            "      ((github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') ||\n"
            "       (github.event.workflow_run.conclusion == 'success' &&\n"
            "        github.event.workflow_run.event == 'push' &&\n"
            "        github.event.workflow_run.head_branch == 'main'))",
            "    if: >-\n"
            "      (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') ||\n"
            "      (github.event.workflow_run.conclusion == 'success' &&\n"
            "       github.event.workflow_run.event == 'push' &&\n"
            "       github.event.workflow_run.head_branch == 'main')",
        )
        normalized = normalized.replace(
            "    if: vars.AZURE_SHARED_DEPLOYMENT_ENABLED == 'true'",
            "    if: vars.AZURE_DEPLOYMENT_ENABLED == 'true'",
        )
        for name in ("apps", "migration"):
            normalized = normalized.replace(
                f"infra/azure-shared-postgres/{name}.bicep", f"infra/azure/{name}.bicep"
            )
        self.assertEqual(normalized, read(ROOT / ".github/workflows/deploy-production.yml"))


if __name__ == "__main__":
    unittest.main()
