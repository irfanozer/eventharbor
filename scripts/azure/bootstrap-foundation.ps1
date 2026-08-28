[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $SubscriptionId,

    [string] $Location = "eastus2",
    [string] $ResourceGroup = "rg-eventharbor-prod",
    [string] $NamePrefix = "eventharbor",
    [string] $EnvironmentName = "prod",
    [string] $CustomDomain = "eventharbor.irfanburakozer.com",
    [string] $PostgresAdministratorLogin = "eventharbor_admin",
    [string] $PostgresDatabaseName = "eventharbor",
    [SecureString] $PostgresAdministratorPassword,
    [string] $GitHubRepository,

    [Parameter(Mandatory)]
    [switch] $ConfirmCosts
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Command {
    param([Parameter(Mandatory)][string] $Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' is not installed or is not on PATH."
    }
}

function New-DatabasePassword {
    $randomBytes = New-Object byte[] 30
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($randomBytes)
    }
    finally {
        $generator.Dispose()
    }
    $randomText = [Convert]::ToBase64String($randomBytes).TrimEnd("=").Replace("+", "A").Replace("/", "b")
    return "Eh9!$randomText"
}

if (-not $ConfirmCosts) {
    throw "Run this script only after reviewing the cost section in docs/deployment.md, and pass -ConfirmCosts."
}

Assert-Command -Name "az"

az account set --subscription $SubscriptionId
$account = az account show --output json | ConvertFrom-Json
if (-not $account.id) {
    throw "Azure CLI is not signed in. Run 'az login' and try again."
}

$providers = @(
    "Microsoft.App",
    "Microsoft.DBforPostgreSQL",
    "Microsoft.Network",
    "Microsoft.OperationalInsights"
)
foreach ($provider in $providers) {
    Write-Host "Registering Azure provider $provider ..."
    az provider register --namespace $provider --wait --only-show-errors | Out-Null
}

Write-Host "Creating resource group $ResourceGroup in $Location ..."
az group create `
    --name $ResourceGroup `
    --location $Location `
    --tags project=eventharbor environment=production managed-by=bicep `
    --only-show-errors `
    --output none

$plainPassword = if ($PostgresAdministratorPassword) {
    ([System.Net.NetworkCredential]::new("", $PostgresAdministratorPassword)).Password
}
else {
    New-DatabasePassword
}

$parameterFile = Join-Path ([System.IO.Path]::GetTempPath()) "eventharbor-$([Guid]::NewGuid().ToString('N')).parameters.json"
try {
    $parameters = @{
        '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        contentVersion = "1.0.0.0"
        parameters = @{
            location = @{ value = $Location }
            namePrefix = @{ value = $NamePrefix }
            environmentName = @{ value = $EnvironmentName }
            postgresAdministratorLogin = @{ value = $PostgresAdministratorLogin }
            postgresAdministratorPassword = @{ value = $plainPassword }
            postgresDatabaseName = @{ value = $PostgresDatabaseName }
        }
    }
    $parameterJson = $parameters | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText(
        $parameterFile,
        $parameterJson,
        [System.Text.UTF8Encoding]::new($false)
    )

    Write-Host "Validating the foundation deployment ..."
    az deployment group validate `
        --resource-group $ResourceGroup `
        --template-file "infra/azure/foundation.bicep" `
        --parameters "@$parameterFile" `
        --only-show-errors `
        --output none

    Write-Host "Creating the Azure foundation. PostgreSQL is the main recurring cost ..."
    $outputs = az deployment group create `
        --resource-group $ResourceGroup `
        --name "foundation-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))" `
        --template-file "infra/azure/foundation.bicep" `
        --parameters "@$parameterFile" `
        --only-show-errors `
        --query properties.outputs `
        --output json | ConvertFrom-Json
}
finally {
    if ([System.IO.File]::Exists($parameterFile)) {
        [System.IO.File]::Delete($parameterFile)
    }
}

$encodedUser = [Uri]::EscapeDataString($PostgresAdministratorLogin)
$encodedPassword = [Uri]::EscapeDataString($plainPassword)
$databaseUrl = "postgresql+asyncpg://${encodedUser}:${encodedPassword}@$($outputs.postgresServerFqdn.value):5432/$($outputs.postgresDatabaseName.value)?ssl=require"

if ($GitHubRepository) {
    Assert-Command -Name "gh"
    gh auth status | Out-Null

    $repositoryParts = $GitHubRepository.Split('/', 2)
    if ($repositoryParts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($repositoryParts[0])) {
        throw "-GitHubRepository must use OWNER/REPOSITORY format."
    }
    Write-Host "Saving the database URL as a masked GitHub repository secret ..."
    $databaseUrl | gh secret set EVENTHARBOR_DATABASE_URL `
        --repo $GitHubRepository

    gh variable set AZURE_RESOURCE_GROUP --repo $GitHubRepository --body $ResourceGroup
    gh variable set AZURE_NAME_PREFIX --repo $GitHubRepository --body $NamePrefix
    gh variable set AZURE_ENVIRONMENT_NAME --repo $GitHubRepository --body $EnvironmentName
    gh variable set AZURE_CONTAINER_APPS_ENVIRONMENT `
        --repo $GitHubRepository `
        --body $outputs.containerAppsEnvironmentName.value
    gh variable set EVENTHARBOR_APPLICATION_ENVIRONMENT `
        --repo $GitHubRepository `
        --body production
    gh variable set EVENTHARBOR_CUSTOM_DOMAIN `
        --repo $GitHubRepository `
        --body $CustomDomain
}
else {
    Write-Warning "The generated database URL was not saved because -GitHubRepository was omitted."
    Write-Warning "Run the script again with -GitHubRepository OWNER/REPOSITORY before deploying."
}

$plainPassword = $null
$databaseUrl = $null

Write-Host ""
Write-Host "Foundation created successfully."
Write-Host "Resource group: $ResourceGroup"
Write-Host "Container Apps environment: $($outputs.containerAppsEnvironmentName.value)"
Write-Host "PostgreSQL server: $($outputs.postgresServerFqdn.value)"
Write-Host "Next: run scripts/azure/configure-github-oidc.ps1, publish the GHCR images, and make both packages public."
