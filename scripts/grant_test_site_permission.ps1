$ErrorActionPreference = "Stop"

$tenantId = [Environment]::GetEnvironmentVariable(
    "CREWMEAL_M365_TENANT_ID",
    "User"
)
$clientId = [Environment]::GetEnvironmentVariable(
    "CREWMEAL_M365_CLIENT_ID",
    "User"
)
$siteId = [Environment]::GetEnvironmentVariable(
    "CREWMEAL_M365_SITE_ID",
    "User"
)

if (-not $tenantId -or -not $clientId -or -not $siteId) {
    throw "The Crewmeal Microsoft 365 user environment variables are missing."
}

Import-Module Microsoft.Graph.Authentication
Connect-MgGraph `
    -TenantId $tenantId `
    -Scopes "Sites.FullControl.All" `
    -UseDeviceCode `
    -NoWelcome

$uri = "https://graph.microsoft.com/v1.0/sites/$siteId/permissions"
$permissions = Invoke-MgGraphRequest -Method GET -Uri $uri
$existing = $permissions.value |
    Where-Object {
        $_.grantedToIdentitiesV2.application.id -eq $clientId -or
        $_.grantedToIdentities.application.id -eq $clientId
    } |
    Select-Object -First 1

if (-not $existing) {
    $body = @{
        roles = @("write")
        grantedToIdentities = @(
            @{
                application = @{
                    id = $clientId
                    displayName = "crewmeal-ppt-search-poc"
                }
            }
        )
    } | ConvertTo-Json -Depth 6
    $existing = Invoke-MgGraphRequest `
        -Method POST `
        -Uri $uri `
        -Body $body `
        -ContentType "application/json"
}

[pscustomobject]@{
    permissionId = $existing.id
    roles = $existing.roles
    applicationId = $clientId
    siteId = $siteId
} | ConvertTo-Json -Depth 4
