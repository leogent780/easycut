"""TTS plugin interface: synthesize Korean narration audio via a swappable provider.

Two providers implemented:
- "gemini": uses the same GEMINI_API_KEY already required for gemini_client.py's script
  generation, so no extra account signup is needed to get end-to-end dubbing working.
- "elevenlabs": the user's preferred provider. NOTE — `api.elevenlabs.io` is blocked by this
  dev sandbox's network egress policy, so this implementation is written to the documented
  ElevenLabs REST API shape but has NOT been network-verified from this environment. It should
  work as-is on the user's own machine (no such block there); if it doesn't, check that
  ELEVENLABS_API_KEY is set and that the voice_id is valid for the account.

Both providers return a path to a finished .wav file (16-bit PCM), since downstream ffmpeg
compositing (pipeline/viral_translate_dub.py) expects a plain audio file, not raw bytes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_PROVIDER = "gemini"


def synthesize(text: str, output_path: str | Path, provider: str = DEFAULT_PROVIDER, **kwargs) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "gemini":
        _synthesize_gemini(text, output_path, **kwargs)
    elif provider == "elevenlabs":
        _synthesize_elevenlabs(text, output_path, **kwargs)
    else:
        raise ValueError(f"unknown TTS provider: {provider!r} (expected 'gemini' or 'elevenlabs')")
    return output_path


def _synthesize_gemini(text: str, output_path: Path, voice: str | None = None, model: str | None = None) -> None:
    from shorts_factory.integrations import gemini_client

    kwargs = {}
    if voice:
        kwargs["voice"] = voice
    if model:
        kwargs["model"] = model
    pcm_bytes = gemini_client.synthesize_speech_pcm(text, **kwargs)

    pcm_path = output_path.with_suffix(".pcm")
    pcm_path.write_bytes(pcm_bytes)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", str(pcm_path), str(output_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed converting Gemini TTS PCM to wav: {result.stderr[-2000:]}")
    finally:
        pcm_path.unlink(missing_ok=True)


# A default multilingual voice — override via ELEVENLABS_VOICE_ID or the `voice_id` kwarg.
_ELEVENLABS_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs' stock "Rachel" voice


def _synthesize_elevenlabs(
    text: str,
    output_path: Path,
    voice_id: str | None = None,
    model_id: str = "eleven_multilingual_v2",
) -> None:
    import httpx

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set (see .env.example)")
    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or _ELEVENLABS_DEFAULT_VOICE_ID

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": text, "model_id": model_id},
        )
        resp.raise_for_status()
        mp3_bytes = resp.content

    mp3_path = output_path.with_suffix(".mp3")
    mp3_path.write_bytes(mp3_bytes)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), str(output_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed converting ElevenLabs mp3 to wav: {result.stderr[-2000:]}")
    finally:
        mp3_path.unlink(missing_ok=True)
