#requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-EconomyAzureJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$Sensitive
    )
    $operationParts = [System.Collections.Generic.List[string]]::new()
    foreach ($argument in $Arguments) {
        if ($argument -notmatch '^[a-z][a-z-]*$' -or $operationParts.Count -ge 3) { break }
        $operationParts.Add($argument)
    }
    $operation = if ($operationParts.Count) { $operationParts -join ' ' } else { 'native request' }
    $command = Get-Command az -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'Azure CLI is required. Install it and sign in before running this script.'
    }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $command.Source
    if ($IsWindows -and $command.Source.EndsWith('.cmd')) {
        # The official Windows installer places Python beside wbin. Starting
        # that interpreter directly preserves argument boundaries without cmd
        # expansion of file paths or query expressions.
        $cliPython = [IO.Path]::GetFullPath((Join-Path (Split-Path $command.Source) '../python.exe'))
        if (-not (Test-Path -LiteralPath $cliPython -PathType Leaf)) {
            throw 'Unsupported Windows Azure CLI launcher. Use the official Azure CLI installer with its bundled Python runtime.'
        }
        $start.FileName = $cliPython
        $start.ArgumentList.Add('-IBm')
        $start.ArgumentList.Add('azure.cli')
    }
    foreach ($argument in $Arguments + @('--only-show-errors', '--output', 'json')) {
        $start.ArgumentList.Add($argument)
    }
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $content = $stdout.GetAwaiter().GetResult()
        $diagnostics = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            if ($Sensitive) {
                throw "Azure CLI [$operation] failed with exit code $($process.ExitCode). Sensitive request diagnostics were suppressed. Inspect deployment status separately."
            }
            throw "Azure CLI [$operation] failed with exit code $($process.ExitCode): $diagnostics"
        }
        if ([string]::IsNullOrWhiteSpace($content)) { return $null }
        try { return ConvertFrom-Json -InputObject $content -Depth 100 }
        catch { throw "Azure CLI [$operation] returned invalid JSON; no deployment can safely continue." }
    } finally { $process.Dispose() }
}

function Get-EconomyValue {
    param($Object, [Parameter(Mandatory)][string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Test-EconomyFoundation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][guid]$SubscriptionId,
        [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string]$Location,
        [ValidatePattern('^rg-[a-z0-9-]+-economy$')][string]$ResourceGroupName = 'rg-demos-economy',
        [ValidatePattern('^[a-z][a-z0-9-]{2,14}$')][string]$NamePrefix = 'demos',
        [ValidateSet('economy')][string]$EnvironmentName = 'economy',
        [ValidateSet('Standard_B1s', 'Standard_B2ats_v2')][string]$EventHarborVmSize = 'Standard_B1s',
        [ValidateSet('Standard_B1s', 'Standard_B2ats_v2')][string]$PulseExchangeVmSize = 'Standard_B2ats_v2',
        [Nullable[datetime]]$FreeAllowanceExpiry,
        [switch]$AcknowledgeSharedAllowances,
        [switch]$AcknowledgeSharedVmHours
    )
    $blockers = [System.Collections.Generic.List[string]]::new()
    $warnings = [System.Collections.Generic.List[string]]::new()
    if ($ResourceGroupName -in @('rg-eventharbor-prod', 'rg-pulseexchange-prod')) {
        throw 'The economy foundation must use a separate resource group. Existing production resource groups are forbidden.'
    }
    if ($EventHarborVmSize -eq $PulseExchangeVmSize) {
        if ($EventHarborVmSize -eq 'Standard_B2ats_v2' -and $AcknowledgeSharedVmHours) {
            $warnings.Add('Both VMs explicitly use B2ats_v2 and share ONE 750-hour monthly allowance if eligible. At 730 hours per VM, 710 of 1460 combined hours are paid before any other usage. Verify the remaining meter and compute price; this is not a zero-cost compute configuration.')
        } else {
            $blockers.Add('Repeated VM SKUs are blocked. Only two explicitly selected Standard_B2ats_v2 VMs may share the 750-hour allowance with -AcknowledgeSharedVmHours; excess hours are paid. No automatic fallback is allowed.')
        }
    }
    if ($null -ne $FreeAllowanceExpiry -and $FreeAllowanceExpiry.Date -le [datetime]::UtcNow.Date) {
        $blockers.Add('The supplied free-allowance expiry is today or in the past. Reassess paid costs before deployment.')
    }

    $account = Invoke-EconomyAzureJson -Arguments @('account', 'show', '--subscription', "$SubscriptionId")
    if ($account.state -ne 'Enabled') { $blockers.Add("Subscription state is $($account.state), not Enabled.") }
    if ($account.id -ne "$SubscriptionId") { throw 'Azure returned a different subscription from the explicitly requested subscription.' }
    $groupExists = Invoke-EconomyAzureJson -Arguments @('group', 'exists', '--subscription', "$SubscriptionId", '--name', $ResourceGroupName)
    if ($groupExists) {
        $group = Invoke-EconomyAzureJson -Arguments @('group', 'show', '--subscription', "$SubscriptionId", '--name', $ResourceGroupName)
        $groupTags = Get-EconomyValue $group 'tags'
        if ((Get-EconomyValue $groupTags 'costProfile') -ne 'economy' -or
            (Get-EconomyValue $groupTags 'application') -ne 'EventHarbor-PulseExchange' -or
            (Get-EconomyValue $groupTags 'environment') -ne $EnvironmentName) {
            $blockers.Add('The existing resource group lacks matching application, environment and costProfile=economy ownership tags. Choose a new dedicated resource group.')
        }
        if ($group.location -ne $Location) { $blockers.Add("The existing resource group is in $($group.location), not $Location.") }
    }

    # This query reports subscription restrictions, not just catalog existence.
    $skuQuery = "[?name=='$EventHarborVmSize' || name=='$PulseExchangeVmSize']"
    $skus = @(Invoke-EconomyAzureJson -Arguments @('vm', 'list-skus', '--subscription', "$SubscriptionId", '--location', $Location,
        '--resource-type', 'virtualMachines', '--all', '--query', $skuQuery))
    $usage = @(Invoke-EconomyAzureJson -Arguments @('vm', 'list-usage', '--subscription', "$SubscriptionId", '--location', $Location))
    $vms = @(Invoke-EconomyAzureJson -Arguments @('vm', 'list', '--subscription', "$SubscriptionId"))
    $wanted = @(
        @{ Application = 'eventharbor'; Name = "vm-eh-$NamePrefix-$EnvironmentName"; Size = $EventHarborVmSize },
        @{ Application = 'pulseexchange'; Name = "vm-px-$NamePrefix-$EnvironmentName"; Size = $PulseExchangeVmSize }
    )
    $familyCores = @{}
    $totalNeeded = 0
    $skuResults = [System.Collections.Generic.List[object]]::new()
    $targetIds = @($wanted | ForEach-Object {
        "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Compute/virtualMachines/$($_.Name)"
    })

    foreach ($vm in $wanted) {
        $sku = @($skus | Where-Object { $_.name -eq $vm.Size } | Select-Object -First 1)
        if ($sku.Count -ne 1) {
            $blockers.Add("$($vm.Size) is absent from the $Location catalog for this subscription.")
            continue
        }
        $sku = $sku[0]
        $caps = @{}
        foreach ($capability in $sku.capabilities) { $caps[$capability.name] = $capability.value }
        $restrictions = @($sku.restrictions | Where-Object {
            $_.type -eq 'Location' -and
            (@($_.values).Count -eq 0 -or @($_.values) -contains $Location)
        })
        foreach ($restriction in $restrictions) {
            $blockers.Add("$($vm.Size) is restricted in ${Location}: $($restriction.reasonCode). No automatic SKU or region fallback is allowed.")
        }
        if ($caps['CpuArchitectureType'] -ne 'x64') { $blockers.Add("$($vm.Size) does not explicitly report x64 architecture.") }
        if ($caps['PremiumIO'] -ne 'True') { $blockers.Add("$($vm.Size) does not report Premium disk support.") }
        if ($caps['HyperVGenerations'] -notmatch '(^|,)V2(,|$)' -or $caps['TrustedLaunchDisabled'] -eq 'True') {
            $blockers.Add("$($vm.Size) does not support the configured Gen2 Trusted Launch VM.")
        }
        $cores = 0
        if (-not [int]::TryParse([string]$caps['vCPUs'], [ref]$cores) -or $cores -le 0) {
            $blockers.Add("Cannot establish vCPU demand for $($vm.Size).")
        }
        $expectedFamily = if ($vm.Size -eq 'Standard_B1s') { 'standardBSFamily' } else { 'standardBasv2Family' }
        if ($sku.family -ne $expectedFamily) { $blockers.Add("Unexpected quota family for $($vm.Size): $($sku.family).") }
        $targetId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Compute/virtualMachines/$($vm.Name)"
        $existing = @($vms | Where-Object { $_.id -eq $targetId })
        $needed = $cores
        if ($existing.Count -gt 0) {
            $vmTags = Get-EconomyValue $existing[0] 'tags'
            if ((Get-EconomyValue $vmTags 'application') -ne $vm.Application -or
                (Get-EconomyValue $vmTags 'costProfile') -ne 'economy' -or
                (Get-EconomyValue $vmTags 'environment') -ne $EnvironmentName) {
                $blockers.Add("Existing VM $($vm.Name) does not have matching economy ownership tags.")
            }
            if ($existing[0].hardwareProfile.vmSize -ne $vm.Size) {
                $blockers.Add("Existing VM $($vm.Name) has a different size. This bootstrap does not silently resize existing VMs.")
            } else { $needed = 0 }
        }
        $familyCores[$expectedFamily] = [int]$familyCores[$expectedFamily] + $needed
        $totalNeeded += $needed
        $skuResults.Add([pscustomobject]@{
            Application = $vm.Application; VmName = $vm.Name; Size = $vm.Size
            Family = $expectedFamily; Vcpus = $cores; NewCoresRequired = $needed
            RegionRestricted = ($restrictions.Count -gt 0)
        })
    }
    $familyCores['cores'] = $totalNeeded
    $quotaResults = [System.Collections.Generic.List[object]]::new()
    foreach ($family in $familyCores.Keys) {
        $quota = @($usage | Where-Object { $_.name.value -eq $family } | Select-Object -First 1)
        if ($quota.Count -eq 0) { $blockers.Add("Cannot establish $family quota in $Location."); continue }
        $available = [int]$quota[0].limit - [int]$quota[0].currentValue
        if ($available -lt $familyCores[$family]) {
            $blockers.Add("Insufficient $family quota in ${Location}: $available cores available; $($familyCores[$family]) new cores required (limit $($quota[0].limit), used $($quota[0].currentValue)).")
        }
        $quotaResults.Add([pscustomobject]@{
            Family = $family; Used = $quota[0].currentValue; Limit = $quota[0].limit
            Available = $available; Required = $familyCores[$family]
        })
    }
    $competingVms = @($vms | Where-Object {
        $_.hardwareProfile.vmSize -in @($EventHarborVmSize, $PulseExchangeVmSize) -and $_.id -notin $targetIds
    } | Select-Object name, resourceGroup, location, @{ Name = 'Size'; Expression = { $_.hardwareProfile.vmSize } })
    if ($competingVms.Count -gt 0) {
        $warnings.Add("$($competingVms.Count) other VM(s) use the selected free SKU meters in this subscription. Remaining monthly hours cannot be inferred from the current inventory.")
        if (-not $AcknowledgeSharedAllowances) {
            $blockers.Add('Other VMs may consume the same monthly allowances. Review them and pass -AcknowledgeSharedAllowances only after checking remaining hours.')
        }
    }
    # Azure CLI 2.89 requires a resource group for disk list. Enumerate resource
    # IDs subscription-wide, then read complete disk properties by explicit ID.
    $diskIds = @(Invoke-EconomyAzureJson -Arguments @('resource', 'list', '--subscription', "$SubscriptionId",
        '--resource-type', 'Microsoft.Compute/disks', '--query', '[].id'))
    $disks = @(foreach ($diskId in $diskIds) {
        Invoke-EconomyAzureJson -Arguments @('disk', 'show', '--subscription', "$SubscriptionId", '--ids', $diskId)
    })
    $competingDisks = @($disks | Where-Object {
        $_.sku.name -eq 'Premium_LRS' -and $_.diskSizeGb -eq 64 -and
        (Get-EconomyValue $_ 'managedBy') -notin $targetIds
    } | Select-Object name, resourceGroup, diskSizeGb)
    if ($competingDisks.Count -gt 0) {
        $warnings.Add("$($competingDisks.Count) other P6-sized Premium disks may consume the two-disk allowance, including unattached disks.")
        if (-not $AcknowledgeSharedAllowances) {
            $blockers.Add('Other P6 disks may consume the disk allowance. Review them and acknowledge shared allowances before deployment.')
        }
    }
    $postgresServers = @(Invoke-EconomyAzureJson -Arguments @('postgres', 'flexible-server', 'list', '--subscription', "$SubscriptionId"))
    $competingPostgres = @($postgresServers | Where-Object {
        $_.sku.name -eq 'Standard_B1ms' -and $_.resourceGroup -ne $ResourceGroupName
    } | Select-Object name, resourceGroup, location, @{ Name = 'Size'; Expression = { $_.sku.name } })
    if ($competingPostgres.Count -gt 0) {
        $warnings.Add("$($competingPostgres.Count) other B1ms PostgreSQL server(s) may consume the same PostgreSQL compute, storage and backup allowances. Eligibility and remaining usage must be checked separately.")
        if (-not $AcknowledgeSharedAllowances) {
            $blockers.Add('Other B1ms PostgreSQL servers may consume the database allowance. Review them and acknowledge shared allowances before deployment.')
        }
    }

    $postgresCapabilities = @(Invoke-EconomyAzureJson -Arguments @('postgres', 'flexible-server', 'list-skus', '--subscription', "$SubscriptionId", '--location', $Location))
    $pgSupported = $false
    foreach ($capability in $postgresCapabilities) {
        $versions = @(Get-EconomyValue $capability 'supportedServerVersions' @())
        $editions = @(Get-EconomyValue $capability 'supportedServerEditions' @())
        foreach ($edition in @($editions | Where-Object { $_.name -eq 'Burstable' })) {
            $pgSkus = @(Get-EconomyValue $edition 'supportedServerSkus' @())
            $storage = @(Get-EconomyValue $edition 'supportedStorageEditions' @())
            $sizes = @($storage | Where-Object { $_.name -eq 'ManagedDisk' } | ForEach-Object { $_.supportedStorageMb })
            if (@($versions | Where-Object { $_.name -eq '17' }).Count -gt 0 -and
                @($pgSkus | Where-Object { $_.name -eq 'Standard_B1ms' }).Count -gt 0 -and
                @($sizes | Where-Object { $_.storageSizeMb -eq 32768 }).Count -gt 0 -and
                (Get-EconomyValue $capability 'restricted') -notin @($true, 'Enabled', 'True')) {
                $pgSupported = $true
            }
        }
    }
    if (-not $pgSupported) { $blockers.Add('The regional PostgreSQL capability response did not confirm PostgreSQL 17, Standard_B1ms and 32 GiB storage. No deployment will be attempted.') }
    $warnings.Add('This read-only check cannot verify free-service billing meters or remaining allowances. Confirm both VM meters, two P6 disks, and PostgreSQL separately in Cost Management.')
    $warnings.Add('Two Standard IPv4 addresses are paid resources. Budget about $7.30 per 730-hour month at $0.005/hour each, plus any uncovered database, compute, disk, backup or network usage. Verify current prices.')
    $warnings.Add('SKU listings and quota checks do not reserve capacity. A deployment can still fail because regional capacity changes.')
    return [pscustomobject]@{
        SubscriptionId = "$SubscriptionId"; SubscriptionName = $account.name
        Location = $Location; ResourceGroupName = $ResourceGroupName; ResourceGroupExists = [bool]$groupExists
        FreeAllowanceExpiry = $FreeAllowanceExpiry
        SharedVmHoursAcknowledged = [bool]$AcknowledgeSharedVmHours
        SkuChecks = @($skuResults); QuotaChecks = @($quotaResults)
        CompetingVms = $competingVms; CompetingP6Disks = $competingDisks; CompetingPostgresServers = $competingPostgres
        PostgreSqlCapabilitiesConfirmed = $pgSupported
        Warnings = @($warnings); Blockers = @($blockers); Ready = ($blockers.Count -eq 0)
    }
}

function Show-EconomyPreflight {
    param([Parameter(Mandatory)]$Report)
    Write-Host "Subscription: $($Report.SubscriptionName) ($($Report.SubscriptionId))"
    Write-Host "Region: $($Report.Location); dedicated resource group: $($Report.ResourceGroupName)"
    $Report.SkuChecks | Format-Table Application, Size, Family, NewCoresRequired, RegionRestricted | Out-Host
    $Report.QuotaChecks | Format-Table Family, Used, Limit, Available, Required | Out-Host
    if ($Report.CompetingVms.Count -gt 0) { $Report.CompetingVms | Format-Table | Out-Host }
    if ($Report.CompetingP6Disks.Count -gt 0) { $Report.CompetingP6Disks | Format-Table | Out-Host }
    if ($Report.CompetingPostgresServers.Count -gt 0) { $Report.CompetingPostgresServers | Format-Table | Out-Host }
    foreach ($message in $Report.Warnings) { Write-Warning $message }
    foreach ($message in $Report.Blockers) { Write-Host "BLOCKED: $message" -ForegroundColor Red }
    Write-Host "Ready for explicitly confirmed deployment: $($Report.Ready)"
}

function New-EconomyPrivateDirectory {
    # Protect the directory before any secret file is created inside it.
    $path = Join-Path ([IO.Path]::GetTempPath()) ('azure-economy-' + [guid]::NewGuid().ToString('N'))
    if ($IsWindows) {
        $null = New-Item -ItemType Directory -Path $path
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($identity)
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', $inheritance, 'None', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $path -AclObject $acl
    } else {
        & mkdir -m 700 -- $path
        if ($LASTEXITCODE -ne 0) { throw 'Could not create a private deployment directory.' }
    }
    return $path
}
