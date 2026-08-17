"""Thin wrapper around the Gemini API (Files upload + generateContent + TTS).

Used by `pipeline.viral_translate_dub` for the "완성 소스 재구성형" strategy: the user's
explicit requirement is that Gemini itself (not Claude) writes the transcript/script/hook-title,
by actually watching the uploaded reference video — see plan §콘텐츠 소스 전략 4종, strategy 2.

Plain HTTP via httpx (not a `google-generativeai`/`google-genai` SDK dependency) because the
two operations needed here — resumable file upload and generateContent — are simple enough
that a raw REST client avoids pinning to a specific SDK's fast-moving API surface.

Model choice: `gemini-flash-latest` for text/script generation. Each Gemini model has its own
separate free-tier quota bucket (observed: 20 requests/day/model) — `gemini-3.5-flash`,
`gemini-3.1-pro-preview`, and `gemini-2.5-flash` were all tried first and each failed for this
account (daily quota exhausted, daily quota exhausted, and model-retired-for-new-users,
respectively). `gemini-2.5-flash-preview-tts` for speech synthesis. All are passed as
parameters with these as defaults, not hardcoded only, since model availability and per-account
quota shift over time — if the default model's daily quota runs out mid-session, pass a
different `model=` (e.g. `gemini-3-flash-preview`, `gemini-flash-lite-latest`) to keep working
without waiting for a reset.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_TEXT_MODEL = "gemini-flash-latest"
DEFAULT_TTS_MODEL = "gemini-3.1-flash-tts-preview"
# Same free-tier per-model daily quota issue as DEFAULT_TEXT_MODEL — tried in order if the
# primary TTS model's daily quota is exhausted (each model has its own separate quota bucket).
FALLBACK_TTS_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"]
DEFAULT_TTS_VOICE = "Kore"

_API_BASE = "https://generativelanguage.googleapis.com"


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
    return key


def upload_file(path: str | Path, mime_type: str, display_name: str | None = None, timeout_s: float = 120.0) -> str:
    """Resumable-upload a local file to the Gemini Files API. Returns the file's `uri`
    (used as `file_data.file_uri` in a subsequent generateContent call).
    """
    import httpx

    path = Path(path)
    num_bytes = path.stat().st_size
    display_name = display_name or path.stem

    with httpx.Client(timeout=timeout_s) as client:
        start_resp = client.post(
            f"{_API_BASE}/upload/v1beta/files",
            params={"key": _api_key()},
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(num_bytes),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": display_name}},
        )
        start_resp.raise_for_status()
        upload_url = start_resp.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError(f"Gemini upload did not return an upload URL: {start_resp.text[:500]}")

        with path.open("rb") as f:
            upload_resp = client.post(
                upload_url,
                headers={
                    "Content-Length": str(num_bytes),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                },
                content=f.read(),
            )
        upload_resp.raise_for_status()
        data = upload_resp.json()
        return data["file"]["uri"]


def wait_for_file_active(file_uri: str, timeout_s: float = 60.0, poll_interval_s: float = 2.0) -> None:
    """Poll a just-uploaded file until Gemini finishes processing it (state ACTIVE).
    Large video files can take a few seconds to move past PROCESSING.
    """
    import httpx

    name = file_uri.rsplit("/v1beta/", 1)[-1]  # "files/xxxxx"
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() < deadline:
            resp = client.get(f"{_API_BASE}/v1beta/{name}", params={"key": _api_key()})
            resp.raise_for_status()
            state = resp.json().get("state")
            if state == "ACTIVE":
                return
            if state == "FAILED":
                raise RuntimeError(f"Gemini file processing failed for {file_uri}")
            time.sleep(poll_interval_s)
    raise TimeoutError(f"Gemini file {file_uri} did not become ACTIVE within {timeout_s}s")


TRANSIENT_RETRY_BACKOFFS_S = [3, 8, 20]

# Each Gemini model has its own separate free-tier daily quota (observed: 20 requests/day/model
# on this account). A single long multi-clip run can burn through one model's quota mid-session,
# so generate_json rotates to the next model here on a 429 RESOURCE_EXHAUSTED rather than
# failing outright — the caller's chosen `model` is tried first, then these in order.
TEXT_MODEL_FALLBACK_CHAIN = [
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
]


def generate_json(
    parts: list[dict[str, Any]],
    model: str = DEFAULT_TEXT_MODEL,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Call generateContent with responseMimeType=application/json and return the parsed dict.
    `parts` is the raw Gemini `contents[0].parts` list (text parts and/or `file_data` refs).

    Transient server-side errors (503 Service Unavailable, 500, and empirically also 400 — a
    video-attached request has been observed to spuriously 400 once and then succeed identically
    on retry, likely a Files API state hiccup rather than a genuinely malformed request) are
    retried automatically with backoff on the SAME model. A 429 (RESOURCE_EXHAUSTED — daily
    quota) is different: retrying the same model won't help, so instead this rotates to the next
    model in TEXT_MODEL_FALLBACK_CHAIN (deduplicated, `model` tried first).
    """
    import httpx
    import json as _json

    models_to_try = [model] + [m for m in TEXT_MODEL_FALLBACK_CHAIN if m != model]

    last_exc: Exception | None = None
    for candidate_model in models_to_try:
        for attempt, backoff_s in enumerate([0.0, *TRANSIENT_RETRY_BACKOFFS_S]):
            if backoff_s:
                time.sleep(backoff_s)
            try:
                with httpx.Client(timeout=timeout_s) as client:
                    resp = client.post(
                        f"{_API_BASE}/v1beta/models/{candidate_model}:generateContent",
                        params={"key": _api_key()},
                        json={
                            "contents": [{"parts": parts}],
                            "generationConfig": {"responseMimeType": "application/json"},
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        raise RuntimeError(f"Gemini returned no candidates: {data}")
                    text = candidates[0]["content"]["parts"][0]["text"]
                    return _parse_json_response(text)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in (400, 500, 503) and attempt < len(TRANSIENT_RETRY_BACKOFFS_S):
                    continue  # retry same model
                if exc.response.status_code == 429:
                    break  # quota exhausted on this model — move to next model
                raise
    raise last_exc  # exhausted all retries across all fallback models


def _parse_json_response(text: str) -> dict[str, Any]:
    """Gemini's JSON-mode output is usually clean, but occasionally wraps the object in a
    markdown code fence or appends trailing commentary despite responseMimeType being set.
    Strip a fence if present, then decode only the first JSON value and ignore anything after
    it, rather than failing outright on trailing bytes.
    """
    import json as _json

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()

    decoder = _json.JSONDecoder()
    obj, _end_index = decoder.raw_decode(stripped)
    return obj


SCRIPT_SYSTEM_PROMPT = (
    "당신은 쇼츠 영상 편집자입니다. 주어진 영상을 실제로 보고 다음을 수행하세요: "
    "1) 영상 속 음성/화면 자막을 한 글자도 빠짐없이 정확하게 받아쓰기 (transcript_original), "
    "2) 그 내용을 쇼츠 나레이션에 맞는 한국어 구어체 대본으로 재구성 및 번역 (script_ko, 문장 배열). "
    "아래에 실제 참고 대본 예시들이 첨부되어 있습니다 — 이 예시들은 사용자가 직접 준 진짜 레퍼런스이며, "
    "제가 임의로 요약한 지침이 아니라 예시 그 자체이니 이 대본들의 말투, 어미, 문장 길이, 전환 방식, "
    "템포를 최대한 그대로 재현하세요 (내용/사실관계는 당연히 지금 영상에 맞게 새로 써야 합니다). "
    "영상에 제품이 여러 개 등장하면 각 제품마다 이 흐름(등장 → 반전 포인트 → 핵심 기능)을 짧게 반복하되 "
    "자연스럽게 이어붙이고, 대본은 절대 문장이 끊기거나 애매하게 멈추면 안 되며 명확한 마무리 문장(예: "
    "이런 사람들에게 필수/추천이라는 식의 끝맺음)으로 끝나야 합니다. "
    "대본에 '중국', '중국 주부', '중국에서' 처럼 원본 영상이 어느 나라 것인지 밝히는 표현은 절대 넣지 "
    "마세요 — 원본 영상의 출처국가는 언급하지 말고, '최근 SNS에서' 같은 밋밋하고 뻔한 오프닝도 쓰지 "
    "마세요. 대신 첫 문장은 '다이소 가면 무조건 사야 한다는데'처럼 '다이소'라는 구체적인 장소 키워드를 "
    "넣어서, 어디서 발견됐는지가 아니라 지금 당장 왜 사야 하는지에 초점을 맞춘 후킹 문구로 시작하세요. "
    "TTS로 읽었을 때 반드시 target_duration_s 근처(±10%)에 들어오도록 압축하되, 압축하느라 마무리를 "
    "생략하지 마세요 — 마무리 문장을 위한 여유를 항상 남겨두세요. "
    "3) 영상 내용에 어울리는 2줄짜리 상단 후킹 카피(hook_title: line1, line2, emphasis_word — "
    "강조할 단어/구, 노란색으로 표시됨)를 한국어로 작성. JSON으로만 답하세요."
)

# The user's own reference scripts, included VERBATIM (not paraphrased/summarized by this
# codebase) so Gemini can pattern-match the actual tone/cadence/transition-phrase style itself
# rather than trust a secondhand description of it. The 4th example is left with its original
# [MM:SS] timestamps intact — the user gave it specifically so Gemini could gauge how much
# script LENGTH fits how many seconds of narration, not just the wording style.
SCRIPT_STYLE_REFERENCE_EXAMPLES = """
예시 1)
단순한 기능 추가로 떼돈 번 천재의 발명품

최근 누구나 텀블러 하나는 무조건적으로 챙겨 다니는 시대에

평범한 이 텀블러가 전 세계 SNS 플랫폼에서 정신 나갈 듯이 바이럴이 폭발하며 논란이라는데

이건 바로 맥세이프 텀블러

이게 말도 안 되는 게 단순히 거치대로 사용해도 편리할 뿐만 아니라

들고 다닐 때는 손잡이 역할도 해 버리는데

심지어 차량 컵홀더에 끼워주면 딱 내비를 보기 좋은 각도가 되어서 차량 거치대를 살 필요가 없다는 거

근데 진짜 충격적인 포인트는 헬스장에서 러닝머신과 사이클의 컵홀더에 끼워주면 완벽한 유튜브 시청 각도를 제공해 주는 데다가

자연스러운 촬영 삼각대 역할을 해줘서 운동인들에게는 필수템이라고

예시 2)
미국 천재가 만들어 떼돈 번 제품의 정체

최근 딱 봤을 때는 평범한 이 컵홀더가

미국을 넘어 한국 SNS에서까지 바이럴이 대폭발하며 이걸 개발한 미국의 한 천재가 돈방석에 앉았다는데

이건 바로 냉온 컵홀더

이게 말도 안 되는 게 온도를 유지만 시켜 주던 기존 컵홀더와는 달리

버튼 한번 딸깍으로 최대 영하 3도까지 떨어뜨려 냉장고보다 더 시원하게 음료수를 차갑게 만들어 준다는 거

근데 진짜 충격적인 포인트는 최대 60도까지 음료를 뜨겁게 데워주는 기능까지 있어 차를 가진 사람들에게는 인생에 둘도 없는 제품이라고

예시 3)
악마들을 막아준 미국 천재의 발명품

최근 딱 봤을 때는 슬라임 같은 이 젤이

미국을 넘어 한국 SNS에서까지 바이럴이 폭발하며 이걸 개발한 미국의 한 천재가 떼돈을 벌었다는데

이건 바로 뮤지엄 젤

이게 말도 안 되는 게 원래는 박물관에서 전시물들을 아무런 티가 나지 않게 고정시키는 용도로 개발되었지만

한 주부가 집에서 활용해 보니 인테리어 미감적으로 깔끔하게 각종 물건 및 소품들을 뒤집어도 떨어지지 않을 만큼 완벽하게 고정해 버렸다는 거

심지어 제거를 할 때도 잔여물이 하나도 남지 않아 전세집에서는 필수템이라는데

진짜 충격적인 포인트는 특히 물건을 떨어뜨리는 걸 즐기는 악마를 키우는 반려 가정들이 이 제품 하나로 수백만 원을 아껴 버렸다고

예시 4) (아래 [MM:SS]는 실제 이 대본이 낭독되는 시점입니다 — 몇 초 동안 어느 정도 분량의 대본이 들어가는지 템포 참고용입니다. 새로 쓰는 대본에 타임스탬프를 넣으라는 뜻이 아닙니다.)
[00:00] 미국 제조사도 당황한 천재 주부의 활용법
[00:02] 최근 이 평범한 빗자루 걸이용 후크로 나온 미국 제품을 활용한 천재 주부의 활용법이
[00:05] 국내를 넘어 해외 SNS에서까지 바이럴이 폭발하며 논란이라는데
[00:09] 바로 이 홀더의 말도 안 되는 그립력과 흡착력을 이용해
[00:12] 집안에 있는 온갖 청소 도구들부터 모든 물건을 벽에 걸어 수납 공간을 적극 활용해 버리는 데다가
[00:16] 심지어 창틀 위에 붙인 다음 커튼봉을 끼워 무타공으로 커튼 설치까지 해 버렸다는 거
[00:20] 근데 진짜 충격적인 포인트는 벽에서 떼어내도 아무런 자국도 남지 않아
[00:23] 특히 전세집에 특화된 제품이라고
""".strip()


def generate_script_from_video(
    video_file_uri: str,
    target_duration_s: float,
    tone_hint: str | None = None,
    model: str = DEFAULT_TEXT_MODEL,
) -> dict[str, Any]:
    """The core "Gemini watches the reference video and writes the script" call.

    Returns {"transcript_original": [...], "script_ko": [...], "hook_title": {...}}.
    `tone_hint` is optional free-text style guidance (e.g. "정중체 대신 스토리텔링형 어미로") —
    it steers HOW the script is written, not what facts it contains. The user's own reference
    scripts (SCRIPT_STYLE_REFERENCE_EXAMPLES) are always attached verbatim alongside this, so
    Gemini judges the tone from the real examples itself rather than a paraphrase of them.
    """
    instruction = (
        f"목표 나레이션 길이: 약 {target_duration_s:.1f}초 (TTS 자연스러운 속도 기준). "
        + (f"말투 지침: {tone_hint} " if tone_hint else "")
        + 'JSON 형식: {"transcript_original": ["..."], "script_ko": ["..."], '
        '"hook_title": {"line1": "...", "line2": "...", "emphasis_word": "..."}}'
    )
    parts = [
        {"file_data": {"mime_type": "video/mp4", "file_uri": video_file_uri}},
        {"text": SCRIPT_SYSTEM_PROMPT + "\n\n실제 참고 대본 예시:\n" + SCRIPT_STYLE_REFERENCE_EXAMPLES + "\n\n" + instruction},
    ]
    return generate_json(parts, model=model)


SEGMENT_SYSTEM_PROMPT = (
    "당신은 쇼츠 영상 편집자입니다. 이 영상은 여러 개의 제품/꿀템을 순서대로 소개하는 "
    "컴필레이션 영상입니다. 영상을 실제로 보고, 서로 다른 제품을 소개하는 구간을 구분해서 "
    "각 구간의 시작 시각과 끝 시각(초, 소수점 포함), 그 제품을 한 줄로 요약한 라벨을 "
    "알려주세요. 한국 유튜브 쇼츠는 각 영상이 1분 이내여야 하므로, 이 구분은 나중에 영상을 "
    "제품별로 잘라서 각각 별도의 쇼츠로 만드는 데 쓰입니다. 인트로/아웃트로처럼 특정 제품과 "
    "무관한 구간이 있다면 segments에 포함하지 마세요."
)


def detect_product_segments(video_file_uri: str, model: str = DEFAULT_TEXT_MODEL) -> list[dict[str, Any]]:
    """Ask Gemini to watch a multi-product compilation reference video and split it into
    per-product time segments, each destined to become its own separate Shorts clip (Korean
    YouTube Shorts requires <=60s per video, and simply trimming a multi-product video to fit
    would cut mid-product — the actual requirement is one clip per product).

    Returns a list of {"label": str, "start": float, "end": float} dicts, in video order.
    """
    parts = [
        {"file_data": {"mime_type": "video/mp4", "file_uri": video_file_uri}},
        {
            "text": SEGMENT_SYSTEM_PROMPT
            + '\n\nJSON 형식: {"segments": [{"label": "...", "start": 0.0, "end": 0.0}, ...]}'
        },
    ]
    result = generate_json(parts, model=model)
    return result["segments"]


def align_sentence_timestamps(
    audio_file_uri: str,
    sentences: list[str],
    model: str = DEFAULT_TEXT_MODEL,
) -> list[dict[str, float]]:
    """Ask Gemini to listen to a TTS narration file and estimate start/end seconds for each
    of `sentences` (already known, in order). Gemini's audio timestamps are approximate —
    callers should cross-check against real `ffmpeg silencedetect` output rather than trust
    these numbers directly (see pipeline.dub_timing.snap_boundaries_to_silences).
    """
    numbered = "\n".join(f"{i+1}. \"{s}\"" for i, s in enumerate(sentences))
    instruction = (
        "이 오디오는 아래 문장들을 순서대로 읽은 TTS 음성입니다. 각 문장이 시작하는 시각과 "
        "끝나는 시각(초, 소수점 포함)을 최대한 정확히 들어보고 알려주세요.\n\n"
        f"{numbered}\n\n"
        'JSON: {"segments": [{"index": 1, "start": 0.0, "end": 0.0}, ...]}'
    )
    parts = [
        {"file_data": {"mime_type": "audio/wav", "file_uri": audio_file_uri}},
        {"text": instruction},
    ]
    result = generate_json(parts, model=model)
    return result["segments"]


def synthesize_speech_pcm(text: str, voice: str = DEFAULT_TTS_VOICE, model: str = DEFAULT_TTS_MODEL) -> bytes:
    """Gemini TTS: returns raw PCM bytes (signed 16-bit little-endian, mono, 24kHz).
    Caller is responsible for wrapping into a WAV container (see tts_client.synthesize).

    Same retry/fallback strategy as generate_json: transient 400/500/503s retry on the same
    model, a 429 (daily quota exhausted) rotates to the next model in FALLBACK_TTS_MODELS.
    """
    import httpx

    models_to_try = [model] + [m for m in FALLBACK_TTS_MODELS if m != model]
    last_exc: Exception | None = None
    for candidate_model in models_to_try:
        for attempt, backoff_s in enumerate([0.0, *TRANSIENT_RETRY_BACKOFFS_S]):
            if backoff_s:
                time.sleep(backoff_s)
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(
                        f"{_API_BASE}/v1beta/models/{candidate_model}:generateContent",
                        params={"key": _api_key()},
                        json={
                            "contents": [{"parts": [{"text": text}]}],
                            "generationConfig": {
                                "responseModalities": ["AUDIO"],
                                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
                            },
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    part = data["candidates"][0]["content"]["parts"][0]
                    return base64.b64decode(part["inlineData"]["data"])
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in (400, 500, 503) and attempt < len(TRANSIENT_RETRY_BACKOFFS_S):
                    continue
                if exc.response.status_code == 429:
                    break
                raise
    raise last_exc
