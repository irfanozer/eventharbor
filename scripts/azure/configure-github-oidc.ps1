[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $SubscriptionId,

    [Parameter(Mandatory)]
    [string] $GitHubOwner,

    [Parameter(Mandatory)]
    [string] $GitHubRepository,

    [string] $ResourceGroup = "rg-eventharbor-prod",
    [string] $GitHubEnvironment = "production",
    [string] $ApplicationName = "eventharbor-github-production",
    [switch] $ConfigureGitHub
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Command {
    param([Parameter(Mandatory)][string] $Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' is not installed or is not on PATH."
    }
}

Assert-Command -Name "az"
az account set --subscription $SubscriptionId
$account = az account show --output json | ConvertFrom-Json
$tenantId = [string] $account.tenantId
if (-not $tenantId) {
    throw "Azure CLI is not signed in. Run 'az login' and try again."
}

$resourceGroupId = az group show `
    --name $ResourceGroup `
    --query id `
    --output tsv `
    --only-show-errors
if (-not $resourceGroupId) {
    throw "Resource group '$ResourceGroup' does not exist. Run bootstrap-foundation.ps1 first."
}

$clientId = az ad app list `
    --display-name $ApplicationName `
    --query "[0].appId" `
    --output tsv `
    --only-show-errors
if (-not $clientId) {
    Write-Host "Creating Microsoft Entra application $ApplicationName ..."
    $clientId = az ad app create `
        --display-name $ApplicationName `
        --query appId `
        --output tsv `
        --only-show-errors
}

$applicationObjectId = az ad app show `
    --id $clientId `
    --query id `
    --output tsv `
    --only-show-errors

$servicePrincipalId = az ad sp list `
    --filter "appId eq '$clientId'" `
    --query "[0].id" `
    --output tsv `
    --only-show-errors
if (-not $servicePrincipalId) {
    Write-Host "Creating the deployment service principal ..."
    $servicePrincipalId = az ad sp create `
        --id $clientId `
        --query id `
        --output tsv `
        --only-show-errors
}

$credentialName = "github-$GitHubEnvironment"
$subject = "repo:${GitHubOwner}/${GitHubRepository}:environment:${GitHubEnvironment}"
$existingCredential = az ad app federated-credential list `
    --id $applicationObjectId `
    --query "[?name=='$credentialName'].name | [0]" `
    --output tsv `
    --only-show-errors

if (-not $existingCredential) {
    $credentialFile = Join-Path ([System.IO.Path]::GetTempPath()) "eventharbor-oidc-$([Guid]::NewGuid().ToString('N')).json"
    try {
        $credentialJson = @{
            name = $credentialName
            issuer = "https://token.actions.githubusercontent.com"
            subject = $subject
            audiences = @("api://AzureADTokenExchange")
            description = "EventHarbor production deployments from GitHub Actions"
        } | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText(
            $credentialFile,
            $credentialJson,
            [System.Text.UTF8Encoding]::new($false)
        )

        Write-Host "Creating the GitHub production-environment trust relationship ..."
        az ad app federated-credential create `
            --id $applicationObjectId `
            --parameters "@$credentialFile" `
            --only-show-errors `
            --output none
    }
    finally {
        if ([System.IO.File]::Exists($credentialFile)) {
            [System.IO.File]::Delete($credentialFile)
        }
    }
}

$roleAssignment = az role assignment list `
    --assignee-object-id $servicePrincipalId `
    --scope $resourceGroupId `
    --role Contributor `
    --query "[0].id" `
    --output tsv `
    --only-show-errors
if (-not $roleAssignment) {
    Write-Host "Granting Contributor only on the dedicated EventHarbor resource group ..."
    az role assignment create `
        --assignee-object-id $servicePrincipalId `
        --assignee-principal-type ServicePrincipal `
        --role Contributor `
        --scope $resourceGroupId `
        --only-show-errors `
        --output none
}

if ($ConfigureGitHub) {
    Assert-Command -Name "gh"
    gh auth status | Out-Null
    $repository = "$GitHubOwner/$GitHubRepository"

    gh api --method PUT "repos/$repository/environments/$GitHubEnvironment" --silent
    gh variable set AZURE_CLIENT_ID --env $GitHubEnvironment --repo $repository --body $clientId
    gh variable set AZURE_TENANT_ID --env $GitHubEnvironment --repo $repository --body $tenantId
    gh variable set AZURE_SUBSCRIPTION_ID `
        --env $GitHubEnvironment `
        --repo $repository `
        --body $SubscriptionId
}

Write-Host ""
Write-Host "GitHub OIDC configuration is ready. No Azure client secret was created."
Write-Host "Federated subject: $subject"
Write-Host "AZURE_CLIENT_ID: $clientId"
Write-Host "AZURE_TENANT_ID: $tenantId"
Write-Host "AZURE_SUBSCRIPTION_ID: $SubscriptionId"
if (-not $ConfigureGitHub) {
    Write-Host "Add these three values as variables in the GitHub '$GitHubEnvironment' environment."
}
