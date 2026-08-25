[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $SubscriptionId,

    [string] $ResourceGroup = "rg-eventharbor-prod",
    [string] $ContainerAppName = "eventharbor-web-prod",
    [string] $ContainerAppsEnvironment = "cae-eventharbor-prod",
    [string] $HostName = "eventharbor.irfanburakozer.com"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is not installed or is not on PATH."
}

az account set --subscription $SubscriptionId
$generatedFqdn = az containerapp show `
    --resource-group $ResourceGroup `
    --name $ContainerAppName `
    --query properties.configuration.ingress.fqdn `
    --output tsv `
    --only-show-errors
$verificationId = az containerapp env show `
    --resource-group $ResourceGroup `
    --name $ContainerAppsEnvironment `
    --query properties.customDomainConfiguration.customDomainVerificationId `
    --output tsv `
    --only-show-errors

if (-not $generatedFqdn -or -not $verificationId) {
    throw "The web app or its domain-verification value could not be read. Deploy the apps first."
}

$recordName = $HostName.Split(".")[0]
Write-Host ""
Write-Host "Create these records in Cloudflare DNS:"
Write-Host ""
Write-Host "CNAME  $recordName          $generatedFqdn"
Write-Host "TXT    asuid.$recordName    $verificationId"
Write-Host ""
Write-Host "Set the CNAME to DNS only (gray cloud), not Proxied."
Write-Host "If your zone already has CAA records, also allow DigiCert: CAA @ 0 issue digicert.com"
Write-Host "After DNS resolves, add '$HostName' to $ContainerAppName and choose an Azure managed certificate."

