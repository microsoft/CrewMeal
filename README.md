# CrewMeal — 멀티포맷 콘텐츠 강화 플랫폼

> **CrewMeal** = Copilot에게 제공하는 양질의 콘텐츠 → 부조종사(co-pilot)가 기내에서 먹는
> 기내식 **Crew Meal**. Copilot이 잘 일하도록 먹여주는 잘 차려진 한 끼라는 뜻입니다.

복잡한 사내 문서(`.pptx`·`.pdf`·`.hwp`·`.hwpx`)를 구조화된 검색 메타데이터로 강화해
SharePoint 라이브러리의 검색용 일반 텍스트 컬럼 또는 Microsoft Graph 검색 커넥터
(Copilot Connector)의 `externalItem`으로 게시하는 워커입니다. 게시 방식은 관리자가
선택하며 새 설치는 선택 전까지 어떤 대상에도 게시하지 않습니다.
PPTX·PDF는 페이지 이미지와 원문 근거를 비전 LLM에 전달하고, HWP·HWPX는 rhwp
RenderTree에서 본문·표·머리말·꼬리말·각주를 직접 추출합니다. 이미지·수식처럼 semantic
payload가 없는 HWP 페이지만 선택적으로 렌더링·분석합니다. 분석 결과는 Connector용
허용 태그 HTML과 SharePoint 컬럼용 Markdown으로 렌더링하며 원본 문서는 수정하지
않습니다. 분석 모델은 관리 포털에서 교체할 수 있고,
포맷별 지원과 암호화 복호화는 관리자 토글로 켜고 끕니다.

> 📄 **소개 사이트**: `docs/`의 정적 페이지(실제 구동 화면 캡처 포함)는 GitHub Pages로
> 배포됩니다 — <https://microsoft.github.io/CrewMeal/>. 프로젝트 저장소는
> <https://github.com/microsoft/CrewMeal> 입니다.

## CrewMeal 확장 기능

| 기능 | 상태 | 설명 |
| --- | --- | --- |
| PPTX | ✅ 구현됨 | LibreOffice → PDF → 렌더 · 간트 geometry 근거 |
| PDF | ✅ 구현됨 | PyMuPDF 직접 렌더 · 페이지 텍스트 근거 · 암호화 PDF 라우팅 |
| HWP/HWPX | ✅ 구현됨 | rhwp RenderTree semantic-first · visual-only 페이지만 native-skia PNG + Vision |
| 게시 대상 선택 | ✅ 구현됨 | SharePoint 검색 컬럼 또는 Copilot Connector · 무중단 전환 상태 관리 |
| DOCX | 🧩 구조만 | 핸들러 등록·감지, 추출 로직은 명확한 `NotImplemented` |
| XLSX | 🧩 구조만 | 핸들러 등록·감지, 셀/표 추출 예정 |
| 모델 교체 | ✅ 구조 | 관리 포털에서 provider·배포·엔드포인트·reasoning 교체(env fallback) |
| MIP 복호화 | ✅ 구현됨 | MIP/IRM 마커 감지 → MIP File SDK CLI(subprocess)로 위임 복호화. 무인 인증(RMS 슈퍼유저 app-only 토큰). 로컬·CI·데모용 레퍼런스 CLI 내장 (아래 「MIP 복호화」) |
| 기타 복호화 | 🧩 구조·토글 | 배포별 암호화 솔루션용 복호화 파이프라인 훅 |

- **모든 포맷은 SharePoint 인제스트 경로로 흐릅니다.** 포맷을 켜면 인제스트·재조정·시연
  업로드에 자동 편입됩니다. 포맷별 활성화는 `format.<id>.enabled` 설정으로 관리하며
  구현되지 않은(스켈레톤) 포맷은 켤 수 없습니다.
- **분석 모델 교체**는 `/admin/settings`의 「이미지 분석 모델」 카드에서 설정하며, 빈 값은
  환경 변수 기본값을 사용합니다. 변경은 워커 재시작 후 적용됩니다.
- **복호화 파이프라인**은 기본 꺼짐입니다. **MIP 복호화는 구현되어 있으며**(아래 「MIP
  복호화」 참조) 켜면 MIP/IRM로 보호된 문서를 처리 전에 평문으로 복원합니다. SDK CLI를
  구성하지 않은 채 MIP를 켜거나 아직 구현되지 않은 제공자(기타 복호화)를 켜면 해당 문서
  처리 시 조용히 통과하지 않고 명확한 오류로 실패합니다(활성화 사실이 로그·상태에 남습니다).

## 처리 흐름

`src/crewmeal/search_enhancement`의 워커가 SharePoint 라이브러리를 폴링하고
등록된 문서마다 다음을 수행합니다.

1. 문서를 임시 폴더에만 내려받고 포맷을 감지(확장자·매직·크기)한 뒤 검증합니다.
2. (활성화 시) 복호화 훅이 **다운로드 직후·지문(fingerprint) 계산 전에** 암호화된 문서를
   평문으로 복원합니다. MIP 복호화는 MIP File SDK CLI로 위임합니다(「MIP 복호화」 참조).
   지문·포맷 감지·검증은 항상 평문을 대상으로 하므로 변경 감지가 올바르게 동작합니다.
3. 포맷별 기준 근거를 추출합니다. PPTX/PDF는 원문과 페이지 이미지를, HWP/HWPX는 rhwp
   RenderTree의 본문·표·머리말·꼬리말·각주를 사용합니다.
4. HWP/HWPX에서 이미지·수식 등 semantic payload가 없는 페이지에 한해서만 rhwp
   native-skia PNG를 생성합니다.
5. (PPTX) Open XML에서 간트 막대·연결선 같은 결정적 geometry 근거를 계산합니다.
6. PPTX/PDF 전체 페이지와 HWP/HWPX의 선택된 visual-only 페이지만 비전 LLM에 전달합니다.
   HWP semantic 원문과 표는 모델 결과로 대체하지 않고 시각 설명만 병합합니다.
7. 응답을 Connector용 허용 태그 HTML 또는 SharePoint 컬럼용 Markdown으로 렌더링해
   관리자가 선택한 대상에 게시합니다.

원본 문서·PDF·PNG·HTML 본문과 비밀 값은 SQLite 상태에 저장하지 않습니다.

> 프로덕션 서버 모드에서는 상태 저장소가 PostgreSQL이고, 단계별 진행은 `job_events`
> 타임라인으로 기록되며, 추출 HTML·구조화 JSON 등 산출물은 산출물 저장소에 저장되어
> 상태 페이지에서 열람합니다. 산출물 저장소는 `CREWMEAL_ARTIFACT_BACKEND`로 고르며
> `blob`(Azure Blob), `database`(PostgreSQL `artifact_blobs` 테이블), 로컬 파일 중
> 하나입니다. 배포된 PoC는 Storage·Key Vault 공개 접근이 구독 정책으로 강제 차단되어
> `database` 저장소와 Container App 인라인 시크릿을 사용합니다. 로컬 워커와 단위
> 테스트는 계속 SQLite와 로컬 파일을 사용합니다.

## MIP 복호화 (Microsoft Purview 정보 보호)

Microsoft Purview(MIP/IRM)로 보호된 문서는 포맷 핸들러가 읽기 전에 복호화해야 합니다. RMS
보호 파일의 콘텐츠 키는 서버 측에 있어 오프라인 복호화가 불가능하므로, CrewMeal은 공식
**MIP File SDK**를 감싼 CLI에 **서브프로세스로 위임**합니다. 복호화는 **워커가 원본을
내려받은 직후·지문 계산 전에** 일어나므로(`PresentationProcessor.decrypt_source`), 지문·포맷
감지·검증·분석은 모두 평문을 대상으로 합니다.

동작 요약:

1. `MipDecryptionProvider.detect`가 MIP/IRM 마커를 감지합니다(일반 Office/PDF에는 없는
   `MicrosoftIRMServices`·`MSIP_Label`·`\x09DRMContent`).
2. `SubprocessMipSdkRunner`가 기존 M365 서비스 주체로 **app-only 토큰**(기본 스코프
   `https://aadrm.com/.default`)을 발급하고, 토큰을 **임시 파일**(argv/프로세스 목록 노출
   금지)로 CLI에 넘겨 다음 계약으로 실행합니다.

   ```
   <cli...> unprotect --in <입력> --out <출력> --token-file <토큰>
   # exit 0 -> 성공(<출력>에 평문 기록), nonzero -> 실패(stderr 사유)
   ```

3. 실패는 `DecryptionUnavailableError`(구성 안 됨)/`DecryptionFailedError`(실행 실패)로
   매핑되어 워커의 알려진 처리 오류로 기록됩니다. **조용한 통과는 없습니다.**

### 무인 인증과 RMS 슈퍼유저 (필수)

app-only(무인) MIP 소비 복호화는 서비스 주체가 **Azure RMS 슈퍼유저**여야 동작합니다.
슈퍼유저는 문서의 권한 정책과 무관하게 테넌트의 보호 콘텐츠를 복호화할 수 있습니다. 대화형
로그인은 사용하지 않습니다. 슈퍼유저 부여 방법은 Microsoft 문서
「[Azure Information Protection 슈퍼유저](https://learn.microsoft.com/azure/information-protection/configure-super-users)」를
참고하세요. 이 권한 없이 MIP를 켜면 복호화가 런타임 오류로 실패합니다.

> 💡 **관리 포털 준비 마법사** — `/admin/settings`의 「MIP 테넌트 준비 마법사」 카드는 자격 증명·
> RMS 토큰·슈퍼유저 롤·어댑터 구성의 준비 상태를 점검하고(「다시 점검」 버튼은 토글을 켜기
> 전에도 실시간 확인), 관리자 동의 URL과 슈퍼유저 부여용 PowerShell(서비스 주체 object id 자동
> 반영)을 그대로 제공합니다. 앱은 어떤 권한도 자체 부여하지 않으며, 명령은 디렉터리 관리자가
> 직접 실행합니다.

### 활성화

1. 관리자 토글 `decryption.mip.enabled`를 켭니다(`/admin/settings`의 「복호화」 카드).
2. 복호화 CLI 경로를 `CREWMEAL_MIP_SDK_CLI`로 지정합니다.
   - **프로덕션**: Microsoft MIP File SDK를 감싼 CLI 경로.
   - **로컬·CI·데모**: 내장 레퍼런스 CLI `python -m crewmeal.search_enhancement.mip_tool`.
3. 서비스 주체(RMS 슈퍼유저) 자격 증명이 워커 환경에 있어야 토큰을 발급합니다.

| 환경 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `CREWMEAL_MIP_SDK_CLI` | (없음) | 복호화 CLI 명령. 비면 MIP는 켜도 「미구성」으로 실패 |
| `CREWMEAL_MIP_RMS_SCOPE` | `https://aadrm.com/.default` | app-only RMS 토큰 스코프 |
| `CREWMEAL_MIP_SDK_TIMEOUT_SECONDS` | `120` | CLI 호출 타임아웃(초) |
| `CREWMEAL_MIP_SDK_LIB_DIR` | (없음) | 네이티브 라이브러리 경로. `PATH`/`LD_LIBRARY_PATH`에 주입 |
| `CREWMEAL_MIP_SDK_SUBCOMMAND` | `unprotect` | CLI 복호화 서브커맨드 이름 |
| `CREWMEAL_MIP_RELEASE_SECRET` | (레퍼런스 CLI 전용) | 레퍼런스 CLI의 키 릴리스 시크릿. **실제 MIP과 무관** |

### 레퍼런스 CLI vs 실제 SDK

Microsoft 네이티브 SDK와 라이브 Azure RMS는 이 저장소·CI에서 실행할 수 없습니다. 그래서
동일한 러너/config seam 뒤에 **실행 가능한 레퍼런스 protect/unprotect CLI**
(`crewmeal.search_enhancement.mip_tool`)를 함께 제공해 로컬·CI·데모에서 end-to-end를 실제로
완결합니다. 레퍼런스 CLI는 탐지 마커 + AES-256-GCM 봉투를 만들고, RMS 슈퍼유저 키 릴리스를
로컬 시크릿으로 시뮬레이션합니다.

> ⚠️ **레퍼런스 CLI는 데모·검증용이며 실제 MIP/RMS 암호화가 아닙니다.** 어떤 보안 보장도
> 제공하지 않습니다. 프로덕션은 반드시 `CREWMEAL_MIP_SDK_CLI`를 실제 Microsoft MIP File SDK
> CLI로 교체하세요(seam·fetch·Docker 배선은 동일).

### 네이티브 SDK 아티팩트 페치 (`scripts/fetch_mip_sdk.py`)

MIP File SDK 네이티브 라이브러리(NuGet)는 git에 커밋하지 않고 **빌드 시** 구성 가능한
URL+버전+체크섬으로 내려받아 검증·추출합니다. 버전·해시는 핀 고정하며, 체크섬 없이는
`--allow-unverified` 없이 실행되지 않습니다.

```powershell
python scripts/fetch_mip_sdk.py --version <버전> --sha256 <체크섬> --dest /opt/mip/lib
```

환경 변수 대체: `CREWMEAL_MIP_SDK_VERSION`·`_URL`·`_SHA256`·`_RUNTIME`·`_LIB_DIR`. Docker는
opt-in 페치 스테이지를 두고 build-args `MIP_SDK_VERSION`·`MIP_SDK_SHA256`·`MIP_SDK_RUNTIME`로
제어하며, 이미지에 `CREWMEAL_MIP_SDK_LIB_DIR=/opt/mip/lib`을 설정합니다. `infra`의
`mipSdkCli`·`mipRmsScope` 파라미터가 워커/웹 컨테이너 환경으로 전달됩니다.

> MIP File SDK 자체 라이선스가 적용됩니다. 바이너리를 저장소에 커밋하지 말고 빌드 시
> 페치·고지하세요.

### end-to-end 실행

`tests/search_enhancement/test_mip_e2e.py`가 전체 파이프라인을 실제로 완결합니다: 샘플
`.pptx` 생성 → 레퍼런스 protect로 암호화 → tryout 업로드처럼 아티팩트 저장·job enqueue →
워커가 다운로드 → 레퍼런스 CLI로 **실제 서브프로세스 복호화** → 평문 지문·처리 → HTML 산출
및 상태 `Ready` 검증. LibreOffice 실렌더 변형은 `soffice` 존재 시에만 수행하고 없으면
skip합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/search_enhancement/test_mip_e2e.py -v
```

## 준비 사항

- Python 3.11 이상
- LibreOffice (`soffice.exe`)
- rhwp `0.7.19` (`RHWP_PATH`; 프로덕션 Docker image에는 고정 커밋으로 포함)
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
$env:RHWP_PATH = "C:\path\to\rhwp.exe"
```

## Azure 배포

프로덕션은 단일 컨테이너 이미지를 **웹 앱**(Ingest API + 상태 페이지 + 관리자 포탈)과
**워커**(LibreOffice + rhwp 문서 파이프라인)로 각각 실행합니다. 요청은 SharePoint 열이 아니라
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

#### 상태 페이지 Entra ID SSO

사용자 상태 페이지 `/s/{token}`은 프로덕션(`from_environment`)에서 기본으로 **Entra ID
로그인(OpenID Connect authorization-code)** 뒤에 놓입니다. opaque 토큰만으로는 열람·조작할
수 없고, **테넌트 사용자 로그인 + 유효 토큰** 두 요소를 모두 충족해야 조회·rerun·comment·
remove가 가능합니다. 로그인은 기존 M365 앱 등록(`CREWMEAL_M365_TENANT_ID/CLIENT_ID/
CLIENT_SECRET`)을 재사용하며(MSAL confidential client), 별도 자격증명이 필요하면
`CREWMEAL_STATUS_SSO_TENANT_ID/CLIENT_ID/CLIENT_SECRET`로 재정의합니다. 문서별 SharePoint
권한은 검증하지 않습니다(로그인 + 토큰 소지가 경계).

```powershell
# 상태 페이지 SSO는 프로덕션 기본 on. 명시적으로 끄려면(비권장):
azd env set CREWMEAL_STATUS_REQUIRE_AUTH false
```

> ⚠️ **수동 사전작업(1회)** — 재사용하는 M365 앱 등록에 **Web 리다이렉트 URI**
> `{SERVICE_WEB_URI}/auth/callback`(예: `https://ca-web-...azurecontainerapps.io/auth/callback`)을
> 추가하고 **ID 토큰 발급**을 허용해야 합니다. 미등록 시 콜백이 `AADSTS50011`로 실패합니다.
> 로컬 개발은 `http://localhost:8000/auth/callback`을 추가합니다. `CREWMEAL_STATUS_REQUIRE_AUTH=true`
> 인데 SSO 자격증명이 없으면 웹 앱이 `ConfigurationError`로 기동에 실패합니다.

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

### HWP/HWPX 파서 벤치마크

HWP5 10개와 HWPX 10개를 CrewMeal, kordoc, rhwp, hwp-hwpx-parser, pyhwp,
openhanji, python-hwpx의 호환 포맷에 동일하게 실행합니다. 코퍼스는 원본 저장소의 커밋과
SHA-256을 고정해 실행 시 내려받으며 제3자 문서 바이너리는 저장소에 커밋하지 않습니다.
프로덕션 결정과 semantic-first 흐름은
[`docs/rhwp-architecture.md`](docs/rhwp-architecture.md)에 기록했습니다.

```powershell
$env:PYTHONPATH = "src"
python -m crewmeal.search_enhancement.hwp_parser_benchmark setup
python -m crewmeal.search_enhancement.hwp_parser_benchmark fetch
python -m crewmeal.search_enhancement.hwp_parser_benchmark run
# 기존 results.json에서 보고서만 다시 생성
python -m crewmeal.search_enhancement.hwp_parser_benchmark report
```

`crewmeal` benchmark adapter는 rhwp 도입 전 LibreOffice 기준선의 실패를 재현하도록
의도적으로 유지합니다. 공식 최신 LibreOffice 필터 목록은 HWP 97만 제공하며 이번
HWP5/HWPX 20개도 열지 못했습니다.
`setup`은 나머지 파서를 고정 버전으로 격리 설치하고, `run`은 최대 110개 호환 조합의 결과를
`result/hwp-parser-benchmark/results.json`, `report.html`, `report.md`에 기록합니다. 텍스트 sentinel과
최소 표·그림·페이지·주석 수는 기대 근거 회수율로 채점하고, 파서 간 token agreement는
정확도가 아닌 교차 일치도로 별도 표시합니다. `pyhwp`는 AGPL-3.0이므로 제품 코드 통합
전 별도 법무 검토가 필요합니다.

벤치마크 20개와 별도로 조사한 6개 파서 저장소의 추적 `.hwp`·`.hwpx`를 전부 보관하려면
다음을 실행합니다. 저장소별 원래 경로, SHA-256 중복 제거본, JSON manifest와 검색 가능한
단일 HTML 보고서를 `result/hwp-sample-archive/`에 생성합니다.

```powershell
$env:PYTHONPATH = "src"
python -m crewmeal.hwp_sample_archive
```

rhwp `native-skia`의 SVG·PDF·PNG 20문서 렌더와 고유 archive 767개 sweep 결과를 다시
검증하거나 HTML 의사결정 보고서만 재생성하려면 다음을 실행합니다.

```powershell
docker build -f benchmark/hwp/rhwp-native.Dockerfile -t crewmeal-rhwp-native:0.7.19 .
$env:PYTHONPATH = "src"
python -m crewmeal.search_enhancement.rhwp_render_validation all
python -m crewmeal.search_enhancement.rhwp_render_validation report
```

결과는 `result/rhwp-render-validation/results.json`과 self-contained
`report.html`에 저장됩니다. 전체 결과 기준 고정 corpus는 20/20, SVG·PDF·PNG 158페이지
일치, archive sweep은 764/767(99.61%)입니다. 실패 3개는 암호화 HWP 1개와 필수 part가
없는 의도적 malformed HWPX 2개입니다.

### 웹 엔드포인트

배포된 웹 앱(`SERVICE_WEB_URI`)이 제공하는 경로입니다.

- `GET /healthz`, `GET /readyz` — liveness / readiness(= DB 연결) 프로브
- `POST /api/requests` — 강화·삭제 요청을 큐에 적재하는 Ingest API. SPFx 명령이 호출하며,
  `CREWMEAL_INGEST_REQUIRE_AUTH=true`일 때 Bearer 토큰을 검증합니다.
- `GET /s/{token}` — 사용자 상태 페이지(진행 타임라인·현재 단계·결과 링크).
  `CREWMEAL_STATUS_REQUIRE_AUTH=true`(프로덕션 기본)일 때 Entra ID 로그인 필요 —
  미인증 네비게이션은 `/auth/login`으로 리다이렉트, `fetch`/POST는 401.
  - `GET /s/{token}/html` — 추출된 강화 HTML 미리보기
  - `GET /s/{token}/progress` — 진행 단계 JSON
  - `POST /s/{token}/rerun` — 원본이 갱신되어 재작업 요청
  - `POST /s/{token}/comment` — 튜닝 코멘트를 프롬프트에 주입해 재작업(피드백 코퍼스 적재)
  - `POST /s/{token}/remove` — 색인에서 제거 요청
- `GET /auth/login` · `GET /auth/callback` · `GET /auth/logout` — 상태 페이지 SSO
  로그인·콜백·로그아웃(MSAL). 콜백 URL을 M365 앱 등록의 Web 리다이렉트 URI로 등록해야 함.
- `GET /admin` — 관리자 포탈(대시보드). `X-Admin-Key` 헤더 또는 로그인 세션으로 게이트하며
  키는 `CREWMEAL_ADMIN_KEY`입니다.
  - `/admin/documents`, `/admin/documents/{token}` — 문서 목록·상세, 문서별 rerun/remove·job retry
  - `/admin/settings` — 런타임 설정 조회·수정
  - `/admin/feedback`, `/admin/feedback/export.jsonl` — 튜닝 코멘트 코퍼스 열람·내보내기
  - `/admin/tryout` — 지원 문서(PPTX·PDF·HWP·HWPX)를 직접 업로드해 파이프라인을 시험하는 플레이그라운드

상태 페이지·관리자의 comment 재작업이 남긴 튜닝 코멘트는 피드백 코퍼스로 축적되며
`/admin/feedback/export.jsonl`로 내보내 본체 분석 프롬프트·엔진 개선에 활용합니다.

## SharePoint 검색강화

전용 사이트에서 선택한 지원 문서만 처리하는 opt-in 워크플로입니다. 원본 파일
바이너리는 수정하지 않으며 생성한 검색 콘텐츠의 게시 위치를 관리자가 선택합니다.

### 게시 대상과 전환

`/admin/settings`의 **검색 콘텐츠 게시 방식** 카드에서 한 가지 대상을 선택합니다.

| 대상 | 동작 |
| --- | --- |
| `unset` | 새 설치의 초기 상태. 관리자가 선택하기 전에는 SharePoint 명령과 게시를 보류 |
| `sharepoint_column` | 원본 list item의 `CrewmealSearchContent` 일반 텍스트 컬럼에 Markdown으로 저장 |
| `copilot_connector` | 기존 Graph Connector의 `externalItem`에 별도 색인 |

컬럼 방식은 SharePoint 원본 항목의 ACL과 메타데이터를 그대로 사용하므로 권한을
복제하지 않습니다. `CrewmealSearchContent`는 검색 속성 이름을 안정적으로 유지하기
위한 고정 내부 이름이고 표시 이름만 바꿀 수 있습니다. 저장 한도는 보수적으로
**63,999 UTF-16 code units**이며, 초과 Markdown은 페이지·semantic block 단위로만
줄이고 생략 안내를 보존합니다. 게시 직후 필드를 다시 읽어 실제 저장 글자수·byte
수·해시를 기록합니다.

대상 변경은 DB의 generation과 문서별 publication 상태로 재시작 가능한 전환을
수행합니다. 새 대상에 모든 문서를 먼저 게시하고 검증이 끝날 때까지 이전 대상을
유지한 뒤, 이전 `externalItem` 또는 컬럼 값만 정리합니다. 전환 도중 기존 대상으로
되돌리면 이미 staging된 반대 대상도 정리합니다. 컬럼 정의 자체는 재사용을 위해
남깁니다.

컬럼 생성은 검색 준비 완료를 뜻하지 않습니다. `indexed`는 list 조회/정렬 인덱스일
뿐 SharePoint Search crawl의 증거가 아닙니다.

1. 사이트 컬럼을 라이브러리에 연결하고 첫 값을 게시합니다.
2. **Library settings > Advanced settings > Reindex Document Library**에서 관리자가
   수동 재인덱싱합니다. 지원되는 Graph/REST 재인덱싱 API는 사용하지 않습니다.
3. 생성 Markdown에만 있는 고유 canary로 Microsoft Search가 원본 문서를 반환하는지
   확인하고 관리자 포털에 canary와 원본 URL을 기록합니다.
4. 같은 문서 또는 라이브러리에 범위를 둔 SharePoint/Microsoft 365 Copilot이
   canary를 메타데이터 근거로 답하는지 확인합니다.

범위를 지정하지 않은 Copilot 질의에서 컬럼 방식이 Connector와 동일하게 동작한다고
보장하지 않습니다. 실테넌트 A/B 시험에서는 동일 원본 복제본에 같은 사실을 HTML과
Markdown으로 각각 저장했습니다. 고유 canary는 두 포맷 모두 검색됐고 공통 질의의
순위 우위는 일관되지 않았으며, 선택 문서 범위 Copilot은 두 포맷 모두 5개 사실을
5/5로 답했습니다. Markdown은 원본 기준 37% 작았고 SharePoint 정제 후에도 제목·목록·
표 구분과 228자 원문을 그대로 보존해 컬럼 기본 포맷으로 채택했습니다.

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
- 검색 콘텐츠 site column 내부 이름: `CrewmealSearchContent`

워커는 지원 문서를 임시 폴더에만 다운로드합니다. PPTX·PDF는 144 DPI 페이지 이미지와
원문 근거를 구성된 이미지 분석 모델에 전달하고, HWP·HWPX는 rhwp semantic 근거를
우선 사용해 필요한 페이지만 Vision으로 보강합니다. 모델의 strict JSON Schema 응답과
semantic 결과는 코드가 허용 태그 HTML로 렌더링합니다. 발표자 노트는 PPTX에서 별도
섹션으로 추가하며 원본 문서, PDF, PNG, HTML 본문과 비밀 값은 SQLite에 저장하지 않습니다.
DB에는 대상 locator, 실제 저장 크기, hash, 절단 여부와 전환 상태만 기록합니다.

### Microsoft 365 설정

런타임 앱은 사이트별 최소 권한을 유지합니다.

- Microsoft Graph `Sites.Selected`
- SharePoint `Sites.Selected`
- Connector 방식 사용 시 Microsoft Graph
  `ExternalConnection.ReadWrite.OwnedBy`,
  `ExternalItem.ReadWrite.OwnedBy`

사이트 관리자 계정으로 한 번만 대상 사이트 `write` 권한을 부여합니다.

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

site column 생성·라이브러리 연결 동안에만 대상 사이트의 app role을
`fullcontrol`로 올리고, 성공 또는 실패와 관계없이 `finally`에서 런타임 최소
권한인 `write`로 되돌립니다. Graph는 기존 site column의
list 연결을 일관되게 처리하지 않으므로, short-lived delegated SharePoint token으로
같은 GUID의 list field를 연결한 뒤 `RichText=false`, `AppendOnly=false`를 재조회합니다.
SharePoint Online은 client-secret app-only 토큰으로 REST 호출을 허용하지 않으므로
content column 연결과 상태 열 formatter는 사이트 소유자의 인증된 SharePoint
세션에서 수행합니다. 인증서 기반 또는 delegated SharePoint 토큰을 사용하는 경우
해당 토큰을
`CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN`에 임시로 넣고
`configure_test_library.py --apply-content-column --apply-formatting`을 실행합니다.

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
없이 Connector 방식에서만 externalItem ACL을 갱신합니다. 컬럼 방식은 원본 ACL을
그대로 따르고 read-back content hash가 달라졌을 때만 다시 게시합니다. SharePoint
사용자 정의 열 갱신이 패키지 문서 속성을 다시 써 eTag, cTag, quickXorHash까지
바꿀 수 있으므로 이 값들은 슬라이드 콘텐츠 변경 판단에 사용하지 않습니다.

Connector 방식의 `externalItem.id`는 SharePoint `listItemUniqueId`의 32자리
GUID hex입니다.
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
구조화 HTML 렌더링, 컬럼 63,999자/UTF-16 계약, target 전환·롤백,
SharePoint field read-back과 `externalItem` 계약을 포함합니다.

## 정리

리소스가 더 필요하지 않으면 비용이 발생하지 않도록 삭제합니다.

```powershell
azd down --purge --force
```

다중 사이트, 첨부파일 본문, 대용량 산출물용 private endpoint 저장 경로는 아직
포함하지 않습니다. 산출물은 현재 PostgreSQL 저장소를 쓰며, 대용량·고빈도 환경에서는
Container Apps 환경에 private endpoint로 연결한 Blob으로 전환하는 것을 권장합니다.
