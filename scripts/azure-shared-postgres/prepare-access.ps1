#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [switch]$CheckOnly,
    [switch]$Apply,
    [switch]$ConfirmCosts
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-PgAccessAzureJson {
    param([Parameter(Mandatory)][string[]]$Arguments, [int]$TimeoutSeconds = 120)
    $command = Get-Command az -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $command.Source
    if ($IsWindows -and $command.Source.EndsWith('.cmd')) {
        $cliPython = [IO.Path]::GetFullPath((Join-Path (Split-Path $command.Source) '../python.exe'))
        if (-not (Test-Path -LiteralPath $cliPython -PathType Leaf)) { throw 'Use the official Azure CLI installation.' }
        $start.FileName = $cliPython
        $start.ArgumentList.Add('-IBm')
        $start.ArgumentList.Add('azure.cli')
    }
    foreach ($argument in $Arguments + @('--only-show-errors', '--output', 'json')) { $start.ArgumentList.Add($argument) }
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill($true)
            throw 'Azure CLI timed out. If applying, inspect deployment pg-copy-access before retrying; the server operation may still be running.'
        }
        $content = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "Azure CLI failed with exit code $($process.ExitCode). No raw response or credentials are printed." }
        if (-not $content.Trim()) { return $null }
        try { return ConvertFrom-Json -InputObject $content -Depth 100 }
        catch { throw 'Azure CLI returned invalid JSON.' }
    } finally { $process.Dispose() }
}

function Get-PgAccessValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object -or $null -eq $Object.PSObject.Properties[$Name]) { return $Default }
    return $Object.$Name
}

function Get-PgAccessPlan {
    param([guid]$SubscriptionId)
    if ("$SubscriptionId" -ne '83099284-9ad4-4140-b8fe-8388b6d98a98') { throw 'This migration is restricted to the explicitly reviewed demo subscription.' }
    $prefix = "/subscriptions/$SubscriptionId"
    $eventGroup = "$prefix/resourceGroups/rg-eventharbor-prod"
    $pulseGroup = "$prefix/resourceGroups/rg-pulseexchange-prod"
    $backupGroup = "$prefix/resourceGroups/rg-demos-db-migration"
    $eventVnet = "$eventGroup/providers/Microsoft.Network/virtualNetworks/vnet-eventharbor-prod"
    $pulseVnet = "$pulseGroup/providers/Microsoft.Network/virtualNetworks/vnet-pulseexchange-prod"
    $zone = "$eventGroup/providers/Microsoft.Network/privateDnsZones/eventharbor.postgres.database.azure.com"
    $storage = "$backupGroup/providers/Microsoft.Storage/storageAccounts/stpgcopy830992849ad4"
    return [pscustomobject]@{
        SubscriptionId = "$SubscriptionId"; Location = 'eastus2'; GroupName = 'rg-demos-db-migration'; GroupId = $backupGroup
        EventVnetId = $eventVnet; PulseVnetId = $pulseVnet; ZoneId = $zone
        EventPeeringId = "$eventVnet/virtualNetworkPeerings/shared-postgres-to-pulseexchange"
        PulsePeeringId = "$pulseVnet/virtualNetworkPeerings/shared-postgres-to-eventharbor"
        LinkId = "$zone/virtualNetworkLinks/pulseexchange-shared-postgres"
        StorageName = 'stpgcopy830992849ad4'; StorageId = $storage; BlobServiceId = "$storage/blobServices/default"
        ContainerId = "$storage/blobServices/default/containers/pulseexchange-backups"
        IdentityId = "$backupGroup/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-px-pgcopy-prod"
        BlobRoleId = "$prefix/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
        Tags = [pscustomobject]@{ application = 'EventHarbor-PulseExchange'; environment = 'prod'; managedBy = 'Bicep'; purpose = 'shared-postgres-migration' }
        DeploymentIds = @(
            "$eventGroup/providers/Microsoft.Resources/deployments/pg-copy-eh-peering",
            "$pulseGroup/providers/Microsoft.Resources/deployments/pg-copy-px-peering",
            "$eventGroup/providers/Microsoft.Resources/deployments/pg-copy-dns",
            "$backupGroup/providers/Microsoft.Resources/deployments/pg-copy-backup"
        )
    }
}

function Get-PgAccessResource {
    param([string]$Id, [string]$ApiVersion)
    return Invoke-PgAccessAzureJson -Arguments @('rest', '--method', 'get', '--url', "https://management.azure.com${Id}?api-version=$ApiVersion")
}

function Get-PgAccessList {
    param([string]$Id, [string]$ApiVersion)
    $result = Get-PgAccessResource -Id $Id -ApiVersion $ApiVersion
    if ($null -eq $result -or $null -eq $result.PSObject.Properties['value']) { throw 'Azure returned an incomplete resource list.' }
    if (Get-PgAccessValue $result 'nextLink') { throw 'Inventory requires another page; refusing an incomplete ownership check.' }
    return @(Get-PgAccessValue $result 'value' @())
}

function Get-PgAccessContainers {
    param($Plan)
    $listed = @(Get-PgAccessList "$($Plan.BlobServiceId)/containers" '2023-05-01')
    if ($listed.Count -gt 1) { throw 'Unexpected additional storage containers.' }
    foreach ($item in $listed) {
        if ((Get-PgAccessValue $item 'id') -ne $Plan.ContainerId) { throw 'Unexpected storage container in the backup account.' }
        # List Containers omits metadata. Get the exact container to verify its ownership marker.
        $container = Get-PgAccessResource $Plan.ContainerId '2023-05-01'
        if ((Get-PgAccessValue $container 'id') -ne $Plan.ContainerId) { throw 'The container inspection returned an unexpected resource.' }
        $properties = Get-PgAccessValue $container 'properties'
        if ((Get-PgAccessValue $properties 'publicAccess') -cne 'None' -or
            (Get-PgAccessValue (Get-PgAccessValue $properties 'metadata') 'purpose') -cne 'pulseexchange-shared-postgres-backup') {
            throw 'The backup container is public or its ownership metadata is missing or different.'
        }
        $container
    }
}

function Get-PgAccessSnapshot {
    param($Plan)
    $account = Invoke-PgAccessAzureJson -Arguments @('account', 'show', '--subscription', $Plan.SubscriptionId, '--query', '{id:id,state:state}')
    if ($account.id -ne $Plan.SubscriptionId -or $account.state -ne 'Enabled') { throw 'Wrong or disabled subscription.' }
    $snapshot = [ordered]@{
        EventVnet = Get-PgAccessResource $Plan.EventVnetId '2024-05-01'
        PulseVnet = Get-PgAccessResource $Plan.PulseVnetId '2024-05-01'
        Zone = Get-PgAccessResource $Plan.ZoneId '2024-06-01'
        Links = @(Get-PgAccessList "$($Plan.ZoneId)/virtualNetworkLinks" '2024-06-01')
        Group = $null; Inventory = @(); Storage = $null; BlobService = $null; Containers = @(); Identity = $null; Roles = @()
    }
    $exists = Invoke-PgAccessAzureJson -Arguments @('group', 'exists', '--subscription', $Plan.SubscriptionId, '--name', $Plan.GroupName)
    if ($exists -isnot [bool]) { throw 'Cannot establish whether the dedicated migration group exists.' }
    if ($exists) {
        $snapshot.Group = Invoke-PgAccessAzureJson -Arguments @('group', 'show', '--subscription', $Plan.SubscriptionId, '--name', $Plan.GroupName)
        $snapshot.Inventory = @(Invoke-PgAccessAzureJson -Arguments @('resource', 'list', '--subscription', $Plan.SubscriptionId, '--resource-group', $Plan.GroupName, '--query', '[].{id:id,type:type}'))
        $inventoryIds = @($snapshot.Inventory | ForEach-Object { $_.id })
        if ($Plan.StorageId -in $inventoryIds) {
            $snapshot.Storage = Get-PgAccessResource $Plan.StorageId '2023-05-01'
            $snapshot.BlobService = Get-PgAccessResource $Plan.BlobServiceId '2023-05-01'
            $snapshot.Containers = @(Get-PgAccessContainers $Plan)
        }
        if ($Plan.IdentityId -in $inventoryIds) {
            $snapshot.Identity = Get-PgAccessResource $Plan.IdentityId '2023-01-31'
            $principal = $snapshot.Identity.properties.principalId
            if ($principal -notmatch '^[0-9a-fA-F-]{36}$') { throw 'Cannot establish managed identity principal.' }
            $filter = [Uri]::EscapeDataString("principalId eq '$principal'")
            $roleUrl = "https://management.azure.com/subscriptions/$($Plan.SubscriptionId)/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&" + '$filter=' + $filter
            $roleResult = Invoke-PgAccessAzureJson -Arguments @('rest', '--method', 'get', '--url', $roleUrl)
            if ($null -eq $roleResult -or $null -eq $roleResult.PSObject.Properties['value']) { throw 'Azure returned an incomplete role list.' }
            if (Get-PgAccessValue $roleResult 'nextLink') { throw 'Incomplete identity role inventory.' }
            $snapshot.Roles = @(Get-PgAccessValue $roleResult 'value' @())
        }
    }
    if ($null -eq $snapshot.Storage) {
        $availability = Invoke-PgAccessAzureJson -Arguments @('storage', 'account', 'check-name', '--subscription', $Plan.SubscriptionId, '--name', $Plan.StorageName)
        if ($availability.nameAvailable -ne $true) { throw 'The fixed backup account name is unavailable or belongs to another deployment.' }
    }
    return [pscustomobject]$snapshot
}

function Assert-PgAccessTags {
    param($Resource, $ExpectedTags)
    foreach ($tag in $ExpectedTags.PSObject.Properties) {
        if ((Get-PgAccessValue (Get-PgAccessValue $Resource 'tags') $tag.Name) -cne $tag.Value) {
            throw "Ownership tag $($tag.Name) is missing or different. Existing resources are not adopted."
        }
    }
}

function Assert-PgAccessSnapshot {
    param($Plan, $Snapshot, [switch]$RequireReady)
    $pairs = @(
        @{ Resource = $Snapshot.EventVnet; Id = $Plan.EventVnetId; Cidr = '10.42.0.0/16'; App = 'EventHarbor'; Remote = $Plan.PulseVnetId; Peer = $Plan.EventPeeringId },
        @{ Resource = $Snapshot.PulseVnet; Id = $Plan.PulseVnetId; Cidr = '10.43.0.0/16'; App = 'PulseExchange'; Remote = $Plan.EventVnetId; Peer = $Plan.PulsePeeringId }
    )
    foreach ($pair in $pairs) {
        $vnet = $pair.Resource
        if ($vnet.id -ne $pair.Id -or $vnet.location.Replace(' ', '').ToLowerInvariant() -ne 'eastus2' -or
            @($vnet.properties.addressSpace.addressPrefixes).Count -ne 1 -or $vnet.properties.addressSpace.addressPrefixes[0] -ne $pair.Cidr) {
            throw 'Existing network identity, region, or exact address range does not match the reviewed layout.'
        }
        Assert-PgAccessTags $vnet ([pscustomobject]@{ application = $pair.App; environment = 'prod'; managedBy = 'Bicep' })
        $dhcp = Get-PgAccessValue $vnet.properties 'dhcpOptions'
        if (@(Get-PgAccessValue $dhcp 'dnsServers' @()).Count) { throw 'Custom DNS needs a separately reviewed forwarding design.' }
        $matching = @()
        foreach ($peer in @(Get-PgAccessValue $vnet.properties 'virtualNetworkPeerings' @())) {
            if ($peer.id -ne $pair.Peer -and $peer.properties.remoteVirtualNetwork.id -eq $pair.Remote) { throw 'A different peering already targets this network; refusing a duplicate.' }
            if ($peer.id -ne $pair.Peer) { continue }
            $matching += $peer
            if ($peer.properties.remoteVirtualNetwork.id -ne $pair.Remote -or
                $peer.properties.allowVirtualNetworkAccess -ne $true -or $peer.properties.allowForwardedTraffic -ne $false -or
                $peer.properties.allowGatewayTransit -ne $false -or $peer.properties.useRemoteGateways -ne $false) {
                throw 'Existing named peering differs from the reviewed direct-only configuration.'
            }
            if ($RequireReady -and $peer.properties.peeringState -ne 'Connected') { throw 'The new private peering is not Connected.' }
        }
        if ($RequireReady -and $matching.Count -ne 1) { throw 'The expected private peering was not created.' }
    }
    if ($Snapshot.Zone.id -ne $Plan.ZoneId) { throw 'Wrong existing PostgreSQL private DNS zone.' }
    Assert-PgAccessTags $Snapshot.Zone ([pscustomobject]@{ application = 'EventHarbor'; environment = 'prod'; managedBy = 'Bicep' })
    $matchingLinks = @()
    foreach ($link in $Snapshot.Links) {
        if ($link.id -ne $Plan.LinkId -and $link.properties.virtualNetwork.id -eq $Plan.PulseVnetId) { throw 'The target DNS zone is linked under another name; refusing duplicate configuration.' }
        if ($link.id -ne $Plan.LinkId) { continue }
        $matchingLinks += $link
        Assert-PgAccessTags $link $Plan.Tags
        if ($link.properties.virtualNetwork.id -ne $Plan.PulseVnetId -or $link.properties.registrationEnabled -ne $false) { throw 'Existing named private DNS link differs from the reviewed configuration.' }
        if ($RequireReady -and $link.properties.provisioningState -ne 'Succeeded') { throw 'Private DNS link is not provisioned.' }
    }
    if ($RequireReady -and $matchingLinks.Count -ne 1) { throw 'The expected private DNS link was not created.' }
    if ($null -ne $Snapshot.Group) {
        Assert-PgAccessTags $Snapshot.Group $Plan.Tags
        if ($Snapshot.Group.id -ne $Plan.GroupId -or $Snapshot.Group.location.Replace(' ', '').ToLowerInvariant() -ne 'eastus2') { throw 'Wrong dedicated migration resource group.' }
    }
    $allowed = @($Plan.StorageId, $Plan.BlobServiceId, $Plan.ContainerId, $Plan.IdentityId) + $Plan.DeploymentIds
    foreach ($resource in $Snapshot.Inventory) {
        if ($resource.id -notin $allowed -and -not $resource.id.StartsWith("$($Plan.ContainerId)/providers/Microsoft.Authorization/roleAssignments/", [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The dedicated backup resource group contains an unrelated resource.'
        }
    }
    if ($null -ne $Snapshot.Storage) {
        $storage = $Snapshot.Storage
        Assert-PgAccessTags $storage $Plan.Tags
        $p = $storage.properties
        if ($storage.id -ne $Plan.StorageId -or $storage.location.Replace(' ', '').ToLowerInvariant() -ne 'eastus2' -or
            $storage.kind -ne 'StorageV2' -or $storage.sku.name -ne 'Standard_LRS' -or
            $p.allowBlobPublicAccess -ne $false -or $p.allowSharedKeyAccess -ne $false -or $p.defaultToOAuthAuthentication -ne $true -or
            $p.minimumTlsVersion -ne 'TLS1_2' -or $p.supportsHttpsTrafficOnly -ne $true -or
            $p.publicNetworkAccess -ne 'Enabled' -or $p.networkAcls.defaultAction -ne 'Allow' -or $p.networkAcls.bypass -ne 'None' -or
            $p.isHnsEnabled -ne $false -or $p.allowCrossTenantReplication -ne $false) { throw 'Existing backup storage does not match the private-data, OAuth-only LRS configuration.' }
        $service = $Snapshot.BlobService.properties
        if ($service.isVersioningEnabled -ne $true -or $service.deleteRetentionPolicy.enabled -ne $true -or
            $service.deleteRetentionPolicy.days -ne 7 -or $service.containerDeleteRetentionPolicy.enabled -ne $true -or
            $service.containerDeleteRetentionPolicy.days -ne 7 -or
            (Get-PgAccessValue $service.deleteRetentionPolicy 'allowPermanentDelete' $false) -ne $false) { throw 'Existing backup versioning or recovery retention differs from the reviewed configuration.' }
        foreach ($container in $Snapshot.Containers) {
            if ($container.id -ne $Plan.ContainerId -or $container.properties.publicAccess -ne 'None' -or
                $container.properties.metadata.purpose -cne 'pulseexchange-shared-postgres-backup') { throw 'Unexpected or unowned storage container.' }
        }
    }
    if ($null -ne $Snapshot.Identity) {
        Assert-PgAccessTags $Snapshot.Identity $Plan.Tags
        if ($Snapshot.Identity.id -ne $Plan.IdentityId -or $Snapshot.Identity.location.Replace(' ', '').ToLowerInvariant() -ne 'eastus2') { throw 'Wrong migration managed identity.' }
        foreach ($role in $Snapshot.Roles) {
            if ($role.properties.principalId -ne $Snapshot.Identity.properties.principalId -or $role.properties.scope -ne $Plan.ContainerId -or
                $role.properties.roleDefinitionId -ne $Plan.BlobRoleId) { throw 'Migration identity has an unexpected subscription role assignment or wider resource scope.' }
        }
    }
    if ($RequireReady -and ($null -eq $Snapshot.Group -or $null -eq $Snapshot.Storage -or $null -eq $Snapshot.Identity -or
        $Snapshot.Containers.Count -ne 1 -or $Snapshot.Roles.Count -ne 1)) { throw 'Backup account, container, identity, or container-scoped role is incomplete.' }
}

function Assert-PgAccessWhatIf {
    param($Plan, $Result)
    if ((Get-PgAccessValue $Result 'status') -ne 'Succeeded') { throw 'The access deployment what-if did not succeed.' }
    $allowed = @($Plan.GroupId, $Plan.StorageId, $Plan.BlobServiceId, $Plan.ContainerId, $Plan.IdentityId,
        $Plan.EventPeeringId, $Plan.PulsePeeringId, $Plan.LinkId) + $Plan.DeploymentIds
    foreach ($change in @(Get-PgAccessValue $Result 'changes' @())) {
        # Incremental previews also list resources they explicitly leave alone.
        # Only real changes belong in the mutation allowlist.
        if ($change.changeType -in @('Ignore','NoChange')) { continue }
        $rolePrefix = "$($Plan.ContainerId)/providers/Microsoft.Authorization/roleAssignments/"
        $allowedRole = $change.resourceId.StartsWith($rolePrefix, [StringComparison]::OrdinalIgnoreCase) -and
            $change.resourceId.Substring($rolePrefix.Length) -match '^[0-9a-fA-F-]{36}$'
        if (($change.resourceId -notin $allowed -and -not $allowedRole) -or $change.changeType -notin @('Create','Modify','Deploy')) {
            throw 'What-if would change an unapproved resource or delete a resource. Nothing was deployed.'
        }
    }
}

function Invoke-PgAccessPrepare {
    param([guid]$SubscriptionId, [switch]$CheckOnly, [switch]$Apply, [switch]$ConfirmCosts)
    if ($CheckOnly -and $Apply) { throw 'Choose read-only inspection or Apply, not both.' }
    if ($Apply -and -not $ConfirmCosts) { throw 'Apply requires -ConfirmCosts: private peering traffic and retained backup storage/operations can be billed.' }
    $plan = Get-PgAccessPlan $SubscriptionId
    $snapshot = Get-PgAccessSnapshot $plan
    Assert-PgAccessSnapshot $plan $snapshot
    if ($Apply) {
        $template = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../infra/azure-shared-postgres/access.bicep'))
        $target = @('--subscription', "$SubscriptionId", '--location', $plan.Location, '--name', 'pg-copy-access', '--template-file', $template)
        $preview = Invoke-PgAccessAzureJson -Arguments (@('deployment', 'sub', 'what-if') + $target + @('--no-pretty-print', '--result-format', 'ResourceIdOnly')) -TimeoutSeconds 900
        Assert-PgAccessWhatIf $plan $preview
        $deployment = Invoke-PgAccessAzureJson -Arguments (@('deployment', 'sub', 'create') + $target) -TimeoutSeconds 900
        if ($deployment.properties.provisioningState -ne 'Succeeded') { throw 'Access deployment did not report Succeeded. Inspect pg-copy-access; no database was copied.' }
        $snapshot = Get-PgAccessSnapshot $plan
        Assert-PgAccessSnapshot $plan $snapshot -RequireReady
    }
    return [pscustomobject]@{
        Action = $(if ($Apply) { 'AccessPrepared' } else { 'ReadOnlyInspectionPassed' })
        SubscriptionId = "$SubscriptionId"; Location = $plan.Location
        BackupResourceGroup = $plan.GroupName; BackupStorageAccountId = $plan.StorageId
        BackupContainerId = $plan.ContainerId; BackupBlobHostname = "$($plan.StorageName).blob.core.windows.net"
        BackupContainerUrl = "https://$($plan.StorageName).blob.core.windows.net/pulseexchange-backups"
        JobIdentityResourceId = $plan.IdentityId
        JobIdentityClientId = $(if ($snapshot.Identity) { $snapshot.Identity.properties.clientId } else { $null })
        EventPeeringId = $plan.EventPeeringId; PulsePeeringId = $plan.PulsePeeringId; PrivateDnsLinkId = $plan.LinkId
        DatabaseCopied = $false; BackupUploadVerified = $false; ApplicationConfigurationChanged = $false
        Notes = @('Existing databases, applications, subnets, and security groups are not redeployed.',
            'VNet peering provides network reachability between both VNets; existing network security rules still apply.',
            'Backup blobs require authorized identity access over public HTTPS; this does not create a private endpoint.',
            'Backups and their resource group are retained until an explicitly approved cleanup.',
            'Storage role propagation and an actual upload/download hash check are still required before migration.')
    }
}

Invoke-PgAccessPrepare @PSBoundParameters | ConvertTo-Json -Depth 20
