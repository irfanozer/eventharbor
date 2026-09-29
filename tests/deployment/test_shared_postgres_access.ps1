#requires -Version 7.4
# All Azure operations are replaced by local fixtures. No login is required.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../scripts/azure-shared-postgres/prepare-access.ps1'))
$tokens = $null
$problems = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$problems)
if ($problems.Count) { throw 'The access preparation script did not parse.' }
foreach ($function in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Set-Item -LiteralPath "Function:$($function.Name)" -Value $function.Body.GetScriptBlock()
}
$subscription = [guid]'83099284-9ad4-4140-b8fe-8388b6d98a98'
$plan = Get-PgAccessPlan $subscription
$checks = 0

function Assert-Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}

function New-AccessFixture([switch]$Existing) {
    $fixture = [ordered]@{
        EventVnet = @{ id = $plan.EventVnetId; location = 'eastus2'; tags = @{ application = 'EventHarbor'; environment = 'prod'; managedBy = 'Bicep' }; properties = @{ addressSpace = @{ addressPrefixes = @('10.42.0.0/16') }; dhcpOptions = @{ dnsServers = @() }; virtualNetworkPeerings = @() } }
        PulseVnet = @{ id = $plan.PulseVnetId; location = 'eastus2'; tags = @{ application = 'PulseExchange'; environment = 'prod'; managedBy = 'Bicep' }; properties = @{ addressSpace = @{ addressPrefixes = @('10.43.0.0/16') }; dhcpOptions = @{ dnsServers = @() }; virtualNetworkPeerings = @() } }
        Zone = @{ id = $plan.ZoneId; tags = @{ application = 'EventHarbor'; environment = 'prod'; managedBy = 'Bicep' } }
        Links = @(); Group = $null; Inventory = @(); Storage = $null; BlobService = $null; Containers = @(); Identity = $null; Roles = @()
    }
    if ($Existing) {
        $fixture.EventVnet.properties.virtualNetworkPeerings = @(@{ id = $plan.EventPeeringId; properties = @{ remoteVirtualNetwork = @{ id = $plan.PulseVnetId }; allowVirtualNetworkAccess = $true; allowForwardedTraffic = $false; allowGatewayTransit = $false; useRemoteGateways = $false; peeringState = 'Connected' } })
        $fixture.PulseVnet.properties.virtualNetworkPeerings = @(@{ id = $plan.PulsePeeringId; properties = @{ remoteVirtualNetwork = @{ id = $plan.EventVnetId }; allowVirtualNetworkAccess = $true; allowForwardedTraffic = $false; allowGatewayTransit = $false; useRemoteGateways = $false; peeringState = 'Connected' } })
        $fixture.Links = @(@{ id = $plan.LinkId; tags = $plan.Tags; properties = @{ virtualNetwork = @{ id = $plan.PulseVnetId }; registrationEnabled = $false; provisioningState = 'Succeeded' } })
        $fixture.Group = @{ id = $plan.GroupId; tags = $plan.Tags; location = 'eastus2' }
        $fixture.Inventory = @(@{ id = $plan.StorageId }, @{ id = $plan.IdentityId })
        $fixture.Storage = @{ id = $plan.StorageId; tags = $plan.Tags; location = 'eastus2'; kind = 'StorageV2'; sku = @{ name = 'Standard_LRS' }; properties = @{
            allowBlobPublicAccess = $false; allowSharedKeyAccess = $false; defaultToOAuthAuthentication = $true
            minimumTlsVersion = 'TLS1_2'; supportsHttpsTrafficOnly = $true; publicNetworkAccess = 'Enabled'
            networkAcls = @{ defaultAction = 'Allow'; bypass = 'None' }; isHnsEnabled = $false; allowCrossTenantReplication = $false
        } }
        $fixture.BlobService = @{ properties = @{ isVersioningEnabled = $true; deleteRetentionPolicy = @{ enabled = $true; days = 7 }; containerDeleteRetentionPolicy = @{ enabled = $true; days = 7 } } }
        $fixture.Containers = @(@{ id = $plan.ContainerId; properties = @{ publicAccess = 'None'; metadata = @{ purpose = 'pulseexchange-shared-postgres-backup' } } })
        $fixture.Identity = @{ id = $plan.IdentityId; location = 'eastus2'; tags = $plan.Tags; properties = @{ principalId = '00000000-0000-4000-8000-000000000001'; clientId = '00000000-0000-4000-8000-000000000002' } }
        $fixture.Roles = @(@{ properties = @{ principalId = $fixture.Identity.properties.principalId; scope = $plan.ContainerId; roleDefinitionId = $plan.BlobRoleId } })
    }
    return $fixture | ConvertTo-Json -Depth 30 | ConvertFrom-Json -Depth 30
}

Assert-PgAccessSnapshot $plan (New-AccessFixture)
Assert-PgAccessSnapshot $plan (New-AccessFixture -Existing) -RequireReady
Assert-Check $true 'Fresh and idempotent configurations should pass.'

$unsafeCases = [ordered]@{
    'wrong CIDR' = { param($s) $s.PulseVnet.properties.addressSpace.addressPrefixes = @('10.42.0.0/16') }
    'extra address range' = { param($s) $s.EventVnet.properties.addressSpace.addressPrefixes += '192.168.0.0/16' }
    'wrong region' = { param($s) $s.EventVnet.location = 'westus2' }
    'foreign network' = { param($s) $s.EventVnet.tags.application = 'Other' }
    'custom DNS' = { param($s) $s.PulseVnet.properties.dhcpOptions.dnsServers = @('10.43.1.2') }
    'forwarded traffic' = { param($s) $s.EventVnet.properties.virtualNetworkPeerings[0].properties.allowForwardedTraffic = $true }
    'gateway transit' = { param($s) $s.EventVnet.properties.virtualNetworkPeerings[0].properties.allowGatewayTransit = $true }
    'remote gateway' = { param($s) $s.PulseVnet.properties.virtualNetworkPeerings[0].properties.useRemoteGateways = $true }
    'different peering name' = { param($s) $s.EventVnet.properties.virtualNetworkPeerings[0].id += '-other' }
    'wrong peering destination' = { param($s) $s.EventVnet.properties.virtualNetworkPeerings[0].properties.remoteVirtualNetwork.id = '/other' }
    'DNS registration' = { param($s) $s.Links[0].properties.registrationEnabled = $true }
    'foreign DNS link' = { param($s) $s.Links[0].tags.purpose = 'Other' }
    'foreign backup group' = { param($s) $s.Group.tags.purpose = 'Other' }
    'unrelated backup resource' = { param($s) $s.Inventory += [pscustomobject]@{ id = "$($plan.GroupId)/providers/Microsoft.Compute/virtualMachines/other" } }
    'public blobs' = { param($s) $s.Storage.properties.allowBlobPublicAccess = $true }
    'shared keys' = { param($s) $s.Storage.properties.allowSharedKeyAccess = $true }
    'old TLS' = { param($s) $s.Storage.properties.minimumTlsVersion = 'TLS1_0' }
    'different storage SKU' = { param($s) $s.Storage.sku.name = 'Standard_GRS' }
    'versioning disabled' = { param($s) $s.BlobService.properties.isVersioningEnabled = $false }
    'public container' = { param($s) $s.Containers[0].properties.publicAccess = 'Blob' }
    'foreign container' = { param($s) $s.Containers[0].properties.metadata.purpose = 'Other' }
    'broader role scope' = { param($s) $s.Roles[0].properties.scope = $plan.StorageId }
    'broader role permissions' = { param($s) $s.Roles[0].properties.roleDefinitionId = 'Contributor' }
}
foreach ($case in $unsafeCases.GetEnumerator()) {
    $fixture = New-AccessFixture -Existing
    & $case.Value $fixture
    $rejected = $false
    try { Assert-PgAccessSnapshot $plan $fixture } catch { $rejected = $true }
    Assert-Check $rejected "Unsafe fixture passed: $($case.Key)"
    Write-Output "Passed: $($case.Key)"
}

$failed = $false
try { $null = Get-PgAccessPlan ([guid]'00000000-0000-4000-8000-000000000001') } catch { $failed = $true }
Assert-Check $failed 'An unreviewed subscription must be rejected.'

$originalListFunction = (Get-Item Function:Get-PgAccessList).ScriptBlock
$originalResourceFunction = (Get-Item Function:Get-PgAccessResource).ScriptBlock
$script:listedContainers = @([pscustomobject]@{ id = $plan.ContainerId; properties = [pscustomobject]@{ publicAccess = 'None' } })
$script:containerDetails = (New-AccessFixture -Existing).Containers[0]
$script:containerGets = 0
function Get-PgAccessList {
    param([string]$Id, [string]$ApiVersion)
    if ($Id -ne "$($plan.BlobServiceId)/containers" -or $ApiVersion -ne '2023-05-01') { throw 'Unexpected container-list request.' }
    return $script:listedContainers
}
function Get-PgAccessResource {
    param([string]$Id, [string]$ApiVersion)
    if ($Id -ne $plan.ContainerId -or $ApiVersion -ne '2023-05-01') { throw 'Unexpected exact-container request.' }
    $script:containerGets++
    return $script:containerDetails
}
$containers = @(Get-PgAccessContainers $plan)
Assert-Check ($containers.Count -eq 1 -and $script:containerGets -eq 1 -and $containers[0].properties.metadata.purpose -ceq 'pulseexchange-shared-postgres-backup') 'A metadata-free list must be followed by exact-container inspection.'
$script:containerDetails.properties.PSObject.Properties.Remove('metadata')
$failed = $false
try { $null = Get-PgAccessContainers $plan } catch { $failed = $true }
Assert-Check $failed 'Missing metadata in the exact-container response must still fail ownership validation.'
$script:containerDetails = (New-AccessFixture -Existing).Containers[0]
$script:containerDetails.properties.metadata.purpose = 'Other'
$failed = $false
try { $null = Get-PgAccessContainers $plan } catch { $failed = $true }
Assert-Check $failed 'Foreign exact-container metadata must not be accepted.'
$script:listedContainers[0].id = "$($plan.ContainerId)-foreign"
$script:containerGets = 0
$failed = $false
try { $null = Get-PgAccessContainers $plan } catch { $failed = $true }
Assert-Check ($failed -and $script:containerGets -eq 0) 'A foreign container name must fail before any detail request.'
$script:listedContainers = @()
Assert-Check (@(Get-PgAccessContainers $plan).Count -eq 0) 'A new account can have no container before deployment.'
Set-Item Function:Get-PgAccessList $originalListFunction
Set-Item Function:Get-PgAccessResource $originalResourceFunction

$script:operations = [System.Collections.Generic.List[string]]::new()
$script:deployed = $false
$script:unsafeWhatIf = $false
function Get-PgAccessSnapshot {
    param($Plan)
    $script:operations.Add('read snapshot')
    return New-AccessFixture -Existing:$script:deployed
}
function Invoke-PgAccessAzureJson {
    param([string[]]$Arguments, [int]$TimeoutSeconds)
    $operation = ($Arguments | Select-Object -First 3) -join ' '
    $script:operations.Add($operation)
    if ($Arguments -notcontains 'pg-copy-access' -or $Arguments -notcontains "$subscription") { throw 'Deployment targets changed.' }
    switch ($operation) {
        'deployment sub what-if' {
            return [pscustomobject]@{ status = 'Succeeded'; changes = @([pscustomobject]@{ changeType = 'Create'; resourceId = $(if ($script:unsafeWhatIf) { $plan.EventVnetId } else { $plan.ContainerId }) }) }
        }
        'deployment sub create' {
            $script:deployed = $true
            return [pscustomobject]@{ properties = [pscustomobject]@{ provisioningState = 'Succeeded' } }
        }
        default { throw 'Unexpected operation in the offline access test.' }
    }
}

$result = Invoke-PgAccessPrepare -SubscriptionId $subscription
Assert-Check ($result.Action -eq 'ReadOnlyInspectionPassed' -and $operations.Count -eq 1) 'Default mode must only inspect.'
$operations.Clear()
$failed = $false
try { $null = Invoke-PgAccessPrepare -SubscriptionId $subscription -Apply } catch { $failed = $true }
Assert-Check ($failed -and $operations.Count -eq 0) 'Cost confirmation must precede any call.'
$failed = $false
try { $null = Invoke-PgAccessPrepare -SubscriptionId $subscription -Apply -CheckOnly -ConfirmCosts } catch { $failed = $true }
Assert-Check ($failed -and $operations.Count -eq 0) 'Conflicting inspection and apply switches must fail.'
$script:unsafeWhatIf = $true
$failed = $false
try { $null = Invoke-PgAccessPrepare -SubscriptionId $subscription -Apply -ConfirmCosts } catch { $failed = $true }
Assert-Check ($failed -and $operations -notcontains 'deployment sub create') 'What-if cannot change an existing VNet.'
$script:unsafeWhatIf = $false
$operations.Clear()
$result = Invoke-PgAccessPrepare -SubscriptionId $subscription -Apply -ConfirmCosts
Assert-Check ($result.Action -eq 'AccessPrepared') 'Safe apply mock did not complete.'
Assert-Check (($operations -join '|') -eq 'read snapshot|deployment sub what-if|deployment sub create|read snapshot') 'Apply must inspect, preview, deploy and verify in that order.'
Assert-Check (-not $result.DatabaseCopied -and -not $result.BackupUploadVerified -and -not $result.ApplicationConfigurationChanged) 'Access provisioning must not claim migration or backup success.'
$failed = $false
try { Assert-PgAccessWhatIf $plan ([pscustomobject]@{ status = 'Succeeded'; changes = @([pscustomobject]@{ resourceId = $plan.ContainerId; changeType = 'Delete' }) }) } catch { $failed = $true }
Assert-Check $failed 'Deletion is forbidden even for an owned resource.'

foreach ($unchanged in @('Ignore','NoChange')) {
    Assert-PgAccessWhatIf $plan ([pscustomobject]@{ status = 'Succeeded'; changes = @([pscustomobject]@{ resourceId = $plan.EventVnetId; changeType = $unchanged }) })
    Assert-Check $true 'Incremental preview can leave existing networks unchanged.'
}
$failed = $false
try { Assert-PgAccessWhatIf $plan ([pscustomobject]@{ status = 'Succeeded'; changes = @([pscustomobject]@{ resourceId = $plan.ContainerId; changeType = 'Unsupported' }) }) } catch { $failed = $true }
Assert-Check $failed 'Indeterminate resource changes must be reviewed rather than deployed.'

Write-Output "SHARED_POSTGRES_ACCESS_OFFLINE_CHECKS_PASS: $checks checks; no Azure requests made."
