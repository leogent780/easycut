# 쇼츠 팩토리 (Shorts Factory) — 구현 계획

## Context

유튜브 쇼츠/인스타 릴스/틱톡 계정을 여러 개(우선 3~4개, 추후 계정당 10개) 운영하는 "쇼츠 공장"을 만든다. 목표는 사용자가 일시정지하기 전까지 24시간 주기로 채널별 쇼츠를 자동 생성·자동 업로드하며 계정을 키우는 것. 채널이 충분히 커지면 조회수가 보장된 여러 채널에 사용자 자신의 제품/서비스 홍보 UGC 영상을 얹어 노출시키는 것이 최종 목적이다 (협찬 1개에 기대는 대신 조회수 검증된 채널 다수를 활용).

레퍼런스 서비스 **EasyCut**(easycut.co.kr)을 분석한 결과, "롱폼 유튜브 영상 URL 입력 → AI가 바이럴 구간 추출 → 여러 개 쇼츠로 자동 편집(제목/자막/가짜 댓글 오버레이 포함) → 다운로드해서 업로드"까지가 핵심 기능이었고, 자동 업로드는 EasyCut에도 없다 — 우리는 여기에 **자동 업로드 + 24시간 자동 반복 + 일시정지/수동개입**을 추가로 얹는 것이 차별점이다.

사용자가 보여준 실제 레퍼런스 채널들을 분석한 결과, 콘텐츠 소스 전략이 하나가 아니라 **4가지**로 갈린다는 것을 확인했고, 채널별로 다른 전략을 쓰기로 했다. 여기에 더해 **예능/아이돌 가십 채널**도 운영 예정이며, 화면 레이아웃(후킹 타이틀 위치, 번역자막 유무, 출처 워터마크, 쇼핑태그, 댓글 스크린샷 오버레이 등)이 채널마다 크게 다르다는 것도 추가 레퍼런스를 통해 확인해 **포맷 템플릿 시스템**을 별도로 설계한다 (§포맷 템플릿 시스템). 저장소(`leogent780/easycut`)는 현재 완전히 비어있는 그린필드 상태(커밋 없음)이며, 이 계획은 처음부터 설계하는 것이다.

**이미 확정된 핵심 결정사항 (재논의 대상 아님):**
- 저작권 리스크: 트렌드 영상을 변형 없이 그대로 하이라이트컷/재업로드하는 EasyCut과 동일한 리스크 프로필을 사용자가 인지하고 수용함
- 실행 환경: 사용자의 로컬 PC/Mac에서 상시 실행하는 로컬 데몬 (이 Claude Code 세션 환경이 아님 — 이 세션은 youtube.com 등 외부 접속이 네트워크 정책으로 차단되어 있고 세션 종료 시 초기화됨)
- 업로드 자동화 우선순위: 유튜브 먼저 완전자동, 인스타/틱톡은 이후 단계
- 계정: 시범 운영할 3~4개 채널/계정은 이미 생성 완료된 상태
- 업로드 빈도: 한 사이클에 10~12개 쇼츠를 배치 생성해두고, 하루 1~2개만 실제 업로드, 나머지는 재고로 쌓아 다음날들에 순차 업로드 (유튜브 API 할당량 문제를 근본적으로 피하면서 원래 구상했던 "24시간마다 1번" 페이스와 자연스럽게 일치)
- TTS/배경음악 공급자: 아직 계정 없음 — 플러그인 구조로 설계해두고 나중에 공급자(ElevenLabs 추천) 결정
- **레퍼런스 발굴 범위: 우선 유튜브(롱폼+쇼츠)만 완전 자동 발굴.** 틱톡/인스타는 공식 API에 트렌딩/조회수순 검색 자체가 없어 자동 발굴이 불가능하므로 후순위. 전략 2(완성 소스 재구성형)도 당장은 유튜브 쇼츠를 소스로 시작 — 유튜브 쇼츠 역시 이미 완성된 숏폼이고 API로 조회수 필터링이 되므로 "반드시 조회수가 검증된 것만 쓴다"는 원칙을 그대로 지킬 수 있음. 틱톡/인스타 확장은 §레퍼런스 발굴 전략 참고
- **댓글 스크린샷 오버레이는 AI가 그럴듯하게 생성(EasyCut 방식)** — 실제 계정의 실제 댓글을 캡처하지 않음. 실제 인물의 실제 공개 발언을 노출하는 것보다 조작 리스크는 낮지만, 명백히 "가짜 참여"를 진짜처럼 보이게 하는 연출이라는 점은 인지하고 감. 예능/아이돌 가십처럼 실존 인물을 다루는 콘텐츠는 저작권 리스크와 별개로 **명예훼손/초상권 리스크**가 추가로 있다는 것도 인지 (§명시적 리스크/한계 참고)

---

## 콘텐츠 소스 전략 4종 (채널별로 다르게 적용)

레퍼런스 채널 분석을 통해 확인한 4가지 전략. 아키텍처(스케줄러/계정관리/업로드/상태관리)는 전략과 무관하게 공유되고, **"콘텐츠 생성 단계"만 전략별로 교체 가능한 플러그인 모듈**로 설계한다.

| # | 전략 | 소스 | 예시 니치 | 저작권 리스크 | 파이프라인 복잡도|
|---|---|---|---|---|---|
| 1 | **롱폼 하이라이트컷** | 3~60분 유튜브 롱폼 (게임 스트리머 VOD, 건강정보 강연 등) | 게임클립형, 건강정보형 | 높음 (EasyCut과 동일 리스크, 사용자 수용) | 높음 — 다운로드+전사+구간선정+리프레이밍 |
| 2 | **완성 소스 재구성형** | 이미 완성된 숏폼/사진 — **1단계는 유튜브 쇼츠 소스로 한정**(자동 발굴), 틱톡/릴스 원본은 후순위 확장 | (a) 해외 바이럴 번역/더빙 — 라이프스타일/공감형 (예: 건강행복지킴이 채널) &nbsp; (b) 국내 예능/아이돌 가십 — 번역 불필요, 후킹 타이틀+댓글 오버레이 중심 (예: 장원영 사례) | 높음 (변형 없이 재업로드) + 가십형은 **명예훼손/초상권 리스크 추가** | 중간 — 다운로드(+번역, 가십형은 생략)+TTS/자막+댓글오버레이, 구간선정 불필요 |
| 3 | **카드뉴스/템플릿형 오리지널** | 없음 — LLM이 텍스트를 처음부터 생성 | 심리테스트/꿀팁/리스트형 (예: 혈액형 성격 카드) | 거의 없음 (완전 오리지널 텍스트) | 낮음 — 소스 다운로드/리프레이밍 리스크 자체가 없음 |
| 4 | *(향후 확장)* 라이선스 소스 전용 | 직접 촬영/제휴 확보 소스 | — | 최저 | 전략 1과 동일 파이프라인, 소스만 다름 |

각 채널의 YAML 설정(`config/channels/<name>.yaml`)에 `source_strategy: longform_highlight_cut | viral_translate_dub | template_card` 필드로 지정. 3~4개 채널을 실제로 어떤 전략에 매핑할지는 Phase 1~2 설정 시점에 사용자가 지정.

---

## 포맷 템플릿 시스템 (채널/업로드별 "카피 포맷" 선택)

추가 레퍼런스 5개(귀여움할당제/해외여행 번역/장원영 가십/또봐숏 뷰티/카더정원 재클립)를 분석한 결과, 콘텐츠 소스 전략과는 별개로 **화면에 무엇을 얹는지(오버레이 레이아웃)** 자체가 채널마다 크게 다르다는 걸 확인했다. 관찰된 요소 조합:

| 레퍼런스 | 후킹 타이틀 | 2차 캡션 | 출처 표기 | 쇼핑/제품 태그 | 댓글 스크린샷 |
|---|---|---|---|---|---|
| 귀여움할당제 (육아) | 상단 | 하단(추가 문구) | 없음 | **있음** | 없음 |
| 이탈리아 여행 (번역) | 상단(배너) | 하단(번역) + 원문 소캡션 | 원 계정 핸들 | 없음 | 없음 |
| 장원영 가십 | 상단(2줄 배너) | 없음 | 없음 | 없음 | **있음(AI생성)** |
| 또봐숏 (뷰티) | 상단 | 하단(설명) | 채널 로고 | 없음 | 없음 |
| 카더정원 재클립 | 상단 | 없음 | **하단 중앙 "출처: X"** | 없음 | 없음 |

**설계: "포맷 템플릿"을 독립적인 선택 가능 단위로 분리한다.** 콘텐츠 소스 전략(무엇을 소스로 쓸지)과 포맷 템플릿(화면에 뭘 얹을지)을 직교(orthogonal)하게 설계해, 채널 세팅 시점에 소스 전략과 포맷 템플릿을 각각 고르고, 필요하면 업로드(수동 실행) 시점에 포맷만 override할 수 있게 한다.

- `config/formats/<template_name>.yaml` — 각 템플릿이 다음 슬롯의 on/off와 스타일을 선언:
  - `hook_title`: 위치(상단/상단+하단), 배너 스타일(색상/굵기/줄 수)
  - `secondary_caption`: on/off, 내용 소스(번역문 vs 설명문 vs 원문 소캡션), 위치
  - `source_attribution`: on/off, 텍스트 템플릿(`@{handle}` vs `출처: {name}`), 위치
  - `shopping_tag`: on/off — 실제 제품/링크는 Phase 3~ 이후 사용자의 홍보 단계에서 채워짐, 지금은 슬롯만 준비
  - `comment_overlay`: on/off, 댓글 텍스트는 `claude_client`가 세그먼트/주제 맥락으로 그럴듯하게 생성 (실제 계정 스크래핑 안 함)
  - `karaoke_caption_style`: ASS 스타일 프리셋 참조
- 채널 config(`config/channels/<name>.yaml`)에 `format_template: <name>` 필드 추가. CLI `run-once --channel X --format Y`로 1회성 override 가능.
- **렌더링 방식**: 카라오케 자막(단어별 하이라이트)은 기존 설계대로 ASS 파일 유지(프레임 정확도 필요). 그 외 정적/준정적 오버레이 요소(후킹 배너, 2차 캡션, 출처 표기, 쇼핑 태그, 댓글 스크린샷)는 **HTML/CSS 템플릿을 Playwright로 투명 PNG로 스크린샷**해서 ffmpeg `overlay` 필터로 합성 — 전략 3(카드뉴스)의 `card_renderer.py`와 동일한 렌더러를 재사용, 복잡한 배지/말풍선/그림자 레이아웃도 유연하게 처리 가능.
- 시작 템플릿 라이브러리(레퍼런스 매핑): `simple_hook_top`(게임클립 기본형), `dual_caption_translate`(번역형), `dual_caption_shopping_tag`(육아/라이프스타일형), `gossip_comment_overlay`(예능/가십형), `domestic_reclip_attribution`(국내 재클립형)

---

## 레퍼런스 발굴(Discovery) 전략

시스템의 4단계(채널분류 → **레퍼런스 발굴** → 숏폼변환 → 업로드) 중 핵심 병목. 원칙은 "플랫폼이 어디든 반드시 조회수가 이미 검증된(터진) 소스만 사용한다"는 것 — 이 원칙을 지키되, 플랫폼별 자동화 가능 여부가 다르다는 기술적 비대칭을 인정하고 간다.

| 플랫폼 | 자동 발굴 가능 여부 | 방법 |
|---|---|---|
| **유튜브 (롱폼+쇼츠)** | 가능 — 1단계 전면 채택 | `search.list`(조회수순/최근 N일) + `videos.list`(정확한 조회수·길이·임베드 가능 여부 확인)로 "최근 급상승/고조회수" 후보를 완전 자동 필터링 |
| 틱톡 | 불가 (공식 트렌딩/조회수 검색 API 없음) | **후순위.** 발굴 자동화는 보류, 필요 시 사용자가 직접 링크를 큐레이션해 넣는 반자동 경로를 나중에 추가 |
| 인스타(릴스) | 불가 (동일 사유) | 틱톡과 동일하게 후순위 |

**결정: Phase 1~2는 유튜브(롱폼+쇼츠)만으로 3가지 전략을 전부 커버한다.**
- 전략 1(롱폼 하이라이트컷): 유튜브 롱폼 소스 — 원래 설계 그대로
- 전략 2(해외 바이럴 번역/더빙형): 소스를 유튜브 쇼츠로 한정 — 이미 완성된 유튜브 쇼츠 중 조회수 검증된 것을 찾아 번역/더빙. 틱톡/릴스 원본 소스는 백로그로 이월
- 전략 3(카드뉴스/템플릿형): 애초에 소스 영상이 필요 없어 이 이슈와 무관

`discover.py`는 플랫폼을 확장 가능한 인터페이스로 설계하되(`discover_youtube()`가 1단계 유일한 구현체), 틱톡/인스타용 `discover_tiktok()`/`discover_instagram()`은 스텁으로만 남겨 향후 공식 API 또는 서드파티 트렌드 서비스가 확보됐을 때 갈아끼울 수 있게 한다.

### 레퍼런스 영상 선정 알고리즘 (채널·사이클당 "그 하나"를 고르는 법)

`order=viewCount`(누적 조회수 랭킹)만으로는 "터지고 있는 것"을 못 고른다 — 2년 전 5천만 뷰 영상이 어제 올라와 지금 폭발 중인 영상보다 항상 위에 뜨기 때문. 그래서 조회수가 아니라 **조회 속도**를 기준으로 삼는다.

1. 채널 config의 `search_keywords`(여러 변형, 사이클마다 로테이션)로 `search.list` 호출 — `publishedAfter`를 최근 3~7일로 좁혀 애초에 "최근 것"만 후보군에 들어오게 함
2. `videos.list` 배치 조회로 정확한 조회수/게시일/길이/embeddable 확보
3. **"터짐 스코어" = 조회수 ÷ 게시 후 경과일수**로 후보를 재정렬 (누적 랭킹이 아니라 최근 며칠 새 속도 근사치)
4. `source_videos` 테이블과 대조해 이미 처리된 영상(채널별 또는 전역) 제외
5. 상위 5~10개 후보만 저비용 LLM(`claude-haiku-4-5`) 1회 호출로 "제목/설명이 실제로 이 채널 니치와 맞는가" 필터링 — 키워드는 맞지만 맥락이 다른 오탐(예: 건강 키워드가 우연히 들어간 무관 영상)을 제거
6. 최종 1순위를 소스로 채택, 다운로드/전사 실패 시 다음 순위로 폴백 (§실패 처리 원칙과 동일)

---

## 아키텍처 개요

### 기술 스택

| 영역 | 선택 | 이유 |
|---|---|---|
| 언어/런타임 | Python 3.11+ | 영상처리/ML전사/Google·Anthropic SDK 생태계가 가장 풍부 |
| 소스 다운로드 | `yt-dlp` | 유튜브뿐 아니라 틱톡/인스타(전략 2)도 지원, 자막 추출 겸용 |
| 전사(transcript) | `yt-dlp` 자동자막 우선 → `faster-whisper` 폴백 | API 자막 엔드포인트는 타인 영상에 실질적으로 사용 불가 |
| LLM (구간선정/제목/번역/카드텍스트) | Anthropic Claude API — 구간선정은 `claude-sonnet-5`(structured output), 제목생성 등 경량 작업은 `claude-haiku-4-5` | 추론 난이도별로 모델 분리해 비용 최적화 |
| 영상 렌더링 | `ffmpeg` (subprocess), `moviepy` 미사용 | 배치 렌더링 처리량/안정성 우위, 캡션은 ASS 자막파일로 카라오케 스타일 지원 |
| 세로 리프레이밍 | 게임클립: 스트리머별 고정영역 크롭 설정(수동 1회 캘리브레이션) / 건강정보: 얼굴추적 기반 자동 팬크롭 (mediapipe) | 완전 자동화 불가 — 게임클립은 신규 스트리머마다 수동 보정 필요함을 명시 |
| 카드뉴스 렌더링 (전략 3) | HTML/CSS + Playwright 스크린샷 (이 환경에 이미 설치됨) → ffmpeg로 카드 시퀀스+팬줌+TTS+BGM 합성 | 스타일링 유연, 별도 렌더러 불필요 |
| TTS (전략 2, 3) | 플러그인 인터페이스 (`tts_client.py`), 기본 추천 ElevenLabs, 미결정 상태로는 자막만 굽고 TTS 단계 skip 가능하게 설계 | 계정 미보유 — 나중에 공급자 교체 용이하도록 |
| 스케줄러 | `APScheduler` (BackgroundScheduler + SQLAlchemyJobStore), 채널별 잡 + 랜덤 지터 | 일시정지/재개/수동실행 API가 내장, cron 대비 상태관리 용이 |
| 로컬 상태 저장 | SQLite (SQLAlchemy) | 로컬 데몬에 적합, 파일 하나로 이식 가능 |
| 로컬 시크릿 저장 | `keyring` (OS 네이티브) + 암호화 파일 폴백 | Mac/PC 우선, 헤드리스 리눅스는 폴백 문서화 |
| 제어 화면 | Phase 1~2: CLI (`typer`) / Phase 3: FastAPI+Jinja2 대시보드 | 일시정지 버튼은 Phase 3에서, 초기엔 CLI로 충분 |

### 저장소 구조

```
shorts_factory/
  pyproject.toml
  .env.example
  config/
    channels/                      # 채널별 YAML: niche, source_strategy, format_template, keywords, layout_ref, gcp_project_ref
    layouts/                       # 게임클립 채널의 수동 크롭 좌표 설정
    formats/                       # 포맷 템플릿 YAML+HTML (hook_title/secondary_caption/source_attribution/shopping_tag/comment_overlay 슬롯 정의)
  shorts_factory/
    cli.py
    config.py
    state.py                       # SQLAlchemy 모델 (아래 스키마)
    secrets.py
    scheduler.py                   # APScheduler, 채널별 잡, pause/resume
    pipeline/
      base.py                      # 전략 공통 인터페이스 (discover→render→upload 단계 계약)
      longform_highlight_cut.py    # 전략 1: discover+transcribe+highlight_select+render
      viral_translate_dub.py       # 전략 2: 완성 숏폼 다운로드+번역+TTS/자막
      template_card.py             # 전략 3: LLM 텍스트생성+카드렌더+TTS/BGM 합성
      captions.py                  # 단어 타임스탬프 → ASS 자막 생성 (공통, 카라오케 캡션 전용)
      overlay_render.py            # 포맷 템플릿 슬롯 → HTML/CSS → Playwright PNG → ffmpeg overlay 합성 (공통)
      upload_youtube.py            # OAuth 갱신 videos.insert + 할당량 스로틀링 (공통)
      quota.py                     # 프로젝트별 일일 유닛 추적 (공통)
    integrations/
      youtube_api.py
      claude_client.py
      ytdlp_client.py
      whisper_client.py
      tts_client.py                # 플러그인 인터페이스, 공급자 미정 상태로 시작
      card_renderer.py             # Playwright 기반 카드 스크린샷 (overlay_render.py와 렌더러 공유)
      comment_generator.py         # AI 가짜 댓글 오버레이 텍스트 생성 (실제 계정 스크래핑 안 함)
    dashboard/                     # Phase 3
    prompts/
  data/                            # gitignored: sqlite db, scratch, output
  tests/
```

### SQLite 스키마 (요지)

```
channels(id, name, source_strategy, format_template, niche_keywords json, layout_config_ref,
         status[active|paused|needs_reauth], gcp_project_ref, last_run_at)
credentials(id, channel_id FK, provider[youtube|instagram|tiktok],
            keyring_key_ref, expires_at, status)
source_videos(id, source_id UNIQUE, channel_id FK, discovered_at, processed_status)
jobs(id, channel_id FK, cycle_date, status[pending|running|completed|failed])
clips(id, job_id FK, hook_title, format_template_used, rendered_path,
      upload_status[pending|pending_upload|uploaded|failed],
      youtube_video_id_uploaded, uploaded_at)
quota_usage(id, gcp_project_ref, date, units_used)
audit_log(id, job_id FK, step, message, level, timestamp)
```

`clips.upload_status = pending_upload`가 "배치로 만들어놨지만 아직 게시 안 한 재고" 상태를 표현 — 스케줄러가 매일 이 재고에서 1~2개를 꺼내 업로드.

---

## 유튜브 API 핵심 사항

- **OAuth**: Google Cloud Console에서 프로젝트 생성 → YouTube Data API v3 활성화 → OAuth 클라이언트(Desktop app 타입) → 채널당 1회 `InstalledAppFlow`로 로그인/동의. **동의 화면을 "In production" 상태로 전환해야** refresh token이 7일 만에 만료되는 문제를 피함 (verification 심사는 불필요, "unverified app" 경고 클릭 1회는 필요 — 채널당 수동 1회, 스크립트화 불가).
- **할당량**: 프로젝트당 일 10,000 유닛. `videos.insert`(실제 업로드) 1건에 1600 유닛 소요 — 배치 10~12개를 한 번에 올리면 이미 초과. **채널별(또는 소수 클러스터별)로 별도 GCP 프로젝트를 두는 것을 Phase 1~2부터 채택**해 할당량 풀을 분리. 배치생성+1~2개만 업로드 정책 덕분에 채널당 일 사용량은 대략 discover 300~500유닛 + upload 1600~3200유닛 수준으로 안정적으로 예산 내에 들어옴.
- **"Shorts로 인식"되는 조건**: 별도 API 플래그 없음 — 실제 파일이 세로(9:16)이고 3분 이하면 자동으로 Shorts 취급. `#Shorts`를 제목/설명에 포함하는 걸 관례적으로 권장.
- **자막 API의 한계**: `captions.list/download`는 본인 소유 채널 영상에만 실질적으로 동작 — 타 채널 소스 영상 전사는 `yt-dlp` 자동자막 또는 whisper 폴백으로 처리 (§기술스택 참고).
- **미사용 갱신토큰 자동폐기**: 6개월 이상 미사용 시 refresh token이 만료됨 — 장기 일시정지 채널에는 주기적 헬스체크(가벼운 인증 호출) 필요.

---

## 파이프라인 데이터 흐름 (채널·사이클당)

```
scheduler.py (지터 적용, paused 체크)
  └─ pipeline.run_cycle(channel_config)
       [source_strategy로 분기]
       ├─ longform_highlight_cut: discover → transcribe → highlight_select(LLM) → render(x10~12)
       ├─ viral_translate_dub:    discover(유튜브 쇼츠, 조회수검증) → transcribe → translate(LLM) → TTS/자막 → render(x1)
       └─ template_card:         topic_select(LLM) → generate_text(LLM) → card_render(Playwright) → render(x1)
       ├─ [공통] upload_youtube.py: 할당량 확인 → 재고(clips.pending_upload)에서 하루 1~2개만 실제 업로드
       └─ [공통] state.py: 매 단계 job/clip 상태 기록
```

**실패 처리 원칙**: 소스 영상 다운로드 실패 → 다음 후보로 스킵 (재시도 안 함) / 전사 실패 → 해당 영상 스킵 / LLM 구간선정이 스키마·시간범위 위반 → 1회 재프롬프트, 재실패 시 해당 사이클 무출력 / 렌더링은 클립 단위로 격리(하나 실패해도 나머지 진행) / 업로드는 할당량 초과 시 나머지를 `pending_upload`로 큐잉, OAuth 만료 시 채널을 `needs_reauth`로 전환하고 자동 일시정지.

---

## 단계별 빌드 순서

**Phase 1 — 단일 채널, CLI 트리거, 가장 어려운 파이프라인(전략 1: 롱폼 하이라이트컷) 검증**
- OAuth 앱 등록 → production 전환 → 장기 미사용 후 refresh 정상 동작까지 실제 검증 (제일 먼저 확인해야 할 항목 — 틀리면 전체 자동화 전제가 무너짐)
- 게임클립/건강정보 중 실제 채널 1개로 고정영역 크롭 또는 얼굴추적 팬크롭 구현, 실제 소스로 육안 검수
- Claude Sonnet 5 구간선정 프롬프트를 실제 전사 데이터로 여러 번 돌려 사람이 직접 채점하며 반복 개선
- Whisper 폴백을 실제 타겟 하드웨어에서 속도/정확도 측정
- 실제 사이클 1회 돌려 할당량 소모량 실측 → §할당량 설계 보정
- 산출물: CLI로 트리거된 1개 채널의 실제 라이브 유튜브 쇼츠 업로드 1건 성공

**Phase 2 — 스케줄러 + 멀티채널 + 일시정지/재개 + 나머지 2개 소스 전략 추가**
- APScheduler 데몬화, 채널별 24h 지터드 사이클, 재고 기반 1~2개/일 업로드 로직
- 채널별 GCP 프로젝트 분리 (3~4번째 채널 추가 전에 실행)
- `viral_translate_dub`(가십 니치 포함), `template_card` 파이프라인 모듈 추가 (공유 인프라 위에 얹기만 하면 되므로 Phase 1보다 훨씬 가벼움)
- 포맷 템플릿 시스템(`overlay_render.py`, `config/formats/*`) 구현 — 시작 템플릿 5종(§포맷 템플릿 시스템) 제작, 채널 config에 `format_template` 연결, CLI `--format` override 배선
- pause/resume이 DB 플래그 + 살아있는 스케줄러 잡 양쪽에 반영되도록 배선
- `needs_reauth`/실패 알림 (로그 또는 데스크톱 알림)

**Phase 3 — 로컬 대시보드 + 수동 업로드 오버라이드 + 작업 이력**
- FastAPI+Jinja2: 채널 목록(일시정지 토글), 작업 이력(클립 썸네일+상태), 수동 업로드 폼 (사용자가 직접 만든 영상을 드롭 → `upload_youtube.upload_short()` 직접 호출, 그날 자동 사이클은 스킵 처리)

**Phase 4 — 인스타/틱톡 (코드 외적 사전조건 있음)**
- 인스타: IG 비즈니스/크리에이터 계정 + 페이스북 페이지 연결 + Meta 앱 리뷰(콘텐츠 퍼블리싱 권한) 통과 필요 — 리뷰 완료 전까지 코드는 준비만 가능, 실제 게시 불가
- 틱톡: 감사(audit) 안 받은 앱은 초안함(inbox)까지만 자동 전송 가능, 실제 "게시"는 사용자가 틱톡 앱에서 수동 탭 필요 — 완전 자동 게시는 틱톡 감사 통과가 별도 선행되어야 함 (코드 밖 절차)

---

## 명시적 리스크/한계 (설계상 인지하고 가는 것)

- 저작권/ToS 리스크는 이미 수용됐지만, 채널이 스트라이크/클레임을 받으면 자동 일시정지+알림하는 `needs_reauth`류 상태를 두어 무한 재시도하지 않도록 함
- **예능/아이돌 가십 채널은 저작권 리스크와 별개로 명예훼손·초상권 리스크가 있음.** 실존 인물을 다루고 자극적인 후킹 타이틀("악플들", "논란" 등)을 쓰는 포맷 특성상, 한국법상 사실적시 명예훼손까지 문제될 수 있는 영역 — AI 생성 댓글 오버레이라 해도 특정인을 저격하는 문구로 읽히지 않도록 프롬프트 가드레일(실존하지 않는 표현으로 특정인을 비방/모욕하는 문구 생성 금지)을 `comment_generator.py`/제목생성 프롬프트에 명시적으로 넣어야 함. 이건 저작권 리스크처럼 "이미 수용"된 게 아니라 별도로 계속 관리해야 하는 항목
- 세로 리프레이밍은 게임클립 채널마다 최초 1회 수동 캘리브레이션 필요 — 완전 자동화(AI 오토크롭)는 R&D 영역으로 MVP 범위 밖
- LLM의 "이 구간이 터질만한가" 판단에 대한 자동 품질 지표가 없음 — Phase 1~2는 사람 육안 검수가 유일한 품질 게이트
- GPU 없는 머신에서는 whisper 전사가 사이클의 병목이 될 수 있음 — Phase 1에서 실측 필요
- `data/scratch`(다운로드 원본)는 보관정책 없이 방치하면 안 됨 — 처리 완료 후 정리 잡 필요

---

## 검증 방법

- Phase 1 종료 시점: `shorts-factory run-once --channel <name>` 실행 → 실제 유튜브 채널에 Shorts 1건이 라이브로 게시되는 것을 직접 확인 (영상 재생, 자막 표시, 세로 비율, `#Shorts` 노출 여부)
- 각 실패 시나리오(소스 다운로드 실패, 전사 실패, LLM 스키마 위반, 렌더링 실패, 업로드 할당량 초과, OAuth 만료)를 의도적으로 유발해 §실패 처리 원칙대로 동작하는지 확인
- Phase 2 종료 시점: 3개 채널을 동시에 스케줄러에 등록하고 하루 이상 방치 후 각 채널이 지터드 타이밍에 맞춰 정상적으로 사이클을 돌고, 하나를 일시정지했을 때 그 채널만 멈추는지 확인
- 할당량 실측치를 `quota_usage` 테이블에서 확인해 Phase 1에서 세운 예산 가정과 실제가 맞는지 대조
