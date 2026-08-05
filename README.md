# 쇼츠 팩토리 (Shorts Factory)

유튜브 롱폼에서 조회수가 검증된("터진") 소스를 자동으로 찾아 쇼츠로 잘라 편집하고,
자동으로 업로드까지 하는 로컬 자동화 데몬입니다. 전체 설계는 [`docs/PLAN.md`](docs/PLAN.md) 계획 문서를 참고하세요.

**지금 구현된 범위(Phase 1)**: 채널 1개, CLI로 직접 트리거, "롱폼 하이라이트컷" 전략
(`longform_highlight_cut`) 파이프라인 전체 — 유튜브 검색/발굴 → 다운로드 → 전사 →
구간선정(Claude) → 렌더링(크롭+자막+오버레이) → 업로드까지. 스케줄러(24시간 자동 반복),
번역/더빙형·카드뉴스형 전략, 대시보드, 인스타/틱톡은 아직입니다 (계획서의 Phase 2~4).

## ⚠️ 중요: 이 코드는 실제 유튜브/네트워크로 아직 실행 검증되지 않았습니다

이 프로젝트는 네트워크가 정책적으로 제한된 개발 샌드박스에서 작성되었습니다
(youtube.com 등 외부 도메인 접속 자체가 차단됨). 그래서:

- 모든 순수 로직(조회 속도 스코어링, 설정 검증, ASS 자막 생성, 상태 전이, 할당량 계산 등)은
  `pytest`로 자동 테스트되어 있고 전부 통과합니다 (`tests/` 참고).
- ffmpeg 크롭/오버레이/자막 합성 파이프라인은 **합성 테스트 영상**으로 실제 렌더링까지
  검증했습니다 (진짜 카메라 영상이 아님).
- **실제 유튜브 API 호출(검색/다운로드/업로드), 실제 OAuth 플로우, 실제 게임/건강정보 영상으로
  얼굴추적·크롭 품질은 전혀 검증되지 않았습니다.** 이건 사용자의 실제 Mac/PC에서 처음
  돌려보면서 확인해야 하는 부분입니다 (계획서 Phase 1 검증 항목과 동일).

아래 설치 과정을 따라가면서 막히는 부분이 있으면 알려주세요 — 실제 환경에서만 드러나는
버그가 있을 수 있습니다.

## 사전 준비물

1. **Python 3.11 이상**
2. **ffmpeg** — `brew install ffmpeg` (Mac) 또는 `apt install ffmpeg` (Linux)
3. **Anthropic API 키** (Claude) — https://console.anthropic.com
4. **Google Cloud 프로젝트 + OAuth 클라이언트** — 채널당 1개 권장 (할당량 격리, 계획서 참고)
5. **Playwright용 Chromium** — 아래 설치 과정에 포함

## 설치

```bash
git clone <이 저장소>
cd easycut
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

`.env.example`을 `.env`로 복사하고 값을 채우세요:

```bash
cp .env.example .env
```

- `ANTHROPIC_API_KEY` — 필수
- `YOUTUBE_OAUTH_CLIENT_SECRETS_FILE` — 아래 OAuth 설정에서 만든 JSON 파일 경로
- `SECRETS_BACKEND` — 기본 `keyring` (Mac Keychain 자동 사용). 헤드리스 리눅스에서 keyring이
  안 될 경우 `file`로 바꾸고 `SECRETS_FILE_PASSPHRASE`를 설정하세요.

## 1) Google Cloud OAuth 설정 (채널당 1회)

1. [Google Cloud Console](https://console.cloud.google.com)에서 새 프로젝트 생성
   (채널마다 별도 프로젝트 권장 — 할당량 10,000유닛/일이 프로젝트 단위이기 때문, 계획서 참고)
2. "API 및 서비스 → 라이브러리"에서 **YouTube Data API v3** 활성화
3. "API 및 서비스 → OAuth 동의 화면"에서:
   - User Type: External
   - **게시 상태를 "In production"으로 전환** (Testing 상태로 두면 refresh token이 7일 만에
     만료되어 무인 자동화가 조용히 멈춥니다 — 계획서에서 가장 강조한 부분)
4. "사용자 인증 정보 → OAuth 클라이언트 ID 만들기" → 애플리케이션 유형: **데스크톱 앱**
5. JSON 다운로드 → `config/youtube_client_secret.json`로 저장 (`.gitignore`에 이미 포함됨)
6. 채널당 1회, 아래 스크립트로 로그인 플로우 실행 (브라우저가 열리고 그 채널 계정으로 로그인):

```bash
python -c "
from shorts_factory.integrations.youtube_api import run_oauth_flow_for_channel
run_oauth_flow_for_channel('example_gaming_clips', 'config/youtube_client_secret.json')
"
```

로그인 시 "Google에서 확인하지 않은 앱" 경고가 뜨는데, "고급 → (앱이름)로 이동(안전하지 않음)"을
눌러 진행하세요 (계획서에서 언급한, 스크립트화 불가능한 1회성 수동 단계입니다).

## 2) 채널 설정

`config/channels/example_gaming_clips.yaml`, `example_health_info.yaml`을 참고해서
실제 채널 설정 파일을 만드세요 (`config/channels/<채널이름>.yaml`):

```yaml
source_strategy: longform_highlight_cut
format_template: simple_hook_top   # config/formats/ 참고 — 5종 템플릿 제공
niche_description: "채널 니치를 자연어로 설명 (LLM 프롬프트에 쓰임)"
search_keywords:
  - "검색 키워드 1"
  - "검색 키워드 2"
layout_config_ref: example_streamer_facecam  # 게임클립형만 필요, 건강정보형은 생략(얼굴추적)
gcp_project_ref: <위에서 만든 GCP 프로젝트 ID>
```

게임클립형(`layout_config_ref` 지정)은 `config/layouts/<ref>.yaml`에 실제 소스 영상의
페이스캠/게임화면 픽셀 좌표를 **채널마다 1회 수동으로** 기록해야 합니다 (완전 자동화 불가
— 계획서의 명시적 한계).

## 3) 실행

```bash
# 채널 1개 사이클 1회 실행 (발굴→다운로드→전사→구간선정→렌더링→업로드까지 전부)
shorts-factory run-once --channel example_gaming_clips

# 상태 확인
shorts-factory status

# 일시정지 / 재개
shorts-factory pause --channel example_gaming_clips
shorts-factory resume --channel example_gaming_clips
```

`run-once`는 한 사이클에 최대 `target_clip_count`개(기본 10개)를 렌더링해서 재고로 쌓고,
그중 `daily_publish_count`개(기본 1개)만 실제로 업로드합니다. 나머지는 다음 실행 때
자동으로 이어서 업로드됩니다 (계획서의 "배치 생성 + 점진적 업로드" 설계).

## 테스트

```bash
pytest tests/ -v
```

네트워크가 필요 없는 순수 로직만 검증합니다 (실제 유튜브 API 호출은 mock 처리).
Playwright 렌더링 테스트 하나는 브라우저가 설치 안 돼 있으면 자동으로 skip됩니다.

## 아직 안 된 것 (Phase 2~4, 계획서 참고)

- 24시간 자동 반복 스케줄러 (`APScheduler`) — 지금은 `run-once`로 수동 트리거만 가능
- 번역/더빙형(`viral_translate_dub`), 카드뉴스형(`template_card`) 전략 파이프라인
- 로컬 대시보드(FastAPI) — 지금은 CLI만
- 인스타그램/틱톡 업로드 — Meta 앱 리뷰 / 틱톡 감사 등 코드 밖 사전 절차 필요
- TTS 공급자 미정 — `tts_client.py` 플러그인 자리만 있고 실제 연동 안 됨
