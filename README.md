# CrewMeal — 멀티포맷 콘텐츠 강화 플랫폼

> **CrewMeal** = Copilot에게 제공하는 양질의 콘텐츠 → 부조종사(co-pilot)가 기내에서 먹는
> 기내식 **Crew Meal**. Copilot이 잘 일하도록 먹여주는 잘 차려진 한 끼라는 뜻입니다.

복잡한 사내 문서(`.pptx`·`.pdf`·`.hwp`)를 구조화된 검색 메타데이터로 강화해 Microsoft Graph
검색 커넥터(Copilot Connector)에 `externalItem`으로 색인하는 로컬 워커입니다.
각 페이지를 이미지로 렌더링하고, 원문 텍스트·도형 근거를 함께 비전 LLM에 전달해
strict JSON Schema로 분석한 뒤 허용 태그 HTML로 렌더링합니다. 원본 문서는 수정하지
않습니다. 분석 모델은 관리 포털에서 교체할 수 있으며(비용 절감), 포맷별 지원과 암호화
복호화는 관리자 토글로 켜고 끕니다.

> 📄 **소개 사이트**: `docs/`의 정적 페이지(실제 구동 화면 캡처 포함)는 GitHub Pages로
> 배포됩니다 — <https://microsoft.github.io/CrewMeal/>. 프로젝트 저장소는
> <https://github.com/microsoft/CrewMeal> 입니다.

## CrewMeal 확장 기능

| 기능 | 상태 | 설명 |
| --- | --- | --- |
| PPTX | ✅ 구현됨 | LibreOffice → PDF → 렌더 · 간트 geometry 근거 |
| PDF | ✅ 구현됨 | PyMuPDF 직접 렌더 · 페이지 텍스트 근거 · 암호화 PDF 라우팅 |
| HWP/HWPX | ✅ 구현됨 | LibreOffice Writer 필터 → PDF → 렌더 |
| DOCX | 🧩 구조만 | 핸들러 등록·감지, 추출 로직은 명확한 `NotImplemented` |
| XLSX | 🧩 구조만 | 핸들러 등록·감지, 셀/표 추출 예정 |
| 모델 교체 | ✅ 구조 | 관리 포털에서 provider·배포·엔드포인트·reasoning 교체(env fallback) |
| MIP 복호화 | 🧩 구조·토글 | 관리자 on/off 훅. 활성화 시 실제 복호화는 배포 환경에 맞춰 확장 |
| 기타 복호화 | 🧩 구조·토글 | 배포별 암호화 솔루션용 복호화 파이프라인 훅 |

- **모든 포맷은 SharePoint 인제스트 경로로 흐릅니다.** 포맷을 켜면 인제스트·재조정·시연
  업로드에 자동 편입됩니다. 포맷별 활성화는 `format.<id>.enabled` 설정으로 관리하며
  구현되지 않은(스켈레톤) 포맷은 켤 수 없습니다.
- **분석 모델 교체**는 `/admin/settings`의 「이미지 분석 모델」 카드에서 설정하며, 빈 값은
  환경 변수 기본값을 사용합니다. 변경은 워커 재시작 후 적용됩니다.
- **복호화 파이프라인**은 기본 꺼짐이며, 아직 구현되지 않은 제공자를 켜면 해당 문서 처리
  시 명확한 오류로 실패합니다(활성화 사실이 로그·상태에 남습니다).

## 처리 흐름

`src/crewmeal/search_enhancement`의 워커가 SharePoint 라이브러리를 폴링하고
등록된 문서마다 다음을 수행합니다.

1. 문서를 임시 폴더에만 내려받고 포맷을 감지(확장자·매직·크기)한 뒤 검증합니다.
2. (활성화 시) 복호화 훅이 암호화된 문서를 복호화합니다.
3. 페이지의 보이는 원문 텍스트와 요소 수를 source manifest로 추출합니다.
4. 포맷 핸들러가 각 페이지를 이미지(PPTX/HWP는 LibreOffice→PDF, PDF는 PyMuPDF)로
   렌더링합니다.
5. (PPTX) Open XML에서 간트 막대·연결선 같은 결정적 geometry 근거를 계산합니다.
6. 각 페이지 이미지를 비전 LLM에 근거와 함께 전달해 병렬 분석하고 strict JSON Schema
   응답을 받습니다. 노트·alt text·숨김 요소는 근거에 포함하지 않습니다.
7. 응답을 허용 태그 HTML로 렌더링하고 `externalItem`으로 게시합니다.

원본 문서·PDF·PNG·HTML 본문과 비밀 값은 SQLite 상태에 저장하지 않습니다.

> 프로덕션 서버 모드에서는 상태 저장소가 PostgreSQL이고, 단계별 진행은 `job_events`
> 타임라인으로 기록되며, 추출 HTML·구조화 JSON 등 산출물은 산출물 저장소에 저장되어
> 상태 페이지에서 열람합니다. 산출물 저장소는 `CREWMEAL_ARTIFACT_BACKEND`로 고르며
> `blob`(Azure Blob), `database`(PostgreSQL `artifact_blobs` 테이블), 로컬 파일 중
> 하나입니다. 배포된 PoC는 Storage·Key Vault 공개 접근이 구독 정책으로 강제 차단되어
> `database` 저장소와 Container App 인라인 시크릿을 사용합니다. 로컬 워커와 단위
> 테스트는 계속 SQLite와 로컬 파일을 사용합니다.

## 준비 사항

- Python 3.11 이상
- LibreOffice (`soffice.exe`)
- Azure CLI 및 Azure Developer CLI 1.28.0 이상
- 대상 구독의 `Owner` 또는 리소스 생성 권한과
  `Microsoft.Authorization/roleAssignments/write`
- SharePoint 테스트 사이트와 Microsoft 365 앱 권한
- 비민감 테스트 문서

현재 Bicep 기본값은 구독
`1004da59-37b1-4a22-80e1-019cc29ce8f1`의 `eastus2` 배포를 전제로 하며, PostgreSQL만
구독 정책상 offer가 제한되지 않는 `centralus`에 둡니다(`postgresLocation` 파라미터).
공개 endpoint를 사용하므로 민감 문서를 입력하지 마세요.

## 로컬 설치

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

LibreOffice 자동 탐색 경로에 없다면 설정합니다.

```powershell
$env:SOFFICE_PATH = "C:\Program Files\LibreOffice\program\soffice.exe"
```

## Azure 배포

프로덕션은 단일 컨테이너 이미지를 **웹 앱**(Ingest API + 상태 페이지 + 관리자 포탈)과
**워커**(LibreOffice 변환 파이프라인)로 각각 실행합니다. 요청은 SharePoint 열이 아니라
서버 DB 큐(PostgreSQL)에 적재되고 워커가 claim/lease로 자동 처리합니다. 이미지는
`Dockerfile` 하나이며 `APP_ROLE=web|worker` 환경변수로 역할을 분기합니다.

`infra/main.bicep` 배포 항목:

- Microsoft Foundry `AIServices` S0 계정 + 운영 `gpt-5.6-luna` / 폴백 `gpt-5.2` / `gpt-5-mini` / `text-embedding-3-large` 배포
- Container Apps 환경(+ Log Analytics), 웹 Container App(외부 ingress:8000), 워커 Container App(ingress 없음, min 1)
- Azure Container Registry(Standard), PostgreSQL Flexible Server 16(+`crewmeal` DB, `centralus`), Storage(Blob `artifacts` 컨테이너)
- Key Vault(M365 secret·admin key·session secret·DB URL), user-assigned Managed Identity
- MI RBAC: AcrPull, Storage Blob Data Contributor, Key Vault Secrets User, Cognitive Services OpenAI User

> 배포된 PoC에서는 구독 정책이 Storage·Key Vault의 공개 접근을 강제로 Disabled로
> 되돌립니다. 그래서 실제 실행 구성은 Blob·Key Vault 대신 **PostgreSQL 산출물 저장소**
> (`CREWMEAL_ARTIFACT_BACKEND=database`)와 **Container App 인라인 시크릿**을 사용합니다.
> Storage·Key Vault 리소스는 여전히 배포되지만 이 경로에서는 사용하지 않습니다.

### 배포 전 시크릿·환경변수

`azd env set`으로 다음을 설정합니다. Microsoft 365 값은 로컬 개발 환경변수와 동일합니다.

```powershell
azd auth login
azd env new poc
azd env set AZURE_SUBSCRIPTION_ID 1004da59-37b1-4a22-80e1-019cc29ce8f1
azd env set AZURE_LOCATION eastus2

# 생성 시크릿 — DATABASE_URL에 들어가므로 암호는 URL 안전 문자만 사용(@ : / ? 회피)
azd env set POSTGRES_ADMIN_PASSWORD "<강력한 URL 안전 암호>"
azd env set CREWMEAL_ADMIN_KEY        "<관리자 포탈 키>"
azd env set CREWMEAL_WEB_SESSION_SECRET "<세션 서명 시크릿>"

# Microsoft 365 앱 (워커 처리 및 ingest driveItem 해석에 필요)
azd env set CREWMEAL_M365_TENANT_ID    "<tenant>"
azd env set CREWMEAL_M365_CLIENT_ID    "<client>"
azd env set CREWMEAL_M365_CLIENT_SECRET "<secret>"
azd env set CREWMEAL_M365_SITE_ID      "<site>"
azd env set CREWMEAL_M365_DRIVE_ID     "<drive>"
azd env set CREWMEAL_M365_LIST_ID      "<list>"
azd env set CREWMEAL_M365_SITE_URL     "<siteUrl>"
azd env set CREWMEAL_M365_CONNECTION_ID "<connectionId>"

# Ingest 인증: 초기 PoC는 무인증으로 시작할 수 있습니다.
azd env set CREWMEAL_INGEST_REQUIRE_AUTH false
azd env set CREWMEAL_INGEST_AUDIENCE     "api://crewmeal-ingest"   # 무인증이면 자리표시자
```

인증을 켜려면 API용 Entra 앱 등록을 만들고, 그 App ID URI를 `CREWMEAL_INGEST_AUDIENCE`,
허용 앱 ID를 `CREWMEAL_INGEST_ALLOWED_APP_IDS`(CSV)에 설정한 뒤
`CREWMEAL_INGEST_REQUIRE_AUTH=true`로 둡니다. SPFx 확장에도 같은 audience와 웹 앱 URL을
구성합니다(아래 SPFx 섹션).

### 배포

```powershell
azd up
```

`azd up`은 인프라를 프로비저닝하고 `Dockerfile`을 빌드해 ACR에 푸시한 뒤 웹·워커
Container App을 갱신합니다. 완료 후 상태 페이지·관리자 포탈 URL은 `SERVICE_WEB_URI`
출력으로 확인합니다(관리자 포탈은 `SERVICE_WEB_URI/admin`, 키는 `CREWMEAL_ADMIN_KEY`).

Foundry만 프로비저닝하려면 `azd provision`, 코드만 재배포하려면 `azd deploy web`
또는 `azd deploy worker`를 사용합니다.

로컬 워커를 Foundry 출력으로 실행하려면 배포 후 환경변수를 현재 PowerShell에 로드합니다.

```powershell
$values = azd env get-values --output json | ConvertFrom-Json
$values.PSObject.Properties | ForEach-Object {
  Set-Item -Path "Env:$($_.Name)" -Value $_.Value
}
```

API key는 사용하거나 저장하지 않습니다. Azure OpenAI 및 Blob 저장소(사용 시) 인증은
워커에 연결된 user-assigned Managed Identity(`AZURE_CLIENT_ID`)를 통한
`DefaultAzureCredential`을 사용하고, 로컬 실행 시에는 Azure CLI/azd 로그인을 사용합니다. 워커가 사용하는 endpoint
환경변수는 `CONTENTUNDERSTANDING_ENDPOINT`이며 같은 Foundry 리소스를 가리킵니다.

운영 기본 모델은 GPT-5.6 Luna입니다. 기존 검증 배포를 사용하려면 다음 값을 설정합니다.

```powershell
$env:SLIDE_IMAGE_MODEL = "gpt-5.6-luna"
$env:SLIDE_IMAGE_DEPLOYMENT = "gpt-5-6-luna-test"
```

실제 업무 PPT 10개(227장) 비교에서 Luna는 10개를 모두 완료했고 GPT-5.2는
8개를 완료했습니다. 두 모델이 모두 완료한 169장 기준으로 Luna는 추정 토큰 비용을
39.65%, 요청 중앙 지연을 76.92%, p95 지연을 66.48% 줄였습니다. 원문 회수율은
GPT-5.2가 1~3%p 높았지만 대표 차이 슬라이드의 시각 검토에서는 대부분 문장 표현
차이였고, 실제 의미 있는 차이 1건은 Luna가 더 정확했습니다. GPT-5.2 배포는 초기
운영 폴백으로 유지합니다.

번들 6장 덱으로 비전 모델의 품질·지연·토큰·추정 비용을 같은 조건에서
비교하려면 모델 배포 이름과 현재 단가를 전달합니다. 결과 원문은 기본적으로
`result/vision-model-benchmark.json`에 저장됩니다.

```powershell
$env:PYTHONPATH = "src"
$env:CONTENTUNDERSTANDING_ENDPOINT = "https://<foundry-account>.cognitiveservices.azure.com"
python -m crewmeal.search_enhancement.benchmark `
  --model "gpt-5.2=<gpt-5.2-deployment>" `
  --price "gpt-5.2=1.75,14" `
  --model "gpt-5.6-luna=<gpt-5.6-luna-deployment>" `
  --price "gpt-5.6-luna=1,6"
```

### 웹 엔드포인트

배포된 웹 앱(`SERVICE_WEB_URI`)이 제공하는 경로입니다.

- `GET /healthz`, `GET /readyz` — liveness / readiness(= DB 연결) 프로브
- `POST /api/requests` — 강화·삭제 요청을 큐에 적재하는 Ingest API. SPFx 명령이 호출하며,
  `CREWMEAL_INGEST_REQUIRE_AUTH=true`일 때 Bearer 토큰을 검증합니다.
- `GET /s/{token}` — 사용자 상태 페이지(진행 타임라인·현재 단계·결과 링크)
  - `GET /s/{token}/html` — 추출된 강화 HTML 미리보기
  - `GET /s/{token}/progress` — 진행 단계 JSON
  - `POST /s/{token}/rerun` — 원본이 갱신되어 재작업 요청
  - `POST /s/{token}/comment` — 튜닝 코멘트를 프롬프트에 주입해 재작업(피드백 코퍼스 적재)
  - `POST /s/{token}/remove` — 색인에서 제거 요청
- `GET /admin` — 관리자 포탈(대시보드). `X-Admin-Key` 헤더 또는 로그인 세션으로 게이트하며
  키는 `CREWMEAL_ADMIN_KEY`입니다.
  - `/admin/documents`, `/admin/documents/{token}` — 문서 목록·상세, 문서별 rerun/remove·job retry
  - `/admin/settings` — 런타임 설정 조회·수정
  - `/admin/feedback`, `/admin/feedback/export.jsonl` — 튜닝 코멘트 코퍼스 열람·내보내기
  - `/admin/tryout` — 지원 문서(PPTX·PDF·HWP)를 직접 업로드해 파이프라인을 시험하는 플레이그라운드

상태 페이지·관리자의 comment 재작업이 남긴 튜닝 코멘트는 피드백 코퍼스로 축적되며
`/admin/feedback/export.jsonl`로 내보내 본체 분석 프롬프트·엔진 개선에 활용합니다.

## SharePoint 검색강화

전용 테스트 사이트에서 선택한 지원 문서만 추가 `externalItem`으로 색인하는 opt-in
워크플로입니다. 원본 문서는 수정하지 않고 SharePoint 기본 검색에도 그대로 남겨
native 항목과 connector 항목이 함께 있을 때의 Copilot 동작을 비교합니다.

### 구성

- 테스트 사이트:
  `https://absx04287555.sharepoint.com/sites/crewmeal-ppt-search-poc`
- Copilot connector connection: `crewpptsearchpoc`
- SharePoint Framework 1.23.2 ListView Command Set:
  `sharepoint/search-enhancement-command`
- 로컬 워커와 SQLite 상태:
  `src/crewmeal/search_enhancement`
- 구조화 출력 계약: `schemas/slide-content.schema.json`
- 상태 열 formatter: `sharepoint/status-column-formatting.json`

워커는 지원 문서를 임시 폴더에만 다운로드하고, 144 DPI 페이지 이미지와 (PPTX의 경우)
보이는 Open XML을 구성된 이미지 분석 모델(기본 GPT-5-mini)에 전달합니다. 모델의 strict JSON
Schema 응답은 코드가 허용 태그 HTML로 렌더링합니다. 발표자 노트는 PPTX에서 별도 섹션으로
추가하며 원본 문서, PDF, PNG, HTML 본문과 비밀 값은 SQLite에 저장하지 않습니다.

### Microsoft 365 설정

앱은 다음 application permission만 상시 사용합니다.

- Microsoft Graph `Sites.Selected`
- SharePoint `Sites.Selected`
- Microsoft Graph `ExternalConnection.ReadWrite.OwnedBy`
- Microsoft Graph `ExternalItem.ReadWrite.OwnedBy`

사이트 관리자 계정으로 한 번만 테스트 사이트 쓰기 권한을 부여합니다.

```powershell
.\scripts\grant_test_site_permission.ps1
```

앱의 client secret과 리소스 ID는 다음 사용자 환경 변수로 주입합니다. 값은
소스, SQLite, 로그에 기록하지 않습니다.

```text
CREWMEAL_M365_TENANT_ID
CREWMEAL_M365_CLIENT_ID
CREWMEAL_M365_CLIENT_SECRET
CREWMEAL_M365_SITE_ID
CREWMEAL_M365_DRIVE_ID
CREWMEAL_M365_LIST_ID
CREWMEAL_M365_SITE_URL
CREWMEAL_M365_CONNECTION_ID
```

사이트 권한 부여 후 connector와 라이브러리 열을 idempotent하게 구성합니다.

```powershell
.\.venv\Scripts\python.exe .\scripts\configure_copilot_connection.py
.\scripts\provision_test_library_admin.ps1
```

열 생성 동안에만 테스트 사이트의 app role을 `fullcontrol`로 올리고, 성공 또는
실패와 관계없이 `finally`에서 런타임 최소 권한인 `write`로 되돌립니다.
SharePoint Online은 client-secret app-only 토큰으로 REST 호출을 허용하지 않으므로
상태 열 formatter는 사이트 소유자의 인증된 SharePoint 세션에서 한 번 적용합니다.
인증서 기반 또는 delegated SharePoint 토큰을 사용하는 경우에만 해당 토큰을
`CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN`에 임시로 넣고
`configure_test_library.py --apply-formatting`을 실행합니다.

Microsoft 365 관리 센터의 **검색 및 인텔리전스 > 사용자 지정 > 세로 항목 >
All > 커넥터 결과 관리**에서 `crewpptsearchpoc`의 인라인 결과를 활성화해야
Copilot에서 항목이 노출됩니다.

### SharePoint 명령

SPFx 명령은 편집 권한이 있는 사용자가 PPTX 한 개를 선택했을 때만 표시됩니다.

- 미등록: **코파일럿을 위해 검색강화**
- 실패 또는 원본 갱신 필요: **검색강화 다시 시도**
- 대기·처리·완료: **검색강화 삭제**

SPFx 1.23.2는 Node `>=22.14 <23`이 필요합니다.

```powershell
cd .\sharepoint\search-enhancement-command
npm install
npm run build
npm run test:unit
```

생성 패키지는
`sharepoint/search-enhancement-command/sharepoint/solution/search-enhancement-command.sppkg`
입니다. App Catalog에 배포하고 전용 테스트 사이트에만 앱을 설치합니다.
tenant-wide deployment는 사용하지 않습니다.

### 워커

한 번 처리하거나 계속 폴링할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m crewmeal.search_enhancement.cli once
.\.venv\Scripts\python.exe -m crewmeal.search_enhancement.cli run
.\.venv\Scripts\python.exe -m crewmeal.search_enhancement.cli reconcile
```

`Queued → Processing → Ready/Failed`와 `Removing → NotEnabled` 전이를 명시적으로
기록합니다. request ID가 바뀌면 이전 작업은 게시하지 않으며, PPTX의 `ppt/`
파트만 정규화한 SHA-256 fingerprint 변경은 전체 재처리하고 ACL 변경은 AI 호출
없이 connector ACL만 갱신합니다. SharePoint 사용자 정의 열 갱신이 패키지 문서
속성을 다시 써 eTag, cTag, quickXorHash까지 바꿀 수 있으므로 이 값들은 슬라이드
콘텐츠 변경 판단에 사용하지 않습니다.

`externalItem.id`는 SharePoint `listItemUniqueId`의 32자리 GUID hex입니다.
원본 `webUrl`에는 `crewmealItemId` query parameter만 추가해
`urlToItemResolver`가 동일 ID를 결정적으로 복원하도록 했습니다. 클릭 대상은
여전히 원본 PPT입니다. HTML은 3,000,000 bytes 이하이고 전체 요청은
4,000,000 bytes 미만이어야 하며 초과 시 절단하지 않고 실패합니다.

라이브 Graph API에서는 ACL 또는 속성만 보낸 update가 각각 누락된
`properties`/`acl` 오류로 거부되거나, content를 생략한 update가 기존 본문을 빈
값으로 만들 수 있습니다. 따라서 갱신 시 현재 항목을 읽고 connector schema에
정의한 속성만 선별한 뒤 ACL·속성·HTML content를 함께 PUT합니다. GET에 추가되는
`ows_*` 같은 시스템 속성은 다시 보내지 않으며, 기존 content를 읽을 수 없으면
본문을 지우지 않고 명시적으로 실패합니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
az bicep build --file .\infra\main.bicep --stdout > $null
```

테스트는 설정, Open XML manifest, 실제 LibreOffice 변환, geometry 근거,
구조화 HTML 렌더링, `externalItem` 계약을 포함합니다.

## 정리

리소스가 더 필요하지 않으면 비용이 발생하지 않도록 삭제합니다.

```powershell
azd down --purge --force
```

다중 사이트, 첨부파일 본문, 대용량 산출물용 private endpoint 저장 경로는 아직
포함하지 않습니다. 산출물은 현재 PostgreSQL 저장소를 쓰며, 대용량·고빈도 환경에서는
Container Apps 환경에 private endpoint로 연결한 Blob으로 전환하는 것을 권장합니다.
