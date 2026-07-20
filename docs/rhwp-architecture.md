# CrewMeal HWP/HWPX 처리 아키텍처

## 결정

CrewMeal은 HWP 5.x와 HWPX의 공식 처리 엔진으로
[rhwp](https://github.com/edwardkim/rhwp)를 사용한다.

- 버전: `0.7.19`
- 커밋: `8d3bfa4b92174b16bac587fe1409975cf34ba566`
- 라이선스: MIT
- 통합 경계: Python에서 고정된 `rhwp` 실행 파일을 subprocess로 호출
- 기본 입력: 페이지별 RenderTree JSON
- 최종 출력: CrewMeal 허용 태그로 정규화한 semantic HTML
- Vision: semantic payload가 없는 시각 객체가 있는 페이지만 선택적으로 분석

LibreOffice의 HWP/HWPX 가져오기 필터는 사용하지 않는다. LibreOffice는 PPTX 처리에
계속 필요하므로 컨테이너에서는 유지한다.

## 처리 흐름

1. 확장자, 파일 크기, OLE2/ZIP magic을 검증한다. HWP 3.x는 지원하지 않는다.
2. 임시 작업 폴더에 원본을 쓰고 `rhwp export-render-tree`를 한 번 실행한다.
3. 각 `render_tree_NNN.json`을 검증하고 다음 정보를 페이지 단위로 추출한다.
   - `Body`, `Column`, `TextLine`, `TextRun`: 읽기 순서의 본문
   - `Table`, `Cell`: 행·열을 보존한 표
   - `Header`, `Footer`: 머리말·꼬리말
   - `FootnoteArea`: 각주
   - `TextBox`와 도형 내부 `TextRun`: 글상자 및 도형 텍스트
4. 추출 결과를 CrewMeal `SlideContent` 계약으로 정규화한다. 이 결과가 최우선
   근거이며 모델 출력이 원문이나 표를 대체하지 않는다.
5. 페이지에서 `Image`, `Equation`, 의미가 없는 도형 같은 visual-only 노드를 찾는다.
6. visual-only 노드가 있는 페이지에만 `rhwp export-png -p <0-based-page>`를 실행하고
   기존 Vision 분석에 전달한다.
7. Vision 결과의 차트·관계·이미지 설명만 semantic 결과에 추가한다. 본문, 표,
   머리말·꼬리말, 각주는 rhwp 결과를 유지한다.
8. 병합된 구조를 기존 허용 목록 HTML renderer로 출력하고 Copilot Connector의
   `externalItem`으로 게시한다.

semantic 정보가 충분한 문서는 PNG 생성과 Vision 호출을 모두 생략한다.

## 런타임 계약

### `PreparedDocument`

기존 포맷과의 공통 파이프라인을 유지하되 다음 두 모드를 구분한다.

| 모드 | semantic slides | page images | 동작 |
| --- | --- | --- | --- |
| Visual-first | 없음 | 전체 페이지 | 기존 PPTX/PDF Vision 분석 |
| Semantic-first | 전체 페이지 | visual-only 페이지만 | rhwp 결과 사용 후 선택적 Vision 병합 |

`RendererManifest.page_count`는 전체 페이지 수다. Semantic-first 모드의
`page_images`는 전체 페이지가 아니라 Vision이 필요한 부분집합이다.

### 출력 보존 원칙

- 본문 순서는 RenderTree의 페이지·컨테이너·자식 순서를 따른다.
- 표는 `rows`, `cols`, `Cell.row`, `Cell.col`로 재구성한다.
- 표 내부 텍스트는 본문에 중복 삽입하지 않는다.
- 머리말, 꼬리말, 각주는 별도 section으로 보존한다.
- 빈 페이지도 페이지 번호를 유지한다.
- 링크는 rhwp RenderTree가 URL 의미를 제공할 때까지 일반 텍스트로 보존한다.
- 이미지와 수식은 존재 사실과 개수를 경고로 남기며, 선택적 Vision이 성공하면
  의미 설명을 추가한다.

## 예외 판정

| 상황 | 판정 | 처리 |
| --- | --- | --- |
| HWP 5.x 또는 정상 HWPX | 지원 | RenderTree 추출 |
| HWP 3.x signature | 미지원 | `InvalidDocumentError` |
| 암호화 HWP | 미지원 | `EncryptedDocumentError`; Vision 우회 금지 |
| 필수 HWPX part 누락 | 손상 문서 | `InvalidDocumentError`; Vision 우회 금지 |
| RenderTree 파일 없음/번호 누락/잘못된 JSON | 엔진 실패 | 명시적 처리 오류 |
| `Image`, `Equation`, visual-only 도형 | semantic 예외 | 해당 페이지만 PNG + Vision |
| `LAYOUT_OVERFLOW` | 품질 경고 | 결과에 경고를 보존하고 처리는 계속 |
| Vision 실패 | 부분 실패 금지 | semantic 원문은 유지하되 visual 보강 실패를 명시 |

암호화와 손상은 이미지 렌더링으로 해결할 수 없으므로 manual review 대상으로
분류한다.

## 배포

프로덕션 Docker image는 multi-stage build로 고정 커밋의 rhwp를
`native-skia` feature와 함께 빌드한다.

- build stage: Rust `1.93.1`, rhwp `0.7.19` 고정 커밋
- runtime binary: `/usr/local/bin/rhwp`
- configuration: `RHWP_PATH`
- runtime fonts: Noto CJK, DejaVu
- runtime libraries: Fontconfig, FreeType 및 Skia가 요구하는 공유 라이브러리
- process user: 기존 비루트 `appuser`
- temporary data: 문서별 임시 폴더에서만 생성하고 처리 후 삭제

한컴 전용 폰트가 없으면 fallback 폰트를 사용하므로 원본과 픽셀 단위로 완전히 같은
렌더링은 보장하지 않는다. semantic 추출은 폰트 fallback과 무관하다.

## 관측성

작업 결과와 stage timing에 다음 정보를 남긴다.

- rhwp 버전과 통합 모드
- 전체 페이지 수
- semantic-only 페이지 수
- 선택적 Vision 페이지 번호와 개수
- 노드별 개수(표, 이미지, 수식, 각주, 머리말, 꼬리말)
- rhwp parse/render 시간
- Vision 토큰과 지연 시간
- rhwp 경고 및 semantic coverage 경고

정상 semantic-only 문서의 Vision 토큰 사용량은 0이어야 한다.

## 검증 기준

- 고정 HWP5 10개와 HWPX 10개에서 production handler가 모두 성공한다.
- 본문 sentinel, 표 행·열, 머리말·꼬리말, 각주가 기대값과 일치한다.
- semantic-only 문서는 PNG와 Vision 호출이 0회다.
- visual-only 객체가 있는 문서는 해당 페이지만 렌더링·분석한다.
- 암호화 HWP와 손상 HWPX는 올바른 오류 유형으로 실패한다.
- PPTX/PDF의 기존 visual-first 동작에는 변화가 없다.
- production Docker image에서 비루트 사용자로 동일한 검증을 통과한다.
