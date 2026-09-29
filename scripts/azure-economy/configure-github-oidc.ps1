#Requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][ValidateSet('eventharbor', 'pulseexchange')][string]$Project,
    [Parameter(Mandatory)][string]$GitHubRepository,
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-EconomyOidcValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object -or $null -eq $Object.PSObject.Properties[$Name]) { return $Default }
    return $Object.$Name
}

function Invoke-EconomyOidcAzure {
    param([string[]]$Arguments)
    Invoke-EconomyAzureJson -Arguments $Arguments -Sensitive
}

function Invoke-EconomyOidcGitHub {
    param([string[]]$Arguments, [string]$InputText = '')
    $command = Get-Command gh -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $command.Source
    $start.Environment['GH_HOST'] = 'github.com'
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputText) { $process.StandardInput.Write($InputText) }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(120000)) { $process.Kill($true); throw 'GitHub CLI timed out. Inspect the operation before retrying.' }
        $content = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "GitHub CLI failed with exit code $($process.ExitCode). No raw response or credential values are printed." }
        if (-not $content.Trim()) { return $null }
        try { return ConvertFrom-Json -InputObject $content -Depth 50 }
        catch { throw 'GitHub CLI returned an unexpected non-JSON response.' }
    } finally { $process.Dispose() }
}

function Get-EconomyOidcGitHubList {
    param([string]$Path, [string]$Property)
    $result = Invoke-EconomyOidcGitHub @('api', '--method', 'GET', "$Path`?per_page=30")
    $items = @(Get-EconomyOidcValue $result $Property @())
    if ($null -eq (Get-EconomyOidcValue $result 'total_count') -or $result.total_count -ne $items.Count -or $items.Count -gt 30) {
        throw 'GitHub inventory is incomplete or paginated. Inspect it before changing deployment access.'
    }
    return $items
}

function Get-EconomyOidcSubject {
    param($Repository, $Customization)
    if ([string]$Repository.owner.id -notmatch '^[1-9][0-9]*$' -or [string]$Repository.id -notmatch '^[1-9][0-9]*$') { throw 'GitHub did not provide immutable owner and repository IDs.' }
    $immutable = Get-EconomyOidcValue $Customization 'use_immutable_subject'
    if ($null -ne $immutable -and $immutable -ne $true) { throw 'This repository has not enabled immutable OIDC subjects. Existing repository OIDC settings will not be changed.' }
    if ($null -eq $immutable) {
        # GitHub documents immutable defaults for repositories created after July 15, 2026.
        # Older repositories without an explicit immutable setting require separate verification.
        $created = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse([string](Get-EconomyOidcValue $Repository 'created_at'), [ref]$created) -or
            $created -lt [DateTimeOffset]'2026-07-16T00:00:00Z') { throw 'Immutable subject status cannot be established from repository metadata.' }
    }
    $useDefault = Get-EconomyOidcValue $Customization 'use_default'
    if ($useDefault -ne $true) {
        if ($useDefault -ne $false -or (@(Get-EconomyOidcValue $Customization 'include_claim_keys' @()) -join ',') -cne 'repo,context') {
            throw 'The existing OIDC subject template differs from repo/context. It must be reviewed without changing old deployment trust.'
        }
    }
    $prefix = "repo:$($Repository.owner.login)@$($Repository.owner.id)/$($Repository.name)@$($Repository.id)"
    $reportedPrefix = Get-EconomyOidcValue $Customization 'sub_claim_prefix'
    if ($null -ne $reportedPrefix -and $reportedPrefix -cne $prefix) { throw 'GitHub reported a subject prefix that does not match the immutable repository identifiers.' }
    return "${prefix}:environment:azure-economy"
}

function Assert-EconomyOidcTags {
    param($Resource, [hashtable]$Expected)
    $tags = Get-EconomyOidcValue $Resource 'tags'
    foreach ($entry in $Expected.GetEnumerator()) {
        if ((Get-EconomyOidcValue $tags $entry.Key) -cne $entry.Value) { throw "Ownership tag $($entry.Key) is missing or different. Existing resources are not adopted." }
    }
}

function Assert-EconomyOidcEnvironment {
    param($Environment, [object[]]$Branches)
    $policy = Get-EconomyOidcValue $Environment 'deployment_branch_policy'
    if ((Get-EconomyOidcValue $Environment 'name') -cne 'azure-economy' -or
        (Get-EconomyOidcValue $policy 'protected_branches') -ne $false -or
        (Get-EconomyOidcValue $policy 'custom_branch_policies') -ne $true -or
        $Branches.Count -ne 1 -or $Branches[0].name -cne 'main' -or $Branches[0].type -cne 'branch') {
        throw 'The azure-economy environment must allow only the main branch. Existing protections are not overwritten.'
    }
}

function Assert-EconomyOidcIdentity {
    param($Plan, $Identity, [object[]]$Credentials, [object[]]$Roles, [switch]$RequireReady)
    if ($Identity.id -ine $Plan.IdentityId -or $Identity.location -cne 'westus2' -or $Identity.tenantId -ine $Plan.TenantId -or
        $Identity.clientId -notmatch '^[0-9a-fA-F-]{36}$' -or $Identity.principalId -notmatch '^[0-9a-fA-F-]{36}$') { throw 'Unexpected economy deployment identity.' }
    Assert-EconomyOidcTags $Identity $Plan.IdentityTags
    if ($Credentials.Count -gt 1 -or $Roles.Count -gt 1) { throw 'The dedicated deployment identity has additional trust or permissions.' }
    foreach ($credential in $Credentials) {
        if ($credential.name -cne 'github-azure-economy' -or $credential.issuer -cne 'https://token.actions.githubusercontent.com' -or
            $credential.subject -cne $Plan.Subject -or (@($credential.audiences) -join ',') -cne 'api://AzureADTokenExchange') { throw 'Existing federated trust differs from the exact repository and economy environment.' }
    }
    foreach ($role in $Roles) {
        if ($role.scope -ine $Plan.VmId -or $role.principalId -ine $Identity.principalId -or
            $role.roleDefinitionId.Split('/')[-1] -ine '9980e02c-c2be-4d73-94e8-173b1dc7cf3c') { throw 'Deployment identity must have only Virtual Machine Contributor on its exact project VM.' }
    }
    if ($RequireReady -and ($Credentials.Count -ne 1 -or $Roles.Count -ne 1)) { throw 'Federated identity or VM-scoped permission is incomplete.' }
}

function Get-EconomyOidcIdentityAccess {
    param($Plan, $Identity)
    $credentials = @(Invoke-EconomyOidcAzure @('identity', 'federated-credential', 'list', '--subscription', $Plan.SubscriptionId,
        '--resource-group', 'rg-demos-economy', '--identity-name', $Plan.IdentityName))
    $roles = @(Invoke-EconomyOidcAzure @('role', 'assignment', 'list', '--subscription', $Plan.SubscriptionId,
        '--assignee-object-id', $Identity.principalId, '--all', '--include-inherited', '--fill-principal-name', 'false', '--fill-role-definition-name', 'false'))
    return [pscustomobject]@{ Credentials = $credentials; Roles = $roles }
}

function Invoke-ConfigureEconomyOidc {
    param([guid]$SubscriptionId, [string]$Project, [string]$GitHubRepository, [switch]$Apply)
    $expectedRepository = switch ($Project) { 'eventharbor' { 'irfanozer/eventharbor' }; 'pulseexchange' { 'irfanozer/pulse-exchange' }; default { throw 'Invalid project.' } }
    $short = if ($Project -ceq 'eventharbor') { 'eh' } else { 'px' }
    if ("$SubscriptionId" -cne '83099284-9ad4-4140-b8fe-8388b6d98a98' -or $GitHubRepository -cne $expectedRepository) { throw 'Use the exact reviewed subscription and repository for this project.' }
    $repository = Invoke-EconomyOidcGitHub @('api', '--method', 'GET', "repos/$GitHubRepository")
    if ($repository.full_name -cne $expectedRepository -or $repository.default_branch -cne 'main' -or $repository.permissions.admin -ne $true) { throw 'Repository identity, main default branch, or administrator access could not be verified.' }
    $customization = Invoke-EconomyOidcGitHub @('api', '--method', 'GET', "repos/$GitHubRepository/actions/oidc/customization/sub")
    $subject = Get-EconomyOidcSubject $repository $customization
    $account = Invoke-EconomyOidcAzure @('account', 'show', '--subscription', "$SubscriptionId")
    if ($account.id -ine "$SubscriptionId" -or $account.state -cne 'Enabled' -or $account.tenantId -notmatch '^[0-9a-fA-F-]{36}$') { throw 'Wrong or disabled Azure subscription.' }
    $groupId = "/subscriptions/$SubscriptionId/resourceGroups/rg-demos-economy"
    $identityName = "id-$short-github-economy"
    $plan = [pscustomobject]@{
        SubscriptionId = "$SubscriptionId"; TenantId = $account.tenantId; Repository = $repository.full_name
        Subject = $subject; IdentityName = $identityName; IdentityId = "$groupId/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$identityName"
        VmName = "vm-$short-demos-economy"; VmId = "$groupId/providers/Microsoft.Compute/virtualMachines/vm-$short-demos-economy"
        IdentityTags = @{ application = $Project; environment = 'economy'; costProfile = 'economy'; managedBy = 'economy-oidc-script'; purpose = 'github-vm-deployment'; githubRepository = $repository.full_name; githubOwnerId = [string]$repository.owner.id; githubRepositoryId = [string]$repository.id }
    }
    $ownershipMarker = "$($repository.owner.id):$($repository.id):$($plan.IdentityId)"
    $group = Invoke-EconomyOidcAzure @('group', 'show', '--subscription', "$SubscriptionId", '--name', 'rg-demos-economy')
    $vm = Invoke-EconomyOidcAzure @('vm', 'show', '--subscription', "$SubscriptionId", '--resource-group', 'rg-demos-economy', '--name', $plan.VmName)
    if ($group.id -ine $groupId -or $group.location -cne 'westus2' -or $vm.id -ine $plan.VmId -or $vm.location -cne 'westus2') { throw 'The exact West US 2 economy group and project VM are required.' }
    Assert-EconomyOidcTags $group @{ application = 'EventHarbor-PulseExchange'; costProfile = 'economy'; environment = 'economy'; managedBy = 'Bicep' }
    Assert-EconomyOidcTags $vm @{ application = $Project; costProfile = 'economy'; environment = 'economy'; managedBy = 'Bicep' }
    $identities = @(Invoke-EconomyOidcAzure @('identity', 'list', '--subscription', "$SubscriptionId", '--resource-group', 'rg-demos-economy'))
    $matching = @($identities | Where-Object { $_.id -ieq $plan.IdentityId })
    if ($matching.Count -gt 1) { throw 'Ambiguous deployment identity inventory.' }
    $identity = if ($matching.Count -eq 1) { $matching[0] } else { $null }
    $access = [pscustomobject]@{ Credentials = @(); Roles = @() }
    if ($null -ne $identity) {
        Assert-EconomyOidcIdentity $plan $identity @() @()
        $access = Get-EconomyOidcIdentityAccess $plan $identity
        Assert-EconomyOidcIdentity $plan $identity $access.Credentials $access.Roles
    }
    $environments = @(Get-EconomyOidcGitHubList "repos/$GitHubRepository/environments" 'environments')
    $matchingEnvironments = @($environments | Where-Object { $_.name -ieq 'azure-economy' })
    if ($matchingEnvironments.Count -gt 1) { throw 'Ambiguous economy environment inventory.' }
    $environment = if ($matchingEnvironments.Count -eq 1) { $matchingEnvironments[0] } else { $null }
    $variables = @(); $secrets = @()
    if ($null -ne $environment) {
        $branches = @(Get-EconomyOidcGitHubList "repos/$GitHubRepository/environments/azure-economy/deployment-branch-policies" 'branch_policies')
        Assert-EconomyOidcEnvironment $environment $branches
        $variables = @(Get-EconomyOidcGitHubList "repos/$GitHubRepository/environments/azure-economy/variables" 'variables')
        $secrets = @(Get-EconomyOidcGitHubList "repos/$GitHubRepository/environments/azure-economy/secrets" 'secrets')
        $existing = @{}
        foreach ($variable in $variables) { $existing[$variable.name] = $variable.value }
        if ($existing.Count -or $secrets.Count) {
            if ($existing['ECONOMY_OIDC_OWNER'] -cne $ownershipMarker) { throw 'Existing environment settings lack the matching economy ownership marker. They were not overwritten.' }
        }
        if ($existing.ContainsKey('ECONOMY_DEPLOYMENT_ENABLED') -and $existing['ECONOMY_DEPLOYMENT_ENABLED'] -cne 'false') { throw 'Disable the economy deployment flag before reconfiguring its identity.' }
    }
    if (-not $Apply) {
        return [pscustomobject]@{ Action = 'ReadOnlyInspectionPassed'; Project = $Project; Repository = $repository.full_name; Subject = $subject; IdentityId = $plan.IdentityId; VmScope = $plan.VmId; WouldCreateIdentity = ($null -eq $identity); WouldCreateEnvironment = ($null -eq $environment); DeploymentEnabled = $false }
    }
    if ($null -eq $environment) {
        $body = @{ deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true } } | ConvertTo-Json -Compress
        $null = Invoke-EconomyOidcGitHub -Arguments @('api', '--method', 'PUT', "repos/$GitHubRepository/environments/azure-economy", '--input', '-') -InputText $body
        $null = Invoke-EconomyOidcGitHub @('api', '--method', 'POST', "repos/$GitHubRepository/environments/azure-economy/deployment-branch-policies", '-f', 'name=main', '-f', 'type=branch')
    }
    $verifiedEnvironment = Invoke-EconomyOidcGitHub @('api', '--method', 'GET', "repos/$GitHubRepository/environments/azure-economy")
    $verifiedBranches = @(Get-EconomyOidcGitHubList "repos/$GitHubRepository/environments/azure-economy/deployment-branch-policies" 'branch_policies')
    Assert-EconomyOidcEnvironment $verifiedEnvironment $verifiedBranches
    # A single atomic marker makes partial setup safely recognizable on retry.
    $null = Invoke-EconomyOidcGitHub @('variable', 'set', 'ECONOMY_OIDC_OWNER', '--repo', $GitHubRepository, '--env', 'azure-economy', '--body', $ownershipMarker)
    # Disable before granting new cloud trust. Never enable automatically.
    $null = Invoke-EconomyOidcGitHub @('variable', 'set', 'ECONOMY_DEPLOYMENT_ENABLED', '--repo', $GitHubRepository, '--env', 'azure-economy', '--body', 'false')
    if ($null -eq $identity) {
        $tagArguments = @($plan.IdentityTags.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })
        $identity = Invoke-EconomyOidcAzure (@('identity', 'create', '--subscription', "$SubscriptionId", '--resource-group', 'rg-demos-economy', '--name', $plan.IdentityName, '--location', 'westus2', '--tags') + $tagArguments)
        Assert-EconomyOidcIdentity $plan $identity @() @()
    }
    if ($access.Credentials.Count -eq 0) {
        $null = Invoke-EconomyOidcAzure @('identity', 'federated-credential', 'create', '--subscription', "$SubscriptionId", '--resource-group', 'rg-demos-economy', '--identity-name', $plan.IdentityName,
            '--name', 'github-azure-economy', '--issuer', 'https://token.actions.githubusercontent.com', '--subject', $plan.Subject, '--audiences', 'api://AzureADTokenExchange')
    }
    if ($access.Roles.Count -eq 0) {
        $null = Invoke-EconomyOidcAzure @('role', 'assignment', 'create', '--subscription', "$SubscriptionId", '--assignee-object-id', $identity.principalId, '--assignee-principal-type', 'ServicePrincipal',
            '--role', '9980e02c-c2be-4d73-94e8-173b1dc7cf3c', '--scope', $plan.VmId)
    }
    $verifiedAccess = Get-EconomyOidcIdentityAccess $plan $identity
    Assert-EconomyOidcIdentity $plan $identity $verifiedAccess.Credentials $verifiedAccess.Roles -RequireReady
    $publishedVariables = @{
        ECONOMY_IDENTITY_RESOURCE_ID = $plan.IdentityId; ECONOMY_GITHUB_REPOSITORY_ID = [string]$repository.id
        ECONOMY_GITHUB_OWNER_ID = [string]$repository.owner.id; ECONOMY_RESOURCE_GROUP = 'rg-demos-economy'; ECONOMY_VM_NAME = $plan.VmName
    }
    foreach ($entry in $publishedVariables.GetEnumerator()) {
        $null = Invoke-EconomyOidcGitHub @('variable', 'set', $entry.Key, '--repo', $GitHubRepository, '--env', 'azure-economy', '--body', $entry.Value)
    }
    foreach ($entry in @{ AZURE_CLIENT_ID = $identity.clientId; AZURE_TENANT_ID = $plan.TenantId; AZURE_SUBSCRIPTION_ID = "$SubscriptionId" }.GetEnumerator()) {
        $null = Invoke-EconomyOidcGitHub -Arguments @('secret', 'set', $entry.Key, '--repo', $GitHubRepository, '--env', 'azure-economy') -InputText $entry.Value
    }
    return [pscustomobject]@{ Action = 'EconomyOidcConfigured'; Project = $Project; Repository = $repository.full_name; Subject = $subject; IdentityId = $plan.IdentityId; VmScope = $plan.VmId; DeploymentEnabled = $false; ExistingProductionCredentialsChanged = $false }
}

. (Join-Path $PSScriptRoot 'common.ps1')
Invoke-ConfigureEconomyOidc @PSBoundParameters
