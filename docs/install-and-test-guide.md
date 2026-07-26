# CrewMeal 설치 · 구성 · 테스트 가이드 (고객사 PoC용)

이 문서는 **CrewMeal을 처음 접하는 고객사 담당자**가 순서대로 따라 하기만 하면
설치·구성·테스트를 끝낼 수 있도록 만든 단계별 안내서입니다. 모든 명령은
**Windows PowerShell** 기준이며, 복사해서 그대로 붙여 넣을 수 있습니다.

> 제품 개요·아키텍처는 [README](../README.md)와
> [소개 사이트](https://microsoft.github.io/CrewMeal/)를 참고하세요.
>
> 💻 **웹(HTML) 버전**: <https://microsoft.github.io/CrewMeal/install-and-test-guide.html>
> — 명령어 복사 버튼, 단계 진행 체크, 트랙별 보기, PDF 저장을 지원합니다.
> `docs/install-and-test-guide.html` 파일 하나만 내려받으면 인터넷 없이도 열립니다.

**목차**

| 장 | 내용 | 누가 보나 |
| --- | --- | --- |
| [0. 트랙 고르기](#0-먼저-트랙을-고르세요) | 어디까지 테스트할지 결정 | 모두 |
| [1. 사전 준비 사항](#1-사전-준비-사항) | 계정·권한·소프트웨어·문서 준비 | 모두 |
| [2. 트랙 A — 내 PC에서 30분 체험](#2-트랙-a--내-pc에서-30분-체험) | 설치 없이 결과 먼저 보기 | 담당 실무자 |
| [3. 트랙 B — Azure 배포](#3-트랙-b--고객사-azure-구독에-배포) | 고품질 AI 분석 켜기 | 클라우드 담당자 |
| [4. 트랙 C — SharePoint · Copilot 연동](#4-트랙-c--sharepoint--copilot-연동) | 실사용 흐름 완성 | M365 관리자 |
| [5. 테스트 시나리오](#5-테스트-시나리오와-합격-기준) | 검증 체크리스트 | 검증 담당자 |
| [6. 환경 변수](#6-환경-변수-총정리) | 설정값 사전 | 운영자 |
| [7. 문제 해결](#7-문제-해결) | 오류별 대처 | 모두 |
| [8. 비용·정리](#8-비용-관리와-정리) | 요금과 삭제 | 관리자 |
| [9. 보안·데이터](#9-보안데이터-취급) | 보안 검토 답변 | 보안 담당자 |
| [10. 도움 요청](#10-도움-요청-시-함께-보내주세요) | 문의 시 보낼 정보 | 모두 |

---

## 0. 먼저 트랙을 고르세요

CrewMeal 테스트는 세 단계로 나뉩니다. **A → B → C 순서**로 진행하는 것을 권장합니다.
트랙 A는 혼자서 바로 할 수 있고, 트랙 C는 **트랙 B의 배포 주소가 있어야** 진행할 수 있습니다.

| 트랙 | 무엇을 확인하나 | 필요한 것 | 걸리는 시간 |
| --- | --- | --- | --- |
| **A. 로컬 체험** | 문서에서 검색용 콘텐츠가 실제로 만들어지는지 | PC 1대 + Python | 약 30분 |
| **B. Azure 배포** | 고품질(AI 비전) 분석과 서버 운영 | Azure 구독(소유자) | 반나절 |
| **C. M365 연동** | SharePoint 버튼 → Copilot 검색까지 | M365 테넌트 관리자 | 1일 |

**"우선 결과물만 빨리 보고 싶다" → 트랙 A만 하세요.** Azure 구독도, Microsoft 365
계정도, 관리자 승인도 필요 없습니다.

> ⚠️ **PoC 공통 주의**: 이 구성은 개념검증(PoC)용으로 공개 엔드포인트를 사용합니다.
> **실제 기밀 문서를 넣지 마세요.** 테스트에는 비민감 문서만 사용합니다.

---

## 1. 사전 준비 사항

시작하기 전에 **미리 준비해 두어야 할 것**을 한곳에 모았습니다. 고른 트랙에
해당하는 것만 준비하면 됩니다. 표에서 `—` 는 필요 없다는 뜻입니다.

> ✅ **트랙 A만 해 본다면 준비물은 이게 전부입니다.**
> PC 1대 · Python · Git · 테스트용 문서 몇 개. Azure 구독도, Microsoft 365
> 계정도, 결제 수단도, 관리자 승인도 필요 없습니다.

### 1-1. 한눈에 보는 준비물

| 준비물 | A | B | C | 비고 |
| --- | --- | --- | --- | --- |
| 작업용 Windows PC (PowerShell) | ✅ | ✅ | ✅ | 1대 |
| Python 3.11 이상 | ✅ | ✅ | ✅ | 3.12 권장 |
| Git | ✅ | ✅ | ✅ | 소스 내려받기 |
| 비민감 테스트 문서 | ✅ | ✅ | ✅ | 5~10개 |
| Azure 구독 (소유자) | — | ✅ | ✅ | 결제 수단 등록됨 |
| Azure CLI · Azure Developer CLI | — | ✅ | ✅ | 배포 도구 |
| Entra 앱 등록 + 관리자 동의 권한 | — | ✅ | ✅ | 앱 1개 |
| eastus2 모델 쿼터 1,150K TPM | — | ✅ | ✅ | 부족하면 배포 실패 |
| Microsoft 365 테넌트 | — | — | ✅ | 테스트 사이트 생성 가능 |
| SharePoint 앱 카탈로그 | — | — | ✅ | 없으면 먼저 생성 |
| Node.js 22.14 이상 23 미만 | — | — | ✅ | SPFx 빌드용 |
| Microsoft 365 Copilot 라이선스 | — | — | ✅ | 마지막 검증 1건에만 |

> 💡 **Docker와 LibreOffice는 설치하지 않아도 됩니다.** 컨테이너 이미지는
> Azure에서 원격 빌드(`remoteBuild`)되므로 로컬 Docker가 필요 없고, PPTX 변환용
> LibreOffice와 한글 문서용 rhwp는 서버 이미지에 이미 포함되어 있습니다.
> 트랙 A는 AI를 쓰지 않는 저품질(텍스트+OCR) 티어로 동작해 둘 다 필요 없습니다.

### 1-2. 누가 참여해야 하나요

한 사람이 모든 권한을 갖고 있다면 혼자서도 가능합니다. 보통은 세 역할이 필요합니다.

| 역할 | 필요한 권한 | 하는 일 | 트랙 |
| --- | --- | --- | --- |
| 테스트 실무 담당자 | 본인 PC에 프로그램 설치 | 문서 준비, 실행, 결과 평가 | A |
| Azure 담당자 | 구독 소유자(Owner) | 쿼터 확인, `azd up` 배포 | B |
| Microsoft 365 관리자 | 테넌트 관리자 · 사이트 소유자 | 앱 동의, 앱 설치, API 승인 | C |

### 1-3. 설치할 소프트웨어

| 프로그램 | 필요 버전 | 필요한 트랙 | 확인 명령 |
| --- | --- | --- | --- |
| Python | 3.11 이상 (3.12 권장) | A · B · C | `python --version` |
| Git | 최신 | A · B · C | `git --version` |
| Azure CLI | 2.60 이상 | B · C | `az version` |
| Azure Developer CLI | 1.28.0 이상 | B · C | `azd version` |
| Node.js | 22.14 이상 23 미만 | C | `node --version` |
| Microsoft.Graph.Authentication | 최신 | C | PowerShell 모듈 |

설치는 winget으로 한 번에 할 수 있습니다. 필요한 줄만 실행하세요.

```powershell
# 모든 트랙 공통
winget install Python.Python.3.12
winget install Git.Git

# 트랙 B 이상
winget install Microsoft.AzureCLI
winget install Microsoft.Azd
```

> ⚠️ Python 설치 화면에서 **"Add python.exe to PATH"** 체크를 꼭 켜세요.
> SPFx 빌드는 **Node.js 22.x**만 허용하므로 최신 LTS가 23 이상이면 빌드가
> 거부됩니다. [nodejs.org](https://nodejs.org/en/download)에서 22.x를 직접 받거나
> `nvm-windows`로 버전을 전환해 사용하세요.

설치가 끝났으면 아래로 한 번에 확인합니다.

```powershell
python --version
git --version
az version --output tsv 2>$null
azd version
node --version
```

### 1-4. Azure 쪽 준비 (트랙 B · C)

| 항목 | 준비 내용 |
| --- | --- |
| 구독 권한 | **소유자(Owner)** 또는 리소스 생성 + `Microsoft.Authorization/roleAssignments/write` |
| 결제 | 결제 수단이 등록된 유료 구독 (무료 평가판은 쿼터가 부족합니다) |
| 리전 | **eastus2** — 템플릿에 고정. PostgreSQL만 `centralus`에 생성됩니다 |
| 모델 쿼터 | eastus2 GlobalStandard 기준 **1,150K TPM** 여유 |
| Azure Policy | 고객사 정책이 특정 리전·SKU·공용 엔드포인트를 막고 있지 않은지 확인 |
| Entra 앱 | 앱 등록 1개와 **관리자 동의**를 부여할 수 있는 권한 |

> 💡 쿼터가 모자라면 배포가 중간에 실패합니다. 확인 명령과 대처법은
> [B-1 단계](#3-트랙-b--고객사-azure-구독에-배포)에 있습니다. 증설 승인에 며칠
> 걸릴 수 있으니 부족하면 미리 신청하세요.

### 1-5. Microsoft 365 쪽 준비 (트랙 C)

| 항목 | 준비 내용 |
| --- | --- |
| 테넌트 관리자 | Entra 앱에 **관리자 동의**를 부여할 수 있는 계정 |
| 테스트 사이트 | SharePoint 팀 사이트 1개(예: `crewmeal-test`)와 문서 라이브러리 |
| 사이트 소유자 | 열 추가·앱 설치 권한 (앱 권한 부여 스크립트도 이 계정으로 실행) |
| 앱 카탈로그 | 테넌트 앱 카탈로그가 있어야 SPFx 패키지를 올릴 수 있습니다 |
| API 액세스 승인 | SharePoint 관리 센터 → 고급 → **API 액세스** 승인 권한 |
| Copilot 라이선스 | 마지막 검증(범위 지정 Copilot 질의)에 필요 |

> 💡 게시 방식으로 **Copilot 커넥터**를 고르면 Microsoft 365 관리 센터의
> **검색 및 인텔리전스** 설정 권한이 추가로 필요합니다. 처음 테스트라면 권한이
> 단순한 **SharePoint 컬럼** 방식을 권장합니다.

### 1-6. 테스트용 문서 준비

아래 종류를 섞어 **5~10개** 준비하면 테스트 시나리오를 그대로 수행할 수 있습니다.

| 문서 종류 | 확인하는 것 | 트랙 |
| --- | --- | --- |
| 텍스트 위주 PPTX | 원문 문장이 보존되는지 | A |
| 표가 들어간 PPTX | 행·열 값이 누락 없이 재현되는지 | A |
| 막대·선 차트 PPTX | 그래프의 의미를 문장으로 설명하는지 | B |
| 간트차트 · 일정표 PPTX | 작업명·기간·순서를 설명하는지 | B |
| 스캔이 아닌 PDF | 페이지별 내용이 추출되는지 | B |
| HWP · HWPX 한글 문서 | 본문·표·머리말이 추출되는지 | B |
| 50장 이상 대용량 덱 | 실패 없이 완료되는지, 소요 시간 | B |

> ⚠️ **실제 기밀 문서는 넣지 마세요.** 이 구성은 PoC용으로 공개 엔드포인트를
> 사용합니다. 유출되어도 문제가 없는 문서만 사용하세요.

### 1-7. 네트워크 · 방화벽

사내망에서 아래 주소가 막혀 있으면 설치나 배포가 중간에 멈춥니다. 폐쇄망이라면
미리 방화벽 예외를 신청해 두세요.

| 접속 대상 | 용도 | 트랙 |
| --- | --- | --- |
| `github.com` | 소스 코드 내려받기 | A · B · C |
| `pypi.org`, `files.pythonhosted.org` | 파이썬 패키지 설치 | A · B · C |
| `127.0.0.1:8000` | 트랙 A 로컬 웹 서버 (외부 통신 아님) | A |
| `login.microsoftonline.com` | Azure · Microsoft 365 로그인 | B · C |
| `management.azure.com`, `portal.azure.com` | Azure 배포와 관리 | B · C |
| `graph.microsoft.com` | SharePoint 정보 조회 · 게시 | B · C |
| `*.azurecontainerapps.io` | 배포된 웹 앱과 상태 페이지 | B · C |
| `*.sharepoint.com` | 테스트 사이트와 앱 카탈로그 | C |
| `registry.npmjs.org` | SPFx 패키지 빌드 | C |

### 1-8. 시작 전 최종 점검

- [ ] 어느 트랙까지 진행할지 정했다 (A / B / C)
- [ ] Python과 Git이 설치되어 버전이 확인된다
- [ ] 비민감 테스트 문서를 5~10개 모았다
- [ ] (B · C) Azure 구독 소유자 권한과 eastus2 쿼터를 확인했다
- [ ] (C) M365 테넌트 관리자와 테스트 사이트가 준비됐다
- [ ] (C) Node.js 22.x와 앱 카탈로그를 확인했다

---

## 2. 트랙 A — 내 PC에서 30분 체험

Azure도 Microsoft 365도 없이, **PC 한 대에서** 문서를 넣고 결과를 확인합니다.
이 트랙은 AI 비전 모델을 쓰지 않는 **저품질(텍스트+OCR) 티어**로 동작하므로
비용이 전혀 들지 않고 LibreOffice 설치도 필요 없습니다.

### A-1. 준비물 확인

| 항목 | 필요 버전 | 확인 명령 |
| --- | --- | --- |
| Python | 3.11 이상 (3.12 권장) | `python --version` |
| Git | 아무 최신 버전 | `git --version` |

없다면 [python.org](https://www.python.org/downloads/)와
[git-scm.com](https://git-scm.com/downloads)에서 설치하세요. Python 설치 시
**"Add python.exe to PATH"** 체크를 꼭 켜세요.

### A-2. 소스 코드 받기

```powershell
cd $HOME
git clone https://github.com/microsoft/CrewMeal.git
cd CrewMeal
```

✅ **확인**: `dir` 을 쳤을 때 `README.md`, `src`, `scripts` 폴더가 보이면 성공.

### A-3. 파이썬 환경 만들기

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

마지막 줄에 `Successfully installed ... crewmeal-0.1.0 ...` 이 나오면 성공입니다
(2~5분 걸립니다).

✅ **확인**: 아래 명령이 `366 passed` 같은 결과로 끝나면 설치가 정상입니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

### A-4. 웹 서버 실행 (터미널 1번)

**PowerShell 창을 하나 열고** 아래를 통째로 붙여 넣습니다.

```powershell
cd $HOME\CrewMeal
$env:CREWMEAL_ADMIN_KEY         = "local-admin-key"
$env:CREWMEAL_WEB_SESSION_SECRET = "local-session-secret"
$env:CREWMEAL_INGEST_REQUIRE_AUTH = "false"
$env:CREWMEAL_STATUS_REQUIRE_AUTH = "false"
.\.venv\Scripts\python.exe -m uvicorn crewmeal.search_enhancement.web:create_app_from_env `
  --factory --host 127.0.0.1 --port 8000
```

`Uvicorn running on http://127.0.0.1:8000` 이 보이면 실행 중입니다.
**이 창은 끄지 말고 그대로 두세요.**

✅ **확인**: 브라우저에서 <http://127.0.0.1:8000/healthz> 를 열면
`{"status":"ok"}` 가 보입니다.

### A-5. 문서 올리기

1. 브라우저에서 <http://127.0.0.1:8000/admin> 접속
2. 관리자 키에 `local-admin-key` 입력 후 로그인
3. 상단 메뉴에서 **시연 업로드**(`/admin/tryout`) 클릭
4. 테스트용 `.pptx` 파일을 선택하고 업로드

업로드하면 자동으로 **상태 페이지(`/s/...`)** 로 이동합니다. 이때 상태는
`대기(Queued)` 입니다 — 아직 처리할 워커를 켜지 않았기 때문입니다.

> 💡 손에 든 PPT가 없다면 PowerPoint에서 슬라이드 2~3장짜리 아무 파일이나
> 만들어 쓰세요. 표·차트가 있으면 결과가 더 잘 보입니다.

### A-6. 워커 실행해서 처리하기 (터미널 2번)

**새 PowerShell 창을 하나 더 열고** 아래를 통째로 붙여 넣습니다.
아래 M365 값은 **의미 없는 더미 값**입니다. 게시 대상이 아직 선택되지 않은
상태(`unset`)이므로 워커는 SharePoint에 접속하지 않습니다.

```powershell
cd $HOME\CrewMeal
$env:CREWMEAL_M365_TENANT_ID     = "00000000-0000-0000-0000-000000000000"
$env:CREWMEAL_M365_CLIENT_ID     = "00000000-0000-0000-0000-000000000000"
$env:CREWMEAL_M365_CLIENT_SECRET = "local-dummy"
$env:CREWMEAL_M365_SITE_ID       = "local-dummy-site"
$env:CREWMEAL_M365_DRIVE_ID      = "local-dummy-drive"
$env:CREWMEAL_M365_LIST_ID       = "local-dummy-list"
$env:CREWMEAL_M365_SITE_URL      = "https://localhost/sites/local"

# 비용 0원 · AI 호출 없음 (저품질 텍스트 티어)
$env:PPTX_ANALYSIS_TIER = "text_ocr"
$env:PPTX_OCR_ENABLED   = "false"

.\.venv\Scripts\python.exe -m crewmeal.search_enhancement.cli once --verbose
```

마지막에 `Processed 0 command(s) and 1 job(s).` 가 보이면 문서 1건을
처리한 것입니다.

### A-7. 결과 확인

A-5에서 열렸던 상태 페이지로 돌아갑니다. 이 페이지는 3초마다 스스로 갱신되므로
그냥 두고 보면 됩니다(수동 새로고침도 가능).

| 보이는 것 | 의미 |
| --- | --- |
| **완료** 배지 | 처리 성공 |
| 「진행 상황」 타임라인 8단계 | 작업 시작 → 원본 다운로드 → 파일 검증 → 문서 변환·추출 → 페이지 렌더링 → 콘텐츠 분석 → HTML 생성 → 완료 |
| 「추출된 HTML 미리보기」 | 실제로 만들어진 검색용 콘텐츠 (**새 창에서 열기 ↗** 로 크게 보기) |

✅ **합격 기준**: 추출된 HTML 안에 원본 슬라이드의 **제목·문장·표 내용이
그대로 들어 있으면** 정상입니다.

### A-8. 트랙 A 정리

- 터미널 1(웹)에서 `Ctrl + C` 를 눌러 서버를 끕니다.
- 로컬 데이터는 `.crewmeal\search-enhancement.db` 파일 하나에만 저장됩니다.
  지우고 싶으면 폴더째 삭제하세요: `Remove-Item -Recurse -Force .crewmeal`

> 📌 **트랙 A의 한계**: 저품질 티어는 AI 비전 모델을 쓰지 않으므로 그림·차트·
> 다이어그램의 **의미 설명은 생성되지 않습니다.** 간트차트 해석, 도표 요약 등
> CrewMeal의 핵심 가치를 보려면 트랙 B로 넘어가세요.

---

## 3. 트랙 B — 고객사 Azure 구독에 배포

여기서부터 **고품질(AI 비전) 분석**이 켜집니다. 웹 앱과 워커가 Azure Container
Apps에서 24시간 돌아갑니다.

### B-1. 준비물 확인

| 항목 | 요구 사항 |
| --- | --- |
| Azure 구독 | **소유자(Owner)** 또는 리소스 생성 + `Microsoft.Authorization/roleAssignments/write` 권한 |
| Azure CLI | 2.60 이상 — `az version` |
| Azure Developer CLI | **1.28.0 이상** — `azd version` |
| 리전 | **eastus2** (템플릿에 고정되어 있음, 아래 참고) |
| 모델 쿼터 | eastus2 GlobalStandard 기준 **1,150K TPM** 여유 |

설치 명령:

```powershell
winget install Microsoft.AzureCLI
winget install Microsoft.Azd
```

> ⚠️ **리전 고정**: `infra/main.bicep`의 `location` 파라미터는 현재
> `@allowed(['eastus2'])` 로 제한되어 있습니다. 다른 리전에 배포하려면 이 목록에
> 원하는 리전을 추가해야 하고, 해당 리전에 GPT-5.6 Luna 모델이 제공되는지 먼저
> 확인해야 합니다. PostgreSQL은 별도로 `centralus` 에 만들어집니다
> (`postgresLocation` 파라미터로 변경 가능).

**쿼터 확인** — 부족하면 `azd up` 이 실패합니다.

```powershell
az login
az account set --subscription "<고객사 구독 ID>"
az cognitiveservices usage list --location eastus2 `
  --query "[?contains(name.value,'GlobalStandard')].{name:name.value,used:currentValue,limit:limit}" -o table
```

배포되는 모델 4종과 기본 용량:

| 배포 이름 | 모델 | 용량(TPM) | 용도 |
| --- | --- | --- | --- |
| `gpt-5-6-luna-test` | gpt-5.6-luna | 500K | **운영 기본** 슬라이드 분석 |
| `gpt-5-2` | gpt-5.2 | 500K | 폴백 |
| `gpt-5-mini` | gpt-5-mini | 100K | 비교용 |
| `text-embedding-3-large` | 임베딩 | 50K | 예비 |

쿼터가 모자라면 `infra/modules/foundry.bicep`의 `gptCapacity`,
`lunaCapacity`, `embeddingCapacity` 값을 낮추세요 (예: 각각 100, 100, 10).

### B-2. Microsoft 365 앱 등록 만들기

Azure 배포에는 M365 앱의 **테넌트 ID / 클라이언트 ID / 시크릿**이 필요합니다.
SharePoint 연동을 아직 안 하더라도 값 자체는 있어야 합니다.

1. [Entra 관리 센터](https://entra.microsoft.com) → **앱 등록** → **새 등록**
2. 이름: `crewmeal-poc`, 지원되는 계정 유형: **이 조직 디렉터리의 계정만**
3. 만들고 나서 **개요** 화면에서 다음 두 값을 메모합니다.
   - 애플리케이션(클라이언트) ID → `CREWMEAL_M365_CLIENT_ID`
   - 디렉터리(테넌트) ID → `CREWMEAL_M365_TENANT_ID`
4. **인증서 및 암호** → **새 클라이언트 암호** → 값 복사
   → `CREWMEAL_M365_CLIENT_SECRET` (이 화면을 벗어나면 다시 볼 수 없습니다)
5. **API 권한** → **권한 추가** → **Microsoft Graph** → **애플리케이션 권한**
   에서 다음을 추가합니다.

   | 권한 | 필수 여부 |
   | --- | --- |
   | `Sites.Selected` | ✅ 필수 |
   | `ExternalConnection.ReadWrite.OwnedBy` | Copilot 커넥터 방식일 때만 |
   | `ExternalItem.ReadWrite.OwnedBy` | Copilot 커넥터 방식일 때만 |

6. 같은 화면에서 **SharePoint** → **애플리케이션 권한** → `Sites.Selected` 도 추가
7. **관리자 동의 부여** 버튼을 눌러 동의 처리 (상태가 모두 초록색 체크가 되어야 함)

> 🔐 `Sites.Selected`는 "관리자가 지정한 사이트에만" 접근하는 최소 권한입니다.
> 테넌트 전체 문서에 접근하지 않습니다.

### B-3. 배포 값 설정

SharePoint 사이트를 아직 안 만들었다면 site/drive/list 값은 임시로
`pending` 을 넣고, 트랙 C에서 실제 값으로 바꾸면 됩니다.

```powershell
cd $HOME\CrewMeal
azd auth login
azd env new poc
azd env set AZURE_SUBSCRIPTION_ID "<고객사 구독 ID>"
azd env set AZURE_LOCATION        "eastus2"

# 새로 만들 비밀 값 — 직접 정하세요
#   ※ POSTGRES_ADMIN_PASSWORD 는 DB 접속 문자열에 들어가므로
#     @ : / ? # 같은 문자를 쓰지 말고 영문·숫자·하이픈만 사용하세요.
azd env set POSTGRES_ADMIN_PASSWORD     "Crewmeal-Poc-2026-Strong"
azd env set CREWMEAL_ADMIN_KEY          "<관리자 포털 접속 키>"
azd env set CREWMEAL_WEB_SESSION_SECRET "<임의의 긴 문자열>"

# B-2에서 만든 앱 값
azd env set CREWMEAL_M365_TENANT_ID     "<테넌트 ID>"
azd env set CREWMEAL_M365_CLIENT_ID     "<클라이언트 ID>"
azd env set CREWMEAL_M365_CLIENT_SECRET "<클라이언트 시크릿>"

# SharePoint 값 (트랙 C에서 확정 — 지금은 자리표시자)
azd env set CREWMEAL_M365_SITE_ID       "pending"
azd env set CREWMEAL_M365_DRIVE_ID      "pending"
azd env set CREWMEAL_M365_LIST_ID       "pending"
azd env set CREWMEAL_M365_SITE_URL      "pending"
azd env set CREWMEAL_M365_CONNECTION_ID "crewmealpoc"

# 초기 PoC는 Ingest 인증 없이 시작 (트랙 C에서 켭니다)
azd env set CREWMEAL_INGEST_REQUIRE_AUTH "false"
azd env set CREWMEAL_INGEST_AUDIENCE     "api://crewmeal-ingest"
```

### B-4. 배포 실행

```powershell
azd up
```

- 인프라 생성 → 컨테이너 이미지 빌드 → 웹/워커 배포까지 자동으로 진행됩니다.
- 이미지는 **Azure Container Registry에서 원격 빌드**(`remoteBuild: true`)되므로
  **PC에 Docker를 설치할 필요가 없습니다.**
- 처음 배포는 15~25분 걸립니다.

만들어지는 주요 리소스:

| 리소스 | 용도 |
| --- | --- |
| Microsoft Foundry (AIServices S0) | 슬라이드 분석 모델 4종 |
| Container Apps 환경 + 웹/워커 앱 | 서비스 실행 |
| Container Registry (Standard) | 컨테이너 이미지 |
| PostgreSQL Flexible Server 16 | 상태·큐·산출물 저장 |
| Storage / Key Vault | (정책 허용 시 사용) |
| Log Analytics | 로그 |
| 사용자 할당 관리 ID | 키 없는 인증 |

✅ **확인**: 완료 메시지의 `SERVICE_WEB_URI` 를 복사해 둡니다.

```powershell
azd env get-values | Select-String "SERVICE_WEB_URI"
```

### B-5. 배포 직후 1회 수동 작업 (필수)

상태 페이지 로그인(Entra SSO)이 동작하려면 앱 등록에 **리디렉션 URI**를
추가해야 합니다. 하지 않으면 로그인 시 `AADSTS50011` 오류가 납니다.

1. Entra 관리 센터 → **앱 등록** → `crewmeal-poc` → **인증**
2. **플랫폼 추가** → **웹**
3. 리디렉션 URI에 아래를 입력 (B-4에서 복사한 주소 사용)
   ```
   https://<SERVICE_WEB_URI 호스트>/auth/callback
   ```
4. **암시적 허용 및 하이브리드 흐름** 에서 **ID 토큰** 체크 → 저장

### B-6. 배포 검증

```powershell
$web = (azd env get-values | Select-String "SERVICE_WEB_URI").ToString().Split('=')[1].Trim('"')
Invoke-WebRequest "$web/healthz" -UseBasicParsing | Select-Object -ExpandProperty Content
Invoke-WebRequest "$web/readyz"  -UseBasicParsing | Select-Object -ExpandProperty Content
```

둘 다 `{"status":"ok"}` / `{"status":"ready"}` 가 나와야 합니다.
(`readyz` 는 데이터베이스 연결까지 확인합니다.)

**고품질 분석 실제 확인**:

1. 브라우저에서 `{SERVICE_WEB_URI}/admin` 접속 → `CREWMEAL_ADMIN_KEY` 로 로그인
2. **설정** 화면에서 「이미지 분석 모델」 카드가 `gpt-5.6-luna` /
   `gpt-5-6-luna-test` 로 표시되는지 확인
3. **시연 업로드**에서 그림·차트가 있는 PPT를 업로드
4. 상태 페이지에서 **완료** 까지 진행되는지 확인
5. 추출된 HTML에 **그림/차트에 대한 설명 문장**이 들어 있으면 성공

✅ **트랙 B 합격 기준**: 차트가 들어간 슬라이드에서 "무엇을 나타내는 그래프인지"
문장으로 설명이 생성됨.

---

## 4. 트랙 C — SharePoint · Copilot 연동

사용자가 SharePoint 문서 라이브러리에서 **파일을 선택하고 버튼 한 번**으로
검색강화를 요청하고, 결과가 Microsoft Search / Copilot에 반영되는 전체 흐름입니다.

### C-1. 테스트 사이트와 라이브러리 준비

1. SharePoint에서 **팀 사이트**를 새로 만듭니다 (예: `crewmeal-test`).
2. 기본 **문서(Documents)** 라이브러리를 사용합니다.
3. 비민감 테스트 문서(PPTX/PDF) 5~10개를 업로드합니다.
4. 라이브러리에 **하이퍼링크 열**을 하나 수동으로 추가합니다.
   - 열 추가 → 종류 **하이퍼링크** → 이름 `CrewmealSearchStatusLink`
   - (SPFx 명령이 상태 페이지 링크를 여기에 기록합니다. 자동 생성되지 않습니다.)

### C-2. 사이트/드라이브/목록 ID 조회

```powershell
Install-Module Microsoft.Graph.Authentication -Scope CurrentUser   # 최초 1회
Import-Module Microsoft.Graph.Authentication
Connect-MgGraph -Scopes "Sites.Read.All" -UseDeviceCode -NoWelcome

$hostName = "contoso.sharepoint.com"     # 고객사 도메인으로 변경
$sitePath = "crewmeal-test"              # 사이트 경로로 변경

$site = Invoke-MgGraphRequest -Method GET `
  -Uri "https://graph.microsoft.com/v1.0/sites/${hostName}:/sites/${sitePath}"
$drives = Invoke-MgGraphRequest -Method GET `
  -Uri "https://graph.microsoft.com/v1.0/sites/$($site.id)/drives"
$drive = $drives.value | Where-Object { $_.name -in @("Documents", "문서") } | Select-Object -First 1
$list = Invoke-MgGraphRequest -Method GET `
  -Uri "https://graph.microsoft.com/v1.0/sites/$($site.id)/drives/$($drive.id)/list"

[pscustomobject]@{
  SITE_ID  = $site.id
  DRIVE_ID = $drive.id
  LIST_ID  = $list.id
  SITE_URL = $site.webUrl
} | Format-List
```

출력된 4개 값을 메모합니다.

### C-3. 사용자 환경 변수 설정 (스크립트용)

> ⚠️ `scripts\*.ps1` 은 **사용자(User) 범위 환경 변수**를 읽습니다.
> `$env:` 로만 설정하면 스크립트가 값을 찾지 못합니다.

```powershell
$vars = @{
  CREWMEAL_M365_TENANT_ID     = "<테넌트 ID>"
  CREWMEAL_M365_CLIENT_ID     = "<클라이언트 ID>"
  CREWMEAL_M365_CLIENT_SECRET = "<클라이언트 시크릿>"
  CREWMEAL_M365_SITE_ID       = "<C-2의 SITE_ID>"
  CREWMEAL_M365_DRIVE_ID      = "<C-2의 DRIVE_ID>"
  CREWMEAL_M365_LIST_ID       = "<C-2의 LIST_ID>"
  CREWMEAL_M365_SITE_URL      = "<C-2의 SITE_URL>"
  CREWMEAL_M365_CONNECTION_ID = "crewmealpoc"
}
foreach ($k in $vars.Keys) {
  [Environment]::SetEnvironmentVariable($k, $vars[$k], "User")
  Set-Item -Path "Env:$k" -Value $vars[$k]
}
```

설정 후 **PowerShell 창을 새로 열어야** 값이 반영됩니다.

### C-4. 앱에 대상 사이트 권한 부여 (사이트 관리자가 1회 실행)

```powershell
cd $HOME\CrewMeal
.\scripts\grant_test_site_permission.ps1
```

장치 코드 로그인이 뜨면 **사이트 관리자 계정**으로 로그인합니다.
출력에 `roles: write` 가 나오면 성공입니다.

### C-5. 게시 방식 선택

관리자 포털 `{SERVICE_WEB_URI}/admin/settings` → **검색 콘텐츠 게시 방식**
카드에서 하나를 고릅니다. **새 설치는 선택 전까지 아무 곳에도 게시하지 않습니다.**

| 방식 | 장점 | 단점 | 추천 |
| --- | --- | --- | --- |
| **SharePoint 컬럼** | 원본 문서의 권한(ACL)을 그대로 사용, 설정 단순 | 컬럼당 63,999자 제한 | ⭐ 처음 테스트에 권장 |
| **Copilot 커넥터** | 길이 여유(HTML 3MB), 별도 색인 | 커넥터 권한·관리센터 설정 필요 | 대규모 검토 시 |

#### C-5-a. SharePoint 컬럼 방식

```powershell
cd $HOME\CrewMeal
.\scripts\provision_test_library_admin.ps1
```

이 스크립트는 라이브러리에 상태 열들을 만들고, 작업 중에만 앱 권한을
`fullcontrol` 로 올렸다가 **끝나면 반드시 `write` 로 되돌립니다.**

검색 콘텐츠 컬럼(`CrewmealSearchContent`) 연결과 상태 열 서식은 SharePoint가
앱 전용 토큰을 허용하지 않아 **사이트 소유자 세션**이 필요합니다. 위임 토큰이
있다면 아래처럼 적용합니다.

```powershell
$env:CREWMEAL_M365_SHAREPOINT_ACCESS_TOKEN = "<위임 SharePoint 액세스 토큰>"
.\.venv\Scripts\python.exe .\scripts\configure_test_library.py `
  --apply-content-column --apply-formatting
```

#### C-5-b. Copilot 커넥터 방식

```powershell
cd $HOME\CrewMeal
.\.venv\Scripts\python.exe .\scripts\configure_copilot_connection.py
```

그리고 **Microsoft 365 관리 센터 → 검색 및 인텔리전스 → 사용자 지정 →
세로 항목 → All → 커넥터 결과 관리** 에서 해당 커넥터의 인라인 결과를
**활성화**해야 Copilot이 항목을 참조합니다.

### C-6. 문서 형식 켜기

`/admin/settings` → **문서 형식 지원** 카드에서 테스트할 형식을 체크합니다.

| 형식 | 상태 | 추가 준비물 |
| --- | --- | --- |
| PPTX | ✅ | 서버 이미지에 LibreOffice 포함 |
| PDF | ✅ | 없음 |
| HWP / HWPX | ✅ | 서버 이미지에 rhwp 0.7.19 포함 |
| DOCX / DOCM | ✅ | 서버 이미지에 LibreOffice 포함 |
| XLSX / XLSM | ✅ | 서버 이미지에 LibreOffice 포함 |

### C-7. SharePoint 명령(SPFx) 배포

1. **설정 값 치환** — 두 파일의 자리표시자를 실제 값으로 바꿉니다.

   | 파일 | 바꿀 값 |
   | --- | --- |
   | `sharepoint\search-enhancement-command\src\extensions\searchEnhancement\SearchEnhancementCommandSet.manifest.json` | `apiBaseUrl`, `apiResource` |
   | `sharepoint\search-enhancement-command\sharepoint\assets\elements.xml` | `apiBaseUrl`, `apiResource` |
   | `sharepoint\search-enhancement-command\config\package-solution.json` | `webApiPermissionRequests.resource` |

   - `apiBaseUrl` = 트랙 B의 `SERVICE_WEB_URI`
   - `apiResource` = Ingest용 앱 등록의 App ID URI (예: `api://crewmeal-ingest`)

2. **패키지 빌드** (Node.js **22.14 이상 23 미만** 필요)

   ```powershell
   cd $HOME\CrewMeal\sharepoint\search-enhancement-command
   npm install
   npm run build
   npm run test:unit
   ```

   결과물: `sharepoint\solution\search-enhancement-command.sppkg`

3. **App Catalog 업로드** → 앱을 **테스트 사이트에만** 설치
   (테넌트 전체 배포는 사용하지 않습니다.)

4. **SharePoint 관리 센터 → 고급 → API 액세스** 에서 SPFx가 요청한
   권한을 **승인**합니다.

5. **CORS/인증 설정** — SPFx는 SharePoint 페이지에서 API를 호출하므로
   허용 오리진이 필요합니다.

   ```powershell
   cd $HOME\CrewMeal
   azd env set CREWMEAL_INGEST_ALLOWED_ORIGINS "https://contoso.sharepoint.com"
   azd env set CREWMEAL_INGEST_REQUIRE_AUTH    "true"
   azd env set CREWMEAL_INGEST_AUDIENCE        "api://crewmeal-ingest"
   azd env set CREWMEAL_INGEST_ALLOWED_APP_IDS "<SPFx가 사용하는 앱 ID>"
   azd env set CREWMEAL_M365_SITE_ID   "<C-2의 SITE_ID>"
   azd env set CREWMEAL_M365_DRIVE_ID  "<C-2의 DRIVE_ID>"
   azd env set CREWMEAL_M365_LIST_ID   "<C-2의 LIST_ID>"
   azd env set CREWMEAL_M365_SITE_URL  "<C-2의 SITE_URL>"
   azd up
   ```

### C-8. 실사용 테스트

1. SharePoint 문서 라이브러리에서 **PPT 파일 1개**를 선택합니다.
2. 명령 모음에 **「코파일럿을 위해 검색강화」** 버튼이 나타납니다.

   | 문서 상태 | 보이는 버튼 |
   | --- | --- |
   | 미등록 | 코파일럿을 위해 검색강화 |
   | 실패 / 원본 갱신됨 | 검색강화 다시 시도 |
   | 대기·처리·완료 | 검색강화 삭제 |

3. 버튼을 누르면 상태 열이 `Queued` → `Processing` → `Ready` 로 바뀝니다.
4. `CrewmealSearchStatusLink` 열의 링크를 눌러 진행 상황을 실시간 확인합니다.

> 버튼은 **편집 권한이 있는 사용자가 지원 문서 1개만 선택했을 때** 나타납니다.
> 여러 개를 선택하면 보이지 않습니다.

### C-9. 검색·Copilot 반영 확인

컬럼 방식은 SharePoint 검색 인덱스에 반영되어야 Copilot이 참조합니다.

1. 라이브러리 **설정 → 고급 설정 → 문서 라이브러리 다시 인덱싱** 실행
2. 인덱싱은 수 분~수 시간 걸립니다 (테넌트 상황에 따라 다름).
3. 생성된 검색 콘텐츠에만 존재하는 **고유 문자열(canary)** 로 Microsoft Search를
   검색해 원본 문서가 결과에 나오는지 확인합니다.
4. `/admin/settings` → **컬럼 검색 준비** 카드에 canary와 원본 URL을 기록합니다.
5. 같은 문서(또는 라이브러리)로 범위를 지정한 Copilot에게 질문해서
   canary 내용을 근거로 답하는지 확인합니다.

✅ **트랙 C 합격 기준**: SharePoint 버튼 → 상태 `Ready` → 검색에서 canary 발견
→ 범위 지정 Copilot이 강화된 내용을 근거로 답변.

---

## 5. 테스트 시나리오와 합격 기준

고객사 검증 회의에서 그대로 쓸 수 있는 체크리스트입니다.

| # | 시나리오 | 조작 | 합격 기준 | 트랙 |
| --- | --- | --- | --- | --- |
| 1 | 기본 텍스트 추출 | 텍스트 위주 PPT 업로드 | 원문 문장이 결과 HTML에 보존됨 | A |
| 2 | 표 추출 | 표가 있는 PPT | 표의 행/열 값이 누락 없이 재현 | A |
| 3 | 차트 해석 | 막대/선 차트 슬라이드 | 그래프가 무엇을 뜻하는지 문장으로 설명 | B |
| 4 | 간트차트 | 일정표 슬라이드 | 작업명·기간·순서가 문장으로 설명 | B |
| 5 | PDF 처리 | 스캔 아닌 PDF | 페이지별 내용이 추출됨 | B |
| 6 | HWP/HWPX | 한글 문서 | 본문·표·머리말/꼬리말 추출 | B |
| 7 | 대용량 | 50장 이상 덱 | 실패 없이 완료, 소요 시간 기록 | B |
| 8 | 재작업 | 상태 페이지 → 다시 실행 | 새 결과로 갱신 | B |
| 9 | 피드백 반영 | 상태 페이지 → 코멘트 후 재작업 | 코멘트가 반영된 결과 | B |
| 10 | 원본 무결성 | 처리 후 원본 다운로드 | **원본 파일이 변경되지 않음** | C |
| 11 | 권한 | 권한 없는 사용자로 접근 | 상태 페이지 접근 차단 | C |
| 12 | 삭제 | 검색강화 삭제 | 색인/컬럼에서 제거, 상태 `NotEnabled` | C |
| 13 | 검색 반영 | canary 검색 | 원본 문서가 결과에 노출 | C |
| 14 | Copilot 답변 | 범위 지정 질의 | 강화 내용을 근거로 답변 | C |
| 15 | 비용 확인 | 관리자 대시보드 | 문서별 토큰·추정 비용 표시 | B |

---

## 6. 환경 변수 총정리

### 필수

| 변수 | 설명 | 예시 |
| --- | --- | --- |
| `CREWMEAL_M365_TENANT_ID` | Entra 테넌트 ID | GUID |
| `CREWMEAL_M365_CLIENT_ID` | 앱 클라이언트 ID | GUID |
| `CREWMEAL_M365_CLIENT_SECRET` | 앱 시크릿 | (비밀) |
| `CREWMEAL_M365_SITE_ID` | SharePoint 사이트 ID | `host,guid,guid` |
| `CREWMEAL_M365_DRIVE_ID` | 문서 라이브러리 드라이브 ID | |
| `CREWMEAL_M365_LIST_ID` | 목록 ID | GUID |
| `CREWMEAL_M365_SITE_URL` | 사이트 URL | `https://.../sites/...` |
| `CREWMEAL_ADMIN_KEY` | 관리자 포털 키 | (비밀) |
| `CREWMEAL_WEB_SESSION_SECRET` | 세션 서명 키 | (비밀) |

### 선택 / 튜닝

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `PPTX_ANALYSIS_TIER` | `vision` | `vision`(고품질) / `text_ocr`(무비용) |
| `PPTX_OCR_ENABLED` | `true` | 저품질 티어에서 이미지 OCR 사용 |
| `SLIDE_IMAGE_MODEL` | `gpt-5.6-luna` | `gpt-5.2`, `gpt-5-mini` 선택 가능 |
| `SLIDE_IMAGE_DEPLOYMENT` | `gpt-5-6-luna-test` | Foundry 배포 이름 |
| `SLIDE_IMAGE_RENDER_DPI` | `144` | 페이지 렌더 해상도 |
| `SLIDE_IMAGE_MAX_WORKERS` | `2` | 동시 분석 슬라이드 수 |
| `CREWMEAL_INGEST_REQUIRE_AUTH` | `true` | Ingest API 토큰 검증 |
| `CREWMEAL_STATUS_REQUIRE_AUTH` | 위 값과 동일 | 상태 페이지 Entra 로그인 |
| `CREWMEAL_INGEST_ALLOWED_ORIGINS` | (없음) | CORS 허용 오리진(CSV) |
| `CREWMEAL_ARTIFACT_BACKEND` | 자동 | `database` / `blob` / 로컬 파일 |
| `SOFFICE_PATH` | 자동 탐색 | 로컬 LibreOffice 경로 |
| `RHWP_PATH` | 자동 탐색 | 로컬 rhwp 경로 |
| `CREWMEAL_MIP_SDK_CLI` | (없음) | MIP 복호화 CLI (README 참고) |

---

## 7. 문제 해결

| 증상 | 원인 | 해결 |
| --- | --- | --- |
| `ConfigurationError: Missing Microsoft 365 settings` | 워커 실행 창에 M365 변수 없음 | 같은 창에서 변수 설정 후 재실행 (트랙 A는 더미 값 사용) |
| 웹 시작 시 `CREWMEAL_WEB_SESSION_SECRET must be set` | 인증 켠 상태에서 세션 키 없음 | 세션 시크릿 설정 또는 `CREWMEAL_INGEST_REQUIRE_AUTH=false` |
| 로그인 시 `AADSTS50011` | 리디렉션 URI 미등록 | B-5 수행 (`{웹주소}/auth/callback` 추가 + ID 토큰 허용) |
| 웹 앱이 기동 실패 (`ConfigurationError`) | `CREWMEAL_STATUS_REQUIRE_AUTH=true` 인데 SSO 자격증명 없음 | M365 변수 3종 설정 또는 해당 값을 `false` |
| SPFx 버튼에서 `Failed to fetch` | CORS 미설정 | `CREWMEAL_INGEST_ALLOWED_ORIGINS` 에 SharePoint 오리진 추가 후 재배포 |
| Graph 403 오류 | 사이트 권한 미부여 | `scripts\grant_test_site_permission.ps1` 실행 |
| 스크립트가 "Missing user environment variable" | `$env:` 로만 설정 | C-3처럼 **User 범위**로 설정 후 새 창에서 실행 |
| `azd up` 이 쿼터 오류로 실패 | 모델 TPM 부족 | `infra/modules/foundry.bicep` 의 capacity 값 축소 |
| 리전 오류 | `location` 이 `eastus2` 로 고정 | `infra/main.bicep` 의 `@allowed` 목록 수정 |
| 상태가 계속 `Queued` | 워커 미실행 | 로컬은 `cli once`, Azure는 워커 앱 상태 확인 |
| 결과에 그림 설명이 없음 | 저품질 티어 | `/admin/settings` 에서 분석 티어를 **고품질**로 변경 후 재작업 |
| 한글 OCR이 깨짐 | 기본 OCR 모델은 한국어 미지원 | 로컬은 `PPTX_OCR_REC_MODEL`/`PPTX_OCR_REC_KEYS` 지정, 컨테이너는 `--build-arg ENABLE_LOW_TIER_OCR=1` 로 한국어 모델 포함해 빌드 |
| MIP 문서 처리 실패 | 복호화 미구성 | README 「MIP 복호화」 참고, 관리 포털의 준비 마법사로 점검 |
| 검색에 반영되지 않음 | 재인덱싱 안 함 | 라이브러리 설정에서 수동 재인덱싱 후 대기 |

**로그 확인**

```powershell
# 웹 앱 로그
az containerapp logs show -g <리소스그룹> -n <웹앱이름> --tail 100
# 워커 로그
az containerapp logs show -g <리소스그룹> -n <워커앱이름> --tail 100
```

---

## 8. 비용 관리와 정리

- **문서별 추정 비용**은 관리자 대시보드와 상태 페이지에 표시됩니다.
- 비용을 아예 쓰지 않으려면 분석 티어를 **저품질(text_ocr)** 로 두세요.
- PoC 구성은 야간 비용 절감을 위해 PostgreSQL이 자동 정지되고, 매일 아침
  (한국시간 기준, 기본 07시) Logic App이 자동 재시작합니다. 워커는 DB 연결이
  끊긴 동안 재시도하며 스스로 복구합니다.

**테스트 종료 후 전체 삭제** (되돌릴 수 없습니다):

```powershell
cd $HOME\CrewMeal
azd down --purge --force
```

추가 정리:

- SharePoint 테스트 사이트에서 앱 제거, App Catalog에서 패키지 삭제
- Entra 앱 등록 삭제
- 커넥터 방식이었다면 Copilot 커넥터 연결 삭제

---

## 9. 보안·데이터 취급

- 원본 문서는 **임시 폴더에만** 내려받고 처리 후 삭제합니다.
- 원본 파일 바이너리는 **수정하지 않습니다.**
- 원본 문서·PDF·PNG·HTML 본문과 비밀 값은 로컬 상태 DB에 저장하지 않습니다.
- Azure OpenAI·Blob 접근은 **API 키 없이** 관리 ID(`DefaultAzureCredential`)로 인증합니다.
- SharePoint 접근 권한은 `Sites.Selected` 로 **지정한 사이트에만** 부여됩니다.
- 컬럼 방식은 원본 문서의 권한을 그대로 따르므로 권한 복제가 없습니다.
- PoC는 공개 엔드포인트를 사용하므로 **기밀 문서를 넣지 마세요.**

---

## 10. 도움 요청 시 함께 보내주세요

문제가 해결되지 않으면 아래 정보를 정리해 전달해 주세요.

1. 어느 트랙(A/B/C)의 몇 번 단계인지
2. 실행한 명령과 화면에 나온 오류 메시지 전문
3. `azd env get-values` 결과 (**비밀 값은 반드시 가리고**)
4. 상태 페이지의 실패 단계 이름과 메시지
5. 컨테이너 앱 로그 마지막 100줄
