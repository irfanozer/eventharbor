#Requires -Version 7.4
# Offline stateful fixtures replace all GitHub and Azure calls.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../scripts/azure-economy/configure-github-oidc.ps1'))
$tokens = $null
$problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw 'Economy OIDC helper has syntax errors.' }
foreach ($function in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Set-Item -LiteralPath "Function:$($function.Name)" -Value $function.Body.GetScriptBlock()
}
$script:checks = 0
function Assert-Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}
function Convert-Fixture($Value) { return $Value | ConvertTo-Json -Depth 30 | ConvertFrom-Json -Depth 30 }
function Reset-Fixture([string]$Project = 'eventharbor') {
    $short = if ($Project -eq 'eventharbor') { 'eh' } else { 'px' }
    $repoName = if ($Project -eq 'eventharbor') { 'eventharbor' } else { 'pulse-exchange' }
    $script:settings = @{ SubscriptionId = [guid]'83099284-9ad4-4140-b8fe-8388b6d98a98'; Project = $Project; GitHubRepository = "irfanozer/$repoName" }
    $script:groupId = "/subscriptions/$($settings.SubscriptionId)/resourceGroups/rg-demos-economy"
    $script:vmId = "$script:groupId/providers/Microsoft.Compute/virtualMachines/vm-$short-demos-economy"
    $script:identityId = "$script:groupId/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-$short-github-economy"
    $script:tenantId = '00000000-0000-4000-8000-000000000010'
    $script:repository = @{ full_name = $settings.GitHubRepository; name = $repoName; id = 123456; owner = @{ login = 'irfanozer'; id = 56190015 }; created_at = '2026-08-20T00:00:00Z'; default_branch = 'main'; permissions = @{ admin = $true } }
    $script:customization = @{ use_default = $true }
    $script:identity = $null; $script:credentials = @(); $script:roles = @()
    $script:environment = $null; $script:branches = @(); $script:variables = @{}; $script:secrets = @{}
    $script:readFailure = $false
    $script:writes = [System.Collections.Generic.List[object]]::new()
}
function Invoke-EconomyOidcGitHub {
    param([string[]]$Arguments, [string]$InputText = '')
    if ($Arguments[0] -in @('variable', 'secret')) {
        Assert-Check ($Arguments -contains '--env' -and $Arguments[[Array]::IndexOf($Arguments, '--env') + 1] -ceq 'azure-economy') 'Settings must only be written to azure-economy.'
        Assert-Check ($Arguments -contains '--repo' -and $Arguments[[Array]::IndexOf($Arguments, '--repo') + 1] -ceq $settings.GitHubRepository) 'Wrong settings repository.'
        $script:writes.Add(@{ Service = 'github'; Arguments = $Arguments; Input = $InputText })
        $name = $Arguments[2]
        if ($Arguments[0] -eq 'variable') { $script:variables[$name] = $Arguments[[Array]::IndexOf($Arguments, '--body') + 1] }
        else { Assert-Check ($Arguments -notcontains '--body' -and $InputText) 'Azure IDs must be passed to gh secret set on stdin.'; $script:secrets[$name] = $InputText }
        return
    }
    $method = $Arguments[[Array]::IndexOf($Arguments, '--method') + 1]
    $path = $Arguments[3]
    if ($method -ne 'GET') {
        $script:writes.Add(@{ Service = 'github'; Arguments = $Arguments; Input = $InputText })
        if ($path -eq "repos/$($settings.GitHubRepository)/environments/azure-economy" -and $method -eq 'PUT') {
            $body = $InputText | ConvertFrom-Json
            $script:environment = @{ name = 'azure-economy'; deployment_branch_policy = $body.deployment_branch_policy }
            return Convert-Fixture $script:environment
        }
        if ($path.EndsWith('/environments/azure-economy/deployment-branch-policies') -and $method -eq 'POST') {
            Assert-Check ($Arguments -contains 'name=main' -and $Arguments -contains 'type=branch') 'Only main branch may be allowed.'
            $script:branches = @(@{ name = 'main'; type = 'branch' })
            return
        }
        throw 'Unexpected GitHub mutation.'
    }
    if ($script:readFailure) { throw 'Fixture: GitHub forbidden.' }
    if ($path -eq "repos/$($settings.GitHubRepository)") { return Convert-Fixture $script:repository }
    if ($path.EndsWith('/actions/oidc/customization/sub')) { return Convert-Fixture $script:customization }
    if ($path -match '/environments\?per_page=30$') { return Convert-Fixture @{ total_count = $(if ($null -eq $script:environment) { 0 } else { 1 }); environments = @($(if ($null -ne $script:environment) { $script:environment })) } }
    if ($path -match '/deployment-branch-policies\?per_page=30$') { return Convert-Fixture @{ total_count = $script:branches.Count; branch_policies = $script:branches } }
    if ($path -match '/variables\?per_page=30$') { return Convert-Fixture @{ total_count = $script:variables.Count; variables = @($script:variables.GetEnumerator() | ForEach-Object { @{ name = $_.Key; value = $_.Value } }) } }
    if ($path -match '/secrets\?per_page=30$') { return Convert-Fixture @{ total_count = $script:secrets.Count; secrets = @($script:secrets.Keys | ForEach-Object { @{ name = $_ } }) } }
    if ($path.EndsWith('/environments/azure-economy')) { return Convert-Fixture $script:environment }
    throw 'Unexpected GitHub read.'
}
function Invoke-EconomyOidcAzure {
    param([string[]]$Arguments)
    Assert-Check ($Arguments -contains '--subscription' -and $Arguments[[Array]::IndexOf($Arguments, '--subscription') + 1] -eq "$($settings.SubscriptionId)") 'Azure calls require explicit subscription.'
    $operation = ($Arguments | Select-Object -First 3) -join ' '
    $tags = @{ application = 'EventHarbor-PulseExchange'; costProfile = 'economy'; environment = 'economy'; managedBy = 'Bicep' }
    switch -Wildcard ($operation) {
        'account show *' { return Convert-Fixture @{ id = "$($settings.SubscriptionId)"; tenantId = $script:tenantId; state = 'Enabled' } }
        'group show *' { return Convert-Fixture @{ id = $script:groupId; location = 'westus2'; tags = $tags } }
        'vm show *' { $tags.application = $settings.Project; return Convert-Fixture @{ id = $script:vmId; location = 'westus2'; tags = $tags } }
        'identity list *' { if ($null -ne $script:identity) { return Convert-Fixture $script:identity }; return }
        'identity create *' {
            $script:writes.Add(@{ Service = 'azure'; Arguments = $Arguments })
            $identityTags = @{}
            foreach ($tag in $Arguments[([Array]::IndexOf($Arguments, '--tags') + 1)..($Arguments.Count - 1)]) { $key, $value = $tag.Split('=', 2); $identityTags[$key] = $value }
            $script:identity = @{ id = $script:identityId; location = 'westus2'; tenantId = $script:tenantId; clientId = '00000000-0000-4000-8000-000000000011'; principalId = '00000000-0000-4000-8000-000000000012'; tags = $identityTags }
            return Convert-Fixture $script:identity
        }
        'identity federated-credential list' { foreach ($credential in $script:credentials) { Convert-Fixture $credential }; return }
        'identity federated-credential create' {
            $script:writes.Add(@{ Service = 'azure'; Arguments = $Arguments })
            $script:credentials = @(@{ name = 'github-azure-economy'; issuer = 'https://token.actions.githubusercontent.com'; subject = $Arguments[[Array]::IndexOf($Arguments, '--subject') + 1]; audiences = @('api://AzureADTokenExchange') })
            return
        }
        'role assignment list' { foreach ($role in $script:roles) { Convert-Fixture $role }; return }
        'role assignment create' {
            $script:writes.Add(@{ Service = 'azure'; Arguments = $Arguments })
            Assert-Check ($Arguments[[Array]::IndexOf($Arguments, '--scope') + 1] -ceq $script:vmId) 'Never grant permissions at resource group scope.'
            $roleId = $Arguments[[Array]::IndexOf($Arguments, '--role') + 1]
            Assert-Check ($roleId -ceq '9980e02c-c2be-4d73-94e8-173b1dc7cf3c') 'Only VM Contributor is permitted.'
            $script:roles = @(@{ scope = $script:vmId; principalId = $script:identity.principalId; roleDefinitionId = "/subscriptions/$($settings.SubscriptionId)/providers/Microsoft.Authorization/roleDefinitions/$roleId" })
            return
        }
        default { throw 'Unexpected Azure operation.' }
    }
}

foreach ($project in @('eventharbor', 'pulseexchange')) {
    Reset-Fixture $project
    $result = Invoke-ConfigureEconomyOidc @settings
    Assert-Check ($result.Action -ceq 'ReadOnlyInspectionPassed' -and $script:writes.Count -eq 0) 'Default mode must not write to either service.'
    Assert-Check ($result.Subject -ceq "repo:irfanozer@56190015/$($script:repository.name)@123456:environment:azure-economy") 'Immutable repo and owner IDs must be included in the subject.'
    $result = Invoke-ConfigureEconomyOidc @settings -Apply
    Assert-Check ($result.Action -ceq 'EconomyOidcConfigured' -and -not $result.DeploymentEnabled -and -not $result.ExistingProductionCredentialsChanged) 'Safe apply must leave deployments disabled.'
    Assert-Check ($script:variables['ECONOMY_DEPLOYMENT_ENABLED'] -ceq 'false' -and $script:secrets.Count -eq 3) 'Only expected environment identity secrets should be configured.'
    Assert-Check (@($script:writes | Where-Object { ($_.Arguments -join '|') -match 'customization/sub' }).Count -eq 0) 'Existing OIDC subject templates cannot change.'
    $script:writes.Clear()
    $null = Invoke-ConfigureEconomyOidc @settings -Apply
    Assert-Check (@($script:writes | Where-Object { $_.Service -eq 'azure' }).Count -eq 0) 'Idempotent setup must not recreate identity, roles, or trust.'
}

$invalidCases = [ordered]@{
    'GitHub read failure' = { $script:readFailure = $true }
    'mutable subject' = { $script:customization.use_immutable_subject = $false }
    'older unknown immutable status' = { $script:repository.created_at = '2026-01-01T00:00:00Z' }
    'custom incompatible subject' = { $script:customization = @{ use_default = $false; include_claim_keys = @('repository_id') } }
    'missing repository id' = { $script:repository.id = $null }
    'different immutable prefix' = { $script:customization.sub_claim_prefix = 'repo:irfanozer@56190015/eventharbor@777' }
}
foreach ($case in $invalidCases.GetEnumerator()) {
    Reset-Fixture
    & $case.Value
    $failed = $false
    try { $null = Invoke-ConfigureEconomyOidc @settings -Apply } catch { $failed = $true }
    Assert-Check ($failed -and $script:writes.Count -eq 0) "Unsafe OIDC preflight accepted: $($case.Key)"
}

$existingCases = [ordered]@{
    'foreign identity ownership' = { $script:identity.tags.githubRepositoryId = '777' }
    'broader role scope' = { $script:roles[0].scope = $script:groupId }
    'broader role permission' = { $script:roles[0].roleDefinitionId = '/roles/owner' }
    'additional federated trust' = { $script:credentials += $script:credentials[0] }
    'different subject' = { $script:credentials[0].subject = 'repo:other/name:environment:azure-economy' }
    'unrestricted environment' = { $script:environment.deployment_branch_policy.custom_branch_policies = $false }
    'main tag is not main branch' = { $script:branches[0].type = 'tag' }
    'foreign environment settings' = { $script:variables['ECONOMY_OIDC_OWNER'] = 'someone-else' }
    'enabled deployment' = { $script:variables['ECONOMY_DEPLOYMENT_ENABLED'] = 'true' }
}
foreach ($case in $existingCases.GetEnumerator()) {
    Reset-Fixture
    $null = Invoke-ConfigureEconomyOidc @settings -Apply
    $script:writes.Clear()
    & $case.Value
    $failed = $false
    try { $null = Invoke-ConfigureEconomyOidc @settings -Apply } catch { $failed = $true }
    Assert-Check ($failed -and $script:writes.Count -eq 0) "Existing unsafe configuration accepted: $($case.Key)"
}
Write-Output "ECONOMY_OIDC_OFFLINE_CHECKS_PASS: $checks checks; no Azure or GitHub calls made."
