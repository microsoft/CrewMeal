# CrewMeal MIP File SDK adapter

A small, headless .NET console app that wraps the official **Microsoft
Information Protection (MIP) File SDK** and satisfies CrewMeal's decryption seam
(`src/crewmeal/search_enhancement/mip_sdk.py`). It is the *production* backend
for `CREWMEAL_MIP_SDK_CLI` — the real replacement for the bundled reference tool
(`python -m crewmeal.search_enhancement.mip_tool`).

## Contract

```
crewmeal-mip-adapter unprotect --in <input> --out <output> --token-file <token> \
    [--client-id <guid>] [--identity <upn>] [--name <original-filename>]
```

* exit `0` → success; the unprotected bytes are written to `<output>`.
* nonzero → failure; `stderr` explains why (`2` usage, `3` access denied,
  `4` operation failed, `5` unexpected).

If the input is not actually protected, the bytes are copied through unchanged.

## Authentication (unattended, app-only)

The MIP File SDK challenges for **two** resources in turn, so a single
pre-acquired token is not enough. The adapter mints an app-only token **per
requested resource** using the M365 service principal's client credentials:

| Stage | Resource | Purpose |
| --- | --- | --- |
| Engine creation | `https://syncservice.o365syncservice.com/` | Download the tenant label policy |
| Decryption | `https://aadrm.com/` | Release the content key (RMS) |

Credentials are read from the environment (same service principal CrewMeal uses):

* `CREWMEAL_M365_TENANT_ID`
* `CREWMEAL_M365_CLIENT_ID` (also accepted via `--client-id`; must match the
  Entra app registration)
* `CREWMEAL_M365_CLIENT_SECRET`

If no client secret is present, the adapter falls back to the bearer token in
`--token-file` (RMS only). Optional: `CREWMEAL_MIP_CLOUD` (default `Commercial`;
e.g. `GccHigh`, `Dod`), `CREWMEAL_MIP_ENGINE_IDENTITY` (a UPN to aid RMS region
discovery).

## Required tenant permissions (application, admin-consented)

For app-only decryption the service principal needs **both**:

| API | App role | Why |
| --- | --- | --- |
| Azure Rights Management Services (`00000012-…`) | `Content.SuperUser` (`7347eb49-…`) | Decrypt any protected content in the tenant |
| Microsoft Information Protection Sync Service (`870c4f2e-…`) | `UnifiedPolicy.Tenant.Read` (`8b2071cd-…`) | Read the tenant label policy to create a file engine |

Grant + consent (admin):

```bash
az ad app permission add --id <clientId> --api 00000012-0000-0000-c000-000000000000 \
    --api-permissions 7347eb49-7a1a-43c5-8eac-a5cd1d1c7cf0=Role
az ad app permission add --id <clientId> --api 870c4f2e-85b6-4d43-bdda-6ed9a579b725 \
    --api-permissions 8b2071cd-015a-4025-8052-1c0dba2d3f64=Role
az ad app permission admin-consent --id <clientId>
```

`Content.SuperUser` covers **consumption** (decryption). To *create* protected
content (e.g. to generate test files) the app additionally needs
`Content.Writer` — not required for CrewMeal's decrypt-only workload.

## Build

```powershell
dotnet publish -c Release -r win-x64   --no-self-contained   # local (Windows)
dotnet publish -c Release -r linux-x64 --no-self-contained   # Docker / prod
```

The `Microsoft.InformationProtection.File` package brings the native runtime
libraries; they are laid down next to the published executable per RID.

## Wire into CrewMeal

Point the seam at the published executable:

```
CREWMEAL_MIP_SDK_CLI=".../publish/crewmeal-mip-adapter"
```

CrewMeal then uses this adapter automatically wherever it decrypts MIP-protected
documents (worker, `/admin/tryout`, and the `mip_preflight` `--sample` check).

## Licensing

This project references the Microsoft Information Protection File SDK under its
own license terms. The native SDK binaries are fetched from NuGet at build time
and are **not** committed to this repository.
