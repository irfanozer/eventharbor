# Separate GitHub identity for economy VM deployment

The additive `scripts/azure-economy/configure-github-oidc.ps1` supports both
projects. It does not edit original deployment workflows, repository secrets,
production environment credentials, or existing repository OIDC templates.

Without `-Apply`, it only reads and validates GitHub and Azure configuration.
Use an Azure account that can create managed identities and assign the reviewed
role, and a GitHub account with administrator access to the selected repository.
The West US 2 foundation and the project's VM must already exist.

```powershell
.\scripts\azure-economy\configure-github-oidc.ps1 `
  -SubscriptionId "83099284-9ad4-4140-b8fe-8388b6d98a98" `
  -Project "eventharbor" `
  -GitHubRepository "irfanozer/eventharbor"
```

For PulseExchange, use `-Project "pulseexchange"` and
`-GitHubRepository "irfanozer/pulse-exchange"`. After reviewing the read-only
result, repeat the same command with `-Apply` to configure access.

Each project receives a different user-assigned identity in `rg-demos-economy`.
Its only Azure role is Virtual Machine Contributor on its exact project VM,
not on the resource group, other VM, network, or database. Existing identities
with mismatched tags, broader permissions, or additional trust are rejected.

The `azure-economy` GitHub environment must permit only the `main` branch.
Existing protection rules are checked, not overwritten. Its three Azure IDs are
written only as environment secrets. Resource-group and VM settings are also
environment-scoped. Ownership markers support safe repeated setup. The
`ECONOMY_DEPLOYMENT_ENABLED` flag is always left `false`. This script does not
start a deployment, enable a workflow, or turn the flag on.

GitHub repository and owner IDs are read from the API. The current OIDC template
must use default claims or exactly `repo,context`, with verified immutable
subjects. New repositories created after July 15, 2026 use GitHub's documented
immutable default; older repositories require the explicit immutable setting.
Other templates stop for review so old trust relationships are not broken.

After database restore, runtime configuration, and manual smoke checks are
complete, an operator can separately enable the environment flag and manually
run **Deploy economy VM** with tested immutable image digests. A successful
configuration run is not proof that OIDC login or application deployment works;
those require the real workflow run.

Offline tests:

```powershell
pwsh -NoProfile -File tests/deployment/test_economy_oidc.ps1
```

References: [GitHub OIDC subject rules](https://docs.github.com/en/actions/reference/security/oidc),
[GitHub OIDC API](https://docs.github.com/en/rest/actions/oidc),
[Azure VM Contributor role](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/compute#virtual-machine-contributor).
