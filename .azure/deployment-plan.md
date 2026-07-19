# Azure Deployment Plan

> **Status:** Application Deployed

Generated: 2026-07-19T20:20:00+09:00

---

## 1. Project Overview

**Goal:** Promote the benchmarked GPT-5.6 Luna deployment to CrewMeal's production slide-image model, update every related product and documentation surface, deploy the web and worker services, deploy the public introduction site, then commit and push the complete change set.

**Path:** Modify Existing

**Approval:** The user explicitly directed the agent to switch production to Luna and to deploy, commit, and push all related changes.

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Production rollout of the existing CrewMeal PoC |
| Scale | Small, single-region |
| Budget | Cost-optimized |
| Subscription | ME-ABSx04287555-jechoi-1 (`1004da59-37b1-4a22-80e1-019cc29ce8f1`) |
| Location | `eastus2` for application and Foundry resources; existing PostgreSQL remains in `centralus` |
| Model | Existing `gpt-5-6-luna-test` deployment (`gpt-5.6-luna`, 500K TPM, GlobalStandard) |
| Rollback | Keep the existing `gpt-5-2` deployment available as a no-provisioning fallback |
| Public site | Publish anonymized 10-document/227-slide benchmark results; do not expose customer filenames or source content |

### Policy Constraints

- Existing subscription Defender policy assignments remain unchanged.
- Existing organizational policy may disable public access on Storage and Key Vault; the deployed application already uses PostgreSQL artifacts and Container App inline secrets for the active path.
- No RBAC, network, database, or resource-deletion changes are planned.

---

## 3. Components Detected

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| web | API and server-rendered portal | Python, FastAPI, Jinja, Docker | `src/crewmeal/search_enhancement/web`, `Dockerfile` |
| worker | Background document processor | Python, LibreOffice, Azure OpenAI, Docker | `src/crewmeal/search_enhancement`, `Dockerfile` |
| introduction-site | Static public site | HTML, CSS, JavaScript, GitHub Pages | `docs/` |
| infrastructure | Existing Azure deployment | AZD with subscription-scope Bicep | `azure.yaml`, `infra/` |
| SharePoint command | Client extension | SPFx/TypeScript | `sharepoint/search-enhancement-command/` |

### Existing Production Resources

- Foundry AIServices account with GPT-5.2, GPT-5-mini, GPT-5.6 Luna, and embedding deployments.
- Two Azure Container Apps (`web`, `worker`) using one ACR image source.
- Existing Container Apps environment, ACR, PostgreSQL, Storage, Key Vault, managed identity, and Log Analytics workspace.

---

## 4. Recipe Selection

**Selected:** AZCLI for the in-place production rollout, while retaining AZD/Bicep as the repository source of truth.

**Rationale:**

- The running environment already exists and contains all required resources and secrets.
- The validated Luna deployment already exists, so no model provisioning is needed.
- An in-place ACR build plus Container App revision update avoids re-provisioning unrelated secret-bearing infrastructure.
- Bicep and generated ARM JSON will still be updated and compiled so future AZD deployments preserve the Luna default.
- GitHub Pages will be deployed from the pushed branch using the existing workflow.

---

## 5. Architecture

**Stack:** Containers

### Service Mapping

| Component | Azure Service | SKU |
|-----------|---------------|-----|
| web | Azure Container Apps | Existing consumption environment |
| worker | Azure Container Apps | Existing consumption environment with PostgreSQL queue scaling |
| container image | Azure Container Registry | Existing Standard |
| vision model | Microsoft Foundry / Azure OpenAI | Existing GPT-5.6 Luna GlobalStandard, 500K TPM |
| fallback model | Microsoft Foundry / Azure OpenAI | Existing GPT-5.2 GlobalStandard, 500K TPM |
| database | PostgreSQL Flexible Server | Existing Standard_B1ms |
| introduction site | GitHub Pages | Existing repository workflow |

### Rollout Changes

1. Set application and infrastructure defaults to model label `gpt-5.6-luna` and deployment `gpt-5-6-luna-test`.
2. Keep Content Understanding's GPT deployment and the GPT-5.2 deployment unchanged.
3. Set Luna pricing defaults to input `$1/M` and output `$6/M`.
4. Make model labels in portal cost estimates configuration-driven.
5. Update tests, README, Bicep, generated ARM JSON, and the bilingual introduction site.
6. Build one immutable ACR image and deploy that same image to web and worker.
7. Set explicit Luna model, deployment, and pricing environment variables on both Container Apps.
8. Verify revision health/readiness and live environment settings.
9. Commit, push, dispatch GitHub Pages, and create/update the pull request.

---

## 6. Provisioning Limit Checklist

No new Azure resources or model quota are requested. The rollout updates existing resources in place.

| Resource Type | Number to Deploy | Total After Deployment | Limit/Quota | Notes |
|---------------|------------------|------------------------|-------------|-------|
| `Microsoft.CognitiveServices/accounts/deployments` | 0 new | Existing Luna deployment remains 500K TPM | Existing allocation verified | `gpt-5-6-luna-test` is `Succeeded`; no quota delta |
| `Microsoft.App/containerApps` | 0 new | 2 | Existing environment count 1 of 50 | Fetched via `az quota`; 49 managed environments available |
| `Microsoft.ContainerRegistry/registries` | 0 new | 1 | Existing registry reused | Image push only; no registry resource change |
| `Microsoft.DBforPostgreSQL/flexibleServers` | 0 new | 1 | Existing server reused | No SKU, storage, or region change |

**Status:** All required resources already exist; no provisioning-capacity increase is required.

---

## 7. Execution Checklist

### Phase 1: Planning

- [x] Analyze workspace
- [x] Gather requirements from the user's explicit rollout instruction
- [x] Confirm existing production subscription and location context
- [x] Inspect subscription policy assignments
- [x] Prepare resource inventory
- [x] Check relevant quota/capacity
- [x] Scan codebase
- [x] Select recipe
- [x] Plan architecture and rollback
- [x] User approved production deployment, commit, and push

### Phase 2: Execution

- [x] Update application defaults and pricing
- [x] Update Bicep and generated ARM JSON
- [x] Update portal copy, tests, README, and introduction site
- [x] Run functional verification (165 tests passed; Bicep compiled; bilingual site checked in browser)
- [x] Update status to `Ready for Validation`

### Phase 3: Validation

- [x] All validation checks pass
  - [x] Azure CLI installation
  - [x] Authentication and target subscription
  - [x] Bicep compilation and linting
  - [x] Subscription template validation
  - [x] Subscription what-if preview
  - [x] Container image build
  - [x] Azure Policy validation
  - [x] Static RBAC role verification
  - [x] Python build/test verification (165 tests passed)
  - [x] Introduction site browser verification (KO/EN)
- [x] Record validation proof below

### Phase 4: Deployment

- [x] Invoke azure-deploy
- [x] Build and deploy immutable image to web and worker
- [x] Verify live health, readiness, model settings, revisions, RBAC, and an actual two-slide analysis
- [ ] Commit and push
- [ ] Deploy and verify GitHub Pages
- [ ] Update status to `Deployed`

---

## 8. Validation Proof

| Check | Command or review | Result |
|-------|-------------------|--------|
| Azure CLI and authentication | `az version`, `az account show`, `az account set` | Authenticated to `ME-ABSx04287555-jechoi-1` (`1004da59-37b1-4a22-80e1-019cc29ce8f1`) |
| Bicep compilation | `az bicep build --file infra/main.bicep --outfile infra/main.json` | Succeeded with Bicep 0.42.1; generated ARM JSON is current |
| Provider template validation | `az deployment sub validate ...` | `Succeeded` against the existing `eastus2` resource group |
| What-if | `az deployment sub what-if ... --result-format ResourceIdOnly` | 32 `Deploy`, 1 `Ignore`, 0 `Create`, 0 `Delete`; proof saved as `luna-production-what-if.json` |
| Container build | `docker build --tag crewmeal:luna-validation .` | Succeeded; manifest `sha256:d064da40abb6634c836f4c28bb4e2da0d60f5899b828bba9f4c70718a81e5330` |
| Python verification | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` | 165 passed |
| Introduction site | Local HTTP server plus Playwright KO/EN checks | Luna model and benchmark sections rendered in both languages with no page console errors |
| Azure resources | Foundry and Container Apps queries | Luna and GPT-5.2 are both `Succeeded` at 500K TPM; web and worker are `Running` |
| Azure Policy | `az policy assignment list` | Three Defender assignments only; no policy conflicts with this in-place revision update |
| Static RBAC | Review of `infra/modules/foundry.bicep`, `platform.bicep`, and `pg-autostart.bicep` | Managed identities retain resource-scoped ACR Pull, Storage Blob Data Contributor, Key Vault Secrets User, Cognitive Services OpenAI User, and PostgreSQL start permissions |

Validation also corrected the Bicep resource-group name to the existing `rg-crewmeal-ppt-poc-eus2`; before the fix, what-if incorrectly targeted a new resource group.

---

## 9. Deployment Proof

| Check | Result |
|-------|--------|
| Immutable image | `acrcrewmealpocgdfqpz5zn7qyu.azurecr.io/crewmeal-ppt/runtime-poc:luna-prod-20260719-120904z` |
| Image digest | `sha256:07935f2a79d1ca3b8ebacd09b6f05d674e1a42ed380f89fa3fe9dece0056080e` |
| Web revision | `ca-web-poc-gdfqpz5zn7qyu--luna-1209` — active, healthy, running |
| Worker revision | `ca-worker-poc-gdfqpz5zn7qyu--luna-1209` — active, healthy, running |
| Runtime environment | Both apps explicitly set model `gpt-5.6-luna`, deployment `gpt-5-6-luna-test`, and pricing `$1/M` input / `$6/M` output |
| Persistent model override | Admin settings store `azure_openai`, `gpt-5.6-luna`, `gpt-5-6-luna-test`, and reasoning effort `high` |
| Web health | `https://ca-web-poc-gdfqpz5zn7qyu.proudground-628f0fb5.eastus2.azurecontainerapps.io/healthz`, `/readyz`, and `/` returned HTTP 200 |
| Production smoke | A two-slide PPTX upload reached `Ready`; stored analysis metadata reports model `gpt-5.6-luna`, deployment `gpt-5-6-luna-test`, and two analyzed slides |
| Mixed-model history | Live dashboard applies GPT-5.2 `$1.75/$14` and Luna `$1/$6` rates separately instead of repricing history at the current-model rate |
| Live RBAC | ACR Pull, Storage Blob Data Contributor, Key Vault Secrets User, Cognitive Services OpenAI User, and PostgreSQL Contributor assignments are present at the intended resource scopes |

---

## 10. Files to Modify

| File or Area | Purpose | Status |
|--------------|---------|--------|
| `.azure/deployment-plan.md` | Deployment source of truth | Application deployed; publication pending |
| `src/crewmeal/config.py` | Luna application defaults | Complete |
| `src/crewmeal/search_enhancement/pricing.py` | Luna pricing and model label | Complete |
| `src/crewmeal/search_enhancement/web/templates/` | Dynamic Luna cost labels | Complete |
| `infra/modules/foundry.bicep` | Preserve GPT-5.2 and declare existing Luna deployment | Complete |
| `infra/modules/platform.bicep` | Explicit model and pricing environment | Complete |
| `infra/main.bicep`, `infra/main.json` | Wire Luna slide-image deployment and outputs | Complete |
| `tests/` | Default/config/pricing/template coverage | Complete |
| `README.md` | Operating model and benchmark evidence | Complete |
| `docs/index.html` | Public bilingual Luna benchmark section | Complete |

---

## 11. Next Steps

> Current: Application deployed and verified

1. Commit and push the rollout.
2. Publish and verify the introduction site.
