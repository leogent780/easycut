"""Thin wrapper around the Gemini API (Files upload + generateContent + TTS).

Used by `pipeline.viral_translate_dub` for the "완성 소스 재구성형" strategy: the user's
explicit requirement is that Gemini itself (not Claude) writes the transcript/script/hook-title,
by actually watching the uploaded reference video — see plan §콘텐츠 소스 전략 4종, strategy 2.

Plain HTTP via httpx (not a `google-generativeai`/`google-genai` SDK dependency) because the
two operations needed here — resumable file upload and generateContent — are simple enough
that a raw REST client avoids pinning to a specific SDK's fast-moving API surface.

Model choice: `gemini-3.5-flash` for text/script generation (validated against this project's
API key — `gemini-3.1-pro-preview` and `gemini-2.5-flash` both failed for this account: free-tier
quota exhausted and model-retired-for-new-users respectively). `gemini-2.5-flash-preview-tts`
for speech synthesis. Both are passed as parameters with these as defaults, not hardcoded only,
since model availability shifts over time and per-account quota.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_TEXT_MODEL = "gemini-3.5-flash"
DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"
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


def generate_json(
    parts: list[dict[str, Any]],
    model: str = DEFAULT_TEXT_MODEL,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Call generateContent with responseMimeType=application/json and return the parsed dict.
    `parts` is the raw Gemini `contents[0].parts` list (text parts and/or `file_data` refs).
    """
    import httpx
    import json as _json

    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(
            f"{_API_BASE}/v1beta/models/{model}:generateContent",
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
        return _json.loads(text)


SCRIPT_SYSTEM_PROMPT = (
    "당신은 쇼츠 영상 편집자입니다. 주어진 영상을 실제로 보고 다음을 수행하세요: "
    "1) 영상 속 음성/화면 자막을 한 글자도 빠짐없이 정확하게 받아쓰기 (transcript_original), "
    "2) 그 내용을 쇼츠 나레이션에 맞는 자연스러운 한국어 구어체 대본으로 재구성 및 번역 "
    "(script_ko, 문장 배열, TTS로 읽었을 때 반드시 target_duration_s 근처(±10%)에 들어오도록 압축), "
    "3) 영상 내용에 어울리는 2줄짜리 상단 후킹 카피(hook_title: line1, line2, emphasis_word — "
    "강조할 단어/구, 노란색으로 표시됨)를 한국어로 작성. JSON으로만 답하세요."
)


def generate_script_from_video(
    video_file_uri: str,
    target_duration_s: float,
    tone_hint: str | None = None,
    model: str = DEFAULT_TEXT_MODEL,
) -> dict[str, Any]:
    """The core "Gemini watches the reference video and writes the script" call.

    Returns {"transcript_original": [...], "script_ko": [...], "hook_title": {...}}.
    `tone_hint` is optional free-text style guidance (e.g. "정중체 대신 스토리텔링형 어미로") —
    it steers HOW the script is written, not what facts it contains.
    """
    instruction = (
        f"목표 나레이션 길이: 약 {target_duration_s:.1f}초 (TTS 자연스러운 속도 기준). "
        + (f"말투 지침: {tone_hint} " if tone_hint else "")
        + 'JSON 형식: {"transcript_original": ["..."], "script_ko": ["..."], '
        '"hook_title": {"line1": "...", "line2": "...", "emphasis_word": "..."}}'
    )
    parts = [
        {"file_data": {"mime_type": "video/mp4", "file_uri": video_file_uri}},
        {"text": SCRIPT_SYSTEM_PROMPT + "\n\n" + instruction},
    ]
    return generate_json(parts, model=model)


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
    """
    import httpx

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{_API_BASE}/v1beta/models/{model}:generateContent",
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
