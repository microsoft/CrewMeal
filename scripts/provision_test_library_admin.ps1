$ErrorActionPreference = "Stop"

$environmentNames = @(
    "CREWMEAL_M365_TENANT_ID",
    "CREWMEAL_M365_CLIENT_ID",
    "CREWMEAL_M365_CLIENT_SECRET",
    "CREWMEAL_M365_SITE_ID",
    "CREWMEAL_M365_DRIVE_ID",
    "CREWMEAL_M365_LIST_ID",
    "CREWMEAL_M365_SITE_URL",
    "CREWMEAL_M365_CONNECTION_ID"
)

foreach ($name in $environmentNames) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if (-not $value) {
        throw "Missing user environment variable: $name"
    }
    Set-Item -Path "Env:$name" -Value $value
}

Import-Module Microsoft.Graph.Authentication
Connect-MgGraph `
    -TenantId $env:CREWMEAL_M365_TENANT_ID `
    -Scopes "Sites.FullControl.All" `
    -UseDeviceCode `
    -NoWelcome

$permissionsUri = (
    "https://graph.microsoft.com/v1.0/sites/" +
    "$($env:CREWMEAL_M365_SITE_ID)/permissions"
)
$permissions = Invoke-MgGraphRequest -Method GET -Uri $permissionsUri
$permission = $permissions.value |
    Where-Object {
        $_.grantedToIdentitiesV2.application.id -eq
            $env:CREWMEAL_M365_CLIENT_ID -or
        $_.grantedToIdentities.application.id -eq
            $env:CREWMEAL_M365_CLIENT_ID
    } |
    Select-Object -First 1

if (-not $permission) {
    throw "The PoC app has no Sites.Selected permission on the test site."
}

$permissionUri = "$permissionsUri/$($permission.id)"
$fullControlBody = @{ roles = @("fullcontrol") } | ConvertTo-Json
$writeBody = @{ roles = @("write") } | ConvertTo-Json

try {
    Invoke-MgGraphRequest `
        -Method PATCH `
        -Uri $permissionUri `
        -Body $fullControlBody `
        -ContentType "application/json" | Out-Null

    $venvPython = "$PSScriptRoot\..\.venv\Scripts\python.exe"
    $python = if (Test-Path $venvPython) {
        $venvPython
    }
    else {
        $sourcePath = (Resolve-Path "$PSScriptRoot\..\src").Path
        $env:PYTHONPATH = if ($env:PYTHONPATH) {
            "$sourcePath$([IO.Path]::PathSeparator)$env:PYTHONPATH"
        }
        else {
            $sourcePath
        }
        (Get-Command python -ErrorAction Stop).Source
    }
    $configureArgs = @("$PSScriptRoot\configure_test_library.py")
    if ($env:CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN) {
        $configureArgs += "--apply-content-column"
    }
    & $python @configureArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Test-library provisioning failed with exit code $LASTEXITCODE."
    }
}
finally {
    $restoredPermission = Invoke-MgGraphRequest `
        -Method PATCH `
        -Uri $permissionUri `
        -Body $writeBody `
        -ContentType "application/json"
    if ($restoredPermission.roles -notcontains "write") {
        throw "Failed to restore the test-site app role to write."
    }
}

[pscustomobject]@{
    siteId = $env:CREWMEAL_M365_SITE_ID
    applicationId = $env:CREWMEAL_M365_CLIENT_ID
    runtimeRole = "write"
    contentColumnAttached = [bool]$env:CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN
    provisioningCompleted = $true
} | ConvertTo-Json
