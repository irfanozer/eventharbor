#requires -Version 7.4
<#
.SYNOPSIS
Check or explicitly deploy the separate economy foundation.
.DESCRIPTION
The default and -CheckOnly are read-only. Deployment requires -Deploy,
-ConfirmCosts and -ConfirmFreeAllowances. The latter confirms that you reviewed
the actual remaining VM, P6-disk and PostgreSQL allowances; it does not mean this
script verified eligibility. The administrator password is never printed or
passed as plaintext on a command line. A restricted temporary parameter file is
removed in finally. The existing Container Apps resource groups are forbidden.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string]$Location,
    [ValidatePattern('^rg-[a-z0-9-]+-economy$')][string]$ResourceGroupName = 'rg-demos-economy',
    [string]$NamePrefix = 'demos',
    [ValidateSet('economy')][string]$EnvironmentName = 'economy',
    [ValidateSet('Standard_B1s', 'Standard_B2ats_v2')][string]$EventHarborVmSize = 'Standard_B1s',
    [ValidateSet('Standard_B1s', 'Standard_B2ats_v2')][string]$PulseExchangeVmSize = 'Standard_B2ats_v2',
    [Nullable[datetime]]$FreeAllowanceExpiry,
    [switch]$AcknowledgeSharedAllowances,
    [switch]$AcknowledgeSharedVmHours,
    [switch]$CheckOnly,
    [switch]$Deploy,
    [switch]$ConfirmCosts,
    [switch]$ConfirmFreeAllowances,
    [string]$SshPublicKeyPath,
    [ValidatePattern('^[a-z_][a-z0-9_-]{0,31}$')][string]$AdminUsername = 'demoops',
    [string]$SshAllowedCidr = '',
    [ValidatePattern('^[a-z][a-z0-9_]{0,62}$')][string]$PostgresAdministratorLogin = 'portfolio_admin',
    [Security.SecureString]$PostgresAdministratorPassword
)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    if ($Deploy -and $CheckOnly) { throw 'Use either -Deploy or -CheckOnly, not both.' }
    if ($Deploy -and (-not $ConfirmCosts -or -not $ConfirmFreeAllowances)) {
        throw 'Deployment requires -Deploy -ConfirmCosts -ConfirmFreeAllowances after reviewing actual account allowances and paid costs.'
    }
    if ($Deploy -and $null -eq $FreeAllowanceExpiry) {
        throw 'Deployment requires -FreeAllowanceExpiry with the verified subscription offer end date.'
    }
    $checkArguments = @{
        SubscriptionId = $SubscriptionId; Location = $Location; ResourceGroupName = $ResourceGroupName
        NamePrefix = $NamePrefix; EnvironmentName = $EnvironmentName
        EventHarborVmSize = $EventHarborVmSize; PulseExchangeVmSize = $PulseExchangeVmSize
        FreeAllowanceExpiry = $FreeAllowanceExpiry; AcknowledgeSharedAllowances = $AcknowledgeSharedAllowances
        AcknowledgeSharedVmHours = $AcknowledgeSharedVmHours
    }
    $report = Test-EconomyFoundation @checkArguments
    Show-EconomyPreflight $report
    if (-not $report.Ready) { throw 'Preflight failed. No Azure resources were created or changed.' }
    if (-not $Deploy) {
        Write-Host 'Read-only checks finished. Re-run with explicit deployment and billing confirmations to deploy.'
        return
    }
    if (-not $SshPublicKeyPath -or -not (Test-Path -LiteralPath $SshPublicKeyPath -PathType Leaf)) {
        throw 'Deployment requires -SshPublicKeyPath pointing to an existing SSH public key.'
    }
    $publicKey = (Get-Content -LiteralPath $SshPublicKeyPath -Raw).Trim()
    if ($publicKey -notmatch '^ssh-(rsa|ed25519) [A-Za-z0-9+/=]+(?: [^\r\n]*)?$') {
        throw 'The SSH key file must contain one OpenSSH RSA or Ed25519 public key, never a private key.'
    }
    if ($SshAllowedCidr) {
        $ip = $null
        $parts = $SshAllowedCidr.Split('/')
        $prefix = 0
        if ($parts.Length -ne 2 -or -not [Net.IPAddress]::TryParse($parts[0], [ref]$ip) -or
            $ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
            -not [int]::TryParse($parts[1], [ref]$prefix) -or $prefix -lt 1 -or $prefix -gt 32) {
            throw 'SshAllowedCidr must be a restricted IPv4 CIDR with prefix 1-32. Leave empty to disable public SSH.'
        }
    }
    if ($PostgresAdministratorLogin -in @('postgres', 'admin', 'administrator', 'root', 'guest', 'public')) {
        throw 'Choose a nonreserved PostgreSQL administrator login.'
    }
    $template = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../infra/azure-economy/foundation.bicep'))
    if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw 'Cannot find the additive economy foundation template.' }
    # A successful local compilation is required before any cloud mutation.
    $null = Invoke-EconomyAzureJson -Arguments @('bicep', 'build', '--file', $template, '--stdout')
    if ($null -eq $PostgresAdministratorPassword) {
        $PostgresAdministratorPassword = Read-Host 'PostgreSQL administrator password' -AsSecureString
    }
    if ($PostgresAdministratorPassword.Length -lt 8 -or $PostgresAdministratorPassword.Length -gt 128) {
        throw 'The administrator password must contain 8-128 characters.'
    }
    $privateDirectory = $null
    $secretPointer = [IntPtr]::Zero
    $plainPassword = $null
    try {
        $privateDirectory = New-EconomyPrivateDirectory
        $parametersPath = Join-Path $privateDirectory 'parameters.json'
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($PostgresAdministratorPassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
        $parameters = @{
            '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
            contentVersion = '1.0.0.0'
            parameters = @{
                location = @{ value = $Location }; namePrefix = @{ value = $NamePrefix }
                environmentName = @{ value = $EnvironmentName }; adminUsername = @{ value = $AdminUsername }
                sshPublicKey = @{ value = $publicKey }; sshAllowedCidr = @{ value = $SshAllowedCidr }
                eventharborVmSize = @{ value = $EventHarborVmSize }; pulseexchangeVmSize = @{ value = $PulseExchangeVmSize }
                postgresAdministratorLogin = @{ value = $PostgresAdministratorLogin }
                postgresAdministratorPassword = @{ value = $plainPassword }
            }
        }
        [IO.File]::WriteAllText($parametersPath, ($parameters | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
        $parameters.parameters.postgresAdministratorPassword.value = $null
        $plainPassword = $null
        if (-not $report.ResourceGroupExists) {
            $null = Invoke-EconomyAzureJson -Arguments @('group', 'create', '--subscription', "$SubscriptionId", '--name', $ResourceGroupName,
                '--location', $Location, '--tags', 'application=EventHarbor-PulseExchange', 'costProfile=economy',
                "environment=$EnvironmentName", 'managedBy=Bicep', 'workload=public-demo')
        }
        $deploymentName = "economy-foundation-$([datetime]::UtcNow.ToString('yyyyMMddHHmmss'))"
        Write-Host "Deploying $deploymentName into $ResourceGroupName. Administrator credentials will not be displayed."
        $deployment = Invoke-EconomyAzureJson -Sensitive -Arguments @(
            'deployment', 'group', 'create', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroupName,
            '--name', $deploymentName, '--mode', 'Incremental', '--template-file', $template,
            '--parameters', "@$parametersPath", '--query', 'properties.outputs'
        )
        # The template contains only explicitly non-secret deployment outputs.
        $deployment | ConvertTo-Json -Depth 8
    } finally {
        $plainPassword = $null
        if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
        if ($privateDirectory -and (Test-Path -LiteralPath $privateDirectory)) {
            # Remove only the exact random directory created above, after checking
            # its resolved parent and prefix; never target a workspace or home.
            $resolved = [IO.Path]::GetFullPath($privateDirectory)
            $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar)
            if ([IO.Path]::GetDirectoryName($resolved) -ne $temporaryRoot -or
                [IO.Path]::GetFileName($resolved) -notmatch '^azure-economy-[0-9a-f]{32}$') {
                throw 'Refusing cleanup outside the exact private deployment directory. Remove the restricted parameter file manually.'
            }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
