[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $SubscriptionId,

    [Parameter(Mandatory)]
    [string] $GitHubOwner,

    [Parameter(Mandatory)]
    [string] $GitHubRepository,

    [string] $ResourceGroup = "rg-eventharbor-prod",
    [string] $GitHubBranch = "main",
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
Assert-Command -Name "gh"

gh auth status | Out-Null
$requestedRepository = "$GitHubOwner/$GitHubRepository"
$repositoryMetadataJson = gh api "repos/$requestedRepository"
if ($LASTEXITCODE -ne 0 -or -not $repositoryMetadataJson) {
    throw "GitHub repository '$requestedRepository' could not be read. Run 'gh auth login' and verify the owner and repository names."
}

$repositoryMetadata = $repositoryMetadataJson | ConvertFrom-Json
$canonicalGitHubOwner = [string] $repositoryMetadata.owner.login
$canonicalGitHubRepository = [string] $repositoryMetadata.name
$githubOwnerId = [string] $repositoryMetadata.owner.id
$githubRepositoryId = [string] $repositoryMetadata.id
if (
    -not $canonicalGitHubOwner `
    -or -not $canonicalGitHubRepository `
    -or -not $githubOwnerId `
    -or -not $githubRepositoryId
) {
    throw "GitHub did not return the immutable owner and repository identifiers required for OIDC."
}

$repository = "$canonicalGitHubOwner/$canonicalGitHubRepository"

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

$credentialBranchName = $GitHubBranch -replace '[^A-Za-z0-9-]', '-'
$credentialName = "github-$credentialBranchName"
$subject = "repo:${canonicalGitHubOwner}@${githubOwnerId}/${canonicalGitHubRepository}@${githubRepositoryId}:ref:refs/heads/${GitHubBranch}"
$existingCredentialJson = az ad app federated-credential list `
    --id $applicationObjectId `
    --query "[?name=='$credentialName'] | [0]" `
    --output json `
    --only-show-errors
$existingCredential = $null
if ($existingCredentialJson -and $existingCredentialJson -ne "null") {
    $existingCredential = $existingCredentialJson | ConvertFrom-Json
}

if (-not $existingCredential -or [string] $existingCredential.subject -ne $subject) {
    $credentialFile = Join-Path ([System.IO.Path]::GetTempPath()) "eventharbor-oidc-$([Guid]::NewGuid().ToString('N')).json"
    try {
        $credentialParameters = @{
            issuer = "https://token.actions.githubusercontent.com"
            subject = $subject
            audiences = @("api://AzureADTokenExchange")
            description = "EventHarbor production deployments from GitHub Actions"
        }
        if (-not $existingCredential) {
            $credentialParameters.name = $credentialName
        }

        $credentialJson = $credentialParameters | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText(
            $credentialFile,
            $credentialJson,
            [System.Text.UTF8Encoding]::new($false)
        )

        if ($existingCredential) {
            Write-Host "Updating the GitHub main-branch trust relationship to the immutable repository subject ..."
            az ad app federated-credential update `
                --id $applicationObjectId `
                --federated-credential-id $credentialName `
                --parameters "@$credentialFile" `
                --only-show-errors `
                --output none
        }
        else {
            Write-Host "Creating the GitHub main-branch trust relationship ..."
            az ad app federated-credential create `
                --id $applicationObjectId `
                --parameters "@$credentialFile" `
                --only-show-errors `
                --output none
        }
    }
    finally {
        if ([System.IO.File]::Exists($credentialFile)) {
            [System.IO.File]::Delete($credentialFile)
        }
    }
}
else {
    Write-Host "The GitHub main-branch trust relationship already matches the immutable repository subject."
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
    gh variable set AZURE_CLIENT_ID --repo $repository --body $clientId
    gh variable set AZURE_TENANT_ID --repo $repository --body $tenantId
    gh variable set AZURE_SUBSCRIPTION_ID `
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
    Write-Host "Add these three values as GitHub repository Actions variables."
}
