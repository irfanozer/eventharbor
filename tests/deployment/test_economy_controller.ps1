#Requires -Version 7.4
[CmdletBinding()]
param([switch]$CheckInstalledAzureCli)

# Default checks use a temporary local mock executable only. The optional check
# invokes `az version` and JMESPath literals; it never contacts Azure resources.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$eventRepository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$workspace = Split-Path -Parent $eventRepository
$controllers = @(
    @{ Repository = $eventRepository; App = 'eventharbor'; Package = 'irfanozer/eventharbor' }
)
$pulseRepository = Join-Path $workspace 'pulse-exchange'
if (Test-Path -LiteralPath (Join-Path $pulseRepository 'scripts/azure-economy/deploy.ps1')) {
    $controllers += @{ Repository = $pulseRepository; App = 'pulseexchange'; Package = 'irfanozer/pulse-exchange' }
} else {
    Write-Host 'PulseExchange sibling checkout not present; checking this repository only.'
}
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar)
$fixtureDirectory = Join-Path $temporaryRoot ('economy-controller-test-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $fixtureDirectory
$mockFile = Join-Path $fixtureDirectory 'mock cli.ps1'
$fixture = @'
$ErrorActionPreference = 'Stop'
$mode = $args[0]
if ($mode -eq 'failure') {
    [Console]::Error.WriteLine('PRIVATE_DIAGNOSTIC_MARKER')
    exit 19
}
if ($mode -eq 'invalid') {
    [Console]::Out.WriteLine('PRIVATE_DIAGNOSTIC_MARKER')
    exit 0
}
[Console]::Error.WriteLine('Benign mock warning on stderr')
@{ ok = $true; received = @($args | Select-Object -Skip 1) } | ConvertTo-Json -Compress
exit 0
'@
[IO.File]::WriteAllText($mockFile, $fixture, [Text.UTF8Encoding]::new($false))
$checks = 0
function Assert-Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}

try {
    foreach ($controller in $controllers) {
        $path = Join-Path $controller.Repository 'scripts/azure-economy/deploy.ps1'
        $tokens = $null
        $parseErrors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
        Assert-Check ($parseErrors.Count -eq 0) "$path does not parse."
        $controllerText = [IO.File]::ReadAllText($path)
        $stageIndex = $controllerText.IndexOf('with target.open("xb") as handle:')
        $prepareIndex = $controllerText.IndexOf('subprocess.run(["bash", str(stage / "install.sh"), "--prepare-swap-only"], check=True)')
        $pullIndex = $controllerText.IndexOf('["docker", "pull", "--platform", "linux/amd64", image]')
        $labelsIndex = $controllerText.IndexOf('if labels.get("org.opencontainers.image.source") != f"https://github.com/{repository}":')
        $installIndex = $controllerText.IndexOf('subprocess.run(["bash", str(stage / "install.sh")], check=True)')
        Assert-Check ($stageIndex -ge 0 -and $prepareIndex -gt $stageIndex -and $pullIndex -gt $prepareIndex) 'Validated public staging and swap-only preparation must precede the first image pull.'
        Assert-Check ($labelsIndex -gt $pullIndex -and $installIndex -gt $labelsIndex) 'The active runtime must not be installed until both images pass provenance checks.'
        $runtimeInstaller = [IO.File]::ReadAllText((Join-Path $controller.Repository 'infra/azure-economy/runtime/install.sh'))
        $prepareExit = $runtimeInstaller.IndexOf('if [[ "${1:-}" == "--prepare-swap-only" ]]; then')
        $liveInstall = $runtimeInstaller.IndexOf('install -d -o root -g root -m 0750 "$target"')
        Assert-Check ($prepareExit -gt $runtimeInstaller.IndexOf('ECONOMY_SWAP_PY') -and $liveInstall -gt $prepareExit) 'Swap-only mode must exit before live runtime installation.'
        $wrapper = $ast.Find({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Invoke-EconomyAzureJson'
        }, $true)
        Assert-Check ($null -ne $wrapper) 'Azure invocation wrapper was not found.'
        $selection = $ast.Find({
            param($node)
            $node -is [Management.Automation.Language.AssignmentStatementAst] -and
                $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
                $node.Left.VariablePath.UserPath -eq 'azureCommand'
        }, $true)
        Assert-Check ($null -ne $selection) 'Azure command selection was not found.'

        # Windows can expose az.cmd and extensionless az simultaneously. Execute
        # the actual selection expression with two mocked command candidates.
        $selected = & {
            function Get-Command {
                [CmdletBinding()]
                param([string]$Name, [string]$CommandType)
                @(
                    [pscustomobject]@{ Source = 'first-command'; Name = 'az.cmd' },
                    [pscustomobject]@{ Source = 'second-command'; Name = 'az' }
                )
            }
            Invoke-Expression $selection.Extent.Text
            $azureCommand
        }
        Assert-Check (@($selected).Count -eq 1 -and $selected.Source -eq 'first-command') 'Controller must select one Azure CLI application.'

        # Load only the already-parsed helper. Never execute the controller's
        # resource operations while testing argument and diagnostic handling.
        Invoke-Expression $wrapper.Extent.Text
        $azureCommand = [pscustomobject]@{
            Source = Join-Path $PSHOME $(if ($IsWindows) { 'pwsh.exe' } else { 'pwsh' })
        }
        $url = 'https://example.invalid/vm?api-version=2024-07-01&$expand=instanceView'
        $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $mockFile, 'success', $url)
        $result = Invoke-EconomyAzureJson -Arguments $arguments -Operation 'mock successful response' -TimeoutSeconds 20
        Assert-Check ($result['ok'] -eq $true) 'Separate stderr diagnostics broke JSON parsing.'
        Assert-Check ($result['received'][0] -ceq $url) 'Query-string arguments were not preserved verbatim.'
        foreach ($mode in @('failure', 'invalid')) {
            $rejected = $false
            try {
                $null = Invoke-EconomyAzureJson -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $mockFile, $mode) -Operation "mock $mode" -TimeoutSeconds 20
            } catch {
                $rejected = $true
                Assert-Check (-not $_.Exception.Message.Contains('PRIVATE_DIAGNOSTIC_MARKER')) 'The wrapper exposed raw CLI diagnostics.'
            }
            Assert-Check $rejected "The wrapper accepted a $mode response."
        }

        $dryRun = & $path -SubscriptionId '00000000-0000-0000-0000-000000000001' `
            -ResourceGroup 'rg-test-economy' -VmName ('vm-' + $controller.App + '-test') `
            -BackendImage ('ghcr.io/' + $controller.Package + '-backend@sha256:' + ('a' * 64)) `
            -FrontendImage ('ghcr.io/' + $controller.Package + '-frontend@sha256:' + ('b' * 64)) `
            -ExpectedSourceRevision ('c' * 40) -DryRun | ConvertFrom-Json
        Assert-Check ($dryRun.azureCalls -eq 0 -and $dryRun.application -ceq $controller.App) 'Dry run did not remain local.'
        Assert-Check ($dryRun.existingRootOnlyConfiguration -ceq "/etc/$($controller.App)/runtime.env") 'Controller and runtime secret paths differ.'

        if ($CheckInstalledAzureCli) {
            # Exercise the exact controller selection and wrapper against the
            # installed CLI, including the ampersand used by instanceView URLs.
            Invoke-Expression $selection.Extent.Text
            $version = Invoke-EconomyAzureJson -Arguments @('version') -Operation 'local CLI version' -TimeoutSeconds 30
            Assert-Check ([bool]$version['azure-cli']) 'Local Azure CLI version could not be read.'
            $literal = Invoke-EconomyAzureJson -Arguments @('version', '--query', "'A&B'") -Operation 'local CLI ampersand literal' -TimeoutSeconds 30
            Assert-Check ($literal -ceq 'A&B') 'Windows CLI handling split the ampersand argument.'
        }
        Write-Host "PASS $($controller.App) controller checks"
    }
    Write-Host "PASS $checks checks; no Azure resource API calls made."
} finally {
    $resolved = [IO.Path]::GetFullPath($fixtureDirectory)
    if ([IO.Path]::GetDirectoryName($resolved) -ne $temporaryRoot -or
        [IO.Path]::GetFileName($resolved) -notmatch '^economy-controller-test-[0-9a-f]{32}$') {
        throw 'Refusing cleanup outside the exact generated test fixture directory.'
    }
    if (Test-Path -LiteralPath $mockFile -PathType Leaf) { Remove-Item -LiteralPath $mockFile -Force }
    if (Test-Path -LiteralPath $fixtureDirectory -PathType Container) { Remove-Item -LiteralPath $fixtureDirectory }
}
