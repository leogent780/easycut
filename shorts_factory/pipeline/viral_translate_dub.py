"""Strategy 2 (완성 소스 재구성형 / "viral_translate_dub"): given ONE user-supplied reference
video file, produce a Korean-dubbed short end-to-end — this is the codified, reusable version
of the manual process validated by hand across two passes (see
samples/xhs_dish_brush_ko_dub/README.md and .../xhs_dish_brush_ko_dub_v2/README.md).

Deliberately takes a single local video file, not an account/URL to auto-discover from: per
the plan (§레퍼런스 발굴 전략), platforms without an official trending/search API (TikTok,
Instagram, Xiaohongshu) are NOT auto-discovered — a human picks the one reference video and
hands it over, same as they would a YouTube Shorts URL for strategy 2's YouTube-sourced path.

Pipeline steps (see each module for detail):
 1. subtitle_crop.detect_bottom_crop_px — best-effort auto-crop to hide a burned-in caption/watermark
 2. gemini_client.generate_script_from_video — Gemini watches the actual video and writes the
    transcript + Korean script + hook title (per explicit user requirement: Gemini, not this
    pipeline's own text, authors the script)
 3. tts_client.synthesize — narration audio (retries once, compressed, if too long for the source)
 4. silence-gap jump-cut removal + speed-up (ffmpeg silencedetect + dub_timing)
 5. gemini_client.align_sentence_timestamps — sentence-level caption timing, cross-checked
    against real silence data (dub_timing.snap_boundary_to_silences)
 6. dub_caption_render — hook banner + short reference-style caption chunks
 7. final ffmpeg composite

Failure handling follows the plan's stated principle (§실패 처리 원칙): each external call
(Gemini script gen, TTS) gets at most one retry with adjusted parameters; if a step still
fails, the exception propagates with enough context for the caller (dashboard route) to log
it via `state.log_audit` and leave the channel/job in a failed state rather than silently
producing a broken clip.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from shorts_factory.integrations import gemini_client, tts_client
from shorts_factory.pipeline import dub_caption_render, dub_timing, subtitle_crop

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
DEFAULT_SPEED_FACTOR = 1.0  # no video/audio speed-up by default — only silence gaps are trimmed;
# an earlier default of 1.3 (from an early single-video test where the user explicitly asked for
# a faster pace) was carried over inappropriately into this general pipeline and made unrelated
# runs visibly "fast-forwarded" with a much shorter total runtime than the source content
# warranted. Pass speed_factor explicitly when a faster pace really is wanted.
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DURATION_S = 0.15
LENGTH_OVERAGE_RETRY_THRESHOLD = 1.15  # re-request a shorter script if TTS runs >15% over target


@dataclass
class DubResult:
    output_path: Path
    hook_title: dict
    script_ko: list[str]
    transcript_original: list[str]
    crop_applied_px: int
    final_duration_s: float
    warnings: list[str] = field(default_factory=list)


def _run_ffmpeg(args: list[str]) -> str:
    result = subprocess.run(["ffmpeg", "-y", "-nostdin", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}):\n{result.stderr[-4000:]}")
    return result.stderr


def _probe(video_path: str | Path) -> tuple[int, int, float]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-show_entries", "format=duration", "-of", "json", str(video_path)],
        capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"]), float(data["format"]["duration"])


def _audio_duration(path: str | Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def _build_crop_scale_filter(source_w: int, source_h: int, keep_height_px: int) -> str:
    """crop-and-rescale: keep the top `keep_height_px` rows (0 = no crop needed), center-crop
    horizontally to the 9:16 target aspect, then scale to the canvas resolution. This is the
    same "crop out the bad region, rescale the rest to fill" technique used for both hiding a
    burned-in caption band AND correcting aspect ratio in one pass.
    """
    target_aspect = CANVAS_WIDTH / CANVAS_HEIGHT
    keep_h = keep_height_px if keep_height_px > 0 else source_h
    crop_w = min(source_w, round(keep_h * target_aspect))
    x_off = (source_w - crop_w) // 2
    return f"crop={crop_w}:{keep_h}:{x_off}:0,scale={CANVAS_WIDTH}:{CANVAS_HEIGHT},setsar=1"


def _detect_silences(audio_path: str | Path) -> list[tuple[float, float]]:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(audio_path), "-af",
         f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_DURATION_S}", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return dub_timing.parse_silence_intervals(result.stderr)


def _extract_and_concat_keep_segments(source_video: Path, keep_segments: list[tuple[float, float]], scratch_dir: Path) -> Path:
    seg_paths = []
    for i, (a, b) in enumerate(keep_segments, start=1):
        seg_path = scratch_dir / f"seg_{i:03d}.mp4"
        _run_ffmpeg([
            "-i", str(source_video), "-ss", str(a), "-to", str(b),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-avoid_negative_ts", "make_zero",
            str(seg_path),
        ])
        seg_paths.append(seg_path)

    concat_list = scratch_dir / "concat_list.txt"
    concat_list.write_text("".join(f"file '{p}'\n" for p in seg_paths))
    gapless_path = scratch_dir / "gapless.mp4"
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(gapless_path)])
    return gapless_path


def _real_segment_durations(scratch_dir: Path, count: int) -> list[float]:
    return [_audio_duration(scratch_dir / f"seg_{i:03d}.mp4") for i in range(1, count + 1)]


def run_manual(
    reference_video_path: str | Path,
    pretendard_font_path: str | Path,
    dohyeon_font_path: str | Path,
    output_path: str | Path,
    tone_hint: str | None = None,
    speed_factor: float = DEFAULT_SPEED_FACTOR,
    tts_provider: str = tts_client.DEFAULT_PROVIDER,
    scratch_dir: str | Path | None = None,
    chromium_executable_path: str | None = None,
) -> DubResult:
    reference_video_path = Path(reference_video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    own_scratch = scratch_dir is None
    scratch_ctx = tempfile.TemporaryDirectory() if own_scratch else None
    scratch_dir = Path(scratch_ctx.name) if own_scratch else Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. subtitle/watermark crop detection
        source_w, source_h, source_duration = _probe(reference_video_path)
        keep_height_px = subtitle_crop.detect_bottom_crop_px(reference_video_path)
        crop_filter = _build_crop_scale_filter(source_w, source_h, keep_height_px)

        # 2. Gemini watches the video, writes transcript + Korean script + hook title
        video_file_uri = gemini_client.upload_file(reference_video_path, "video/mp4")
        gemini_client.wait_for_file_active(video_file_uri)
        script_result = gemini_client.generate_script_from_video(video_file_uri, source_duration, tone_hint=tone_hint)
        script_ko: list[str] = script_result["script_ko"]
        hook_title: dict = script_result["hook_title"]
        transcript_original: list[str] = script_result.get("transcript_original", [])

        # 3. TTS, with one length-correcting retry if it overshoots the source duration a lot
        narration_path = scratch_dir / "narration.wav"
        tts_client.synthesize(" ".join(script_ko), narration_path, provider=tts_provider)
        narration_duration = _audio_duration(narration_path)
        if narration_duration > source_duration * LENGTH_OVERAGE_RETRY_THRESHOLD:
            warnings.append(
                f"1차 대본 낭독 시 {narration_duration:.1f}초로 원본({source_duration:.1f}초)보다 길어 압축 재요청함"
            )
            compress_hint = (
                (tone_hint + " " if tone_hint else "")
                + f"TTS로 읽었을 때 반드시 {source_duration:.1f}초 이내(가능하면 그보다 짧게)로 압축해줘."
            )
            script_result = gemini_client.generate_script_from_video(video_file_uri, source_duration, tone_hint=compress_hint)
            script_ko = script_result["script_ko"]
            hook_title = script_result["hook_title"]
            tts_client.synthesize(" ".join(script_ko), narration_path, provider=tts_provider)
            narration_duration = _audio_duration(narration_path)

        # 4. base composite: crop/scale + hook banner burned in + narration audio
        banner_path = scratch_dir / "banner.png"
        dub_caption_render.render_hook_banner(
            hook_title["line1"], hook_title["line2"], hook_title.get("emphasis_word"),
            pretendard_font_path, banner_path, chromium_executable_path=chromium_executable_path,
        )
        base_composite = scratch_dir / "base_composite.mp4"
        _run_ffmpeg([
            "-i", str(reference_video_path), "-i", str(narration_path), "-i", str(banner_path),
            "-filter_complex", f"[0:v]{crop_filter}[bg];[bg][2:v]overlay=0:0[outv]",
            "-map", "[outv]", "-map", "1:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(base_composite),
        ])

        # 5. remove inter-sentence silence gaps (jump cut) + speed up
        silences = _detect_silences(narration_path)
        keep_segments = dub_timing.compute_keep_segments(silences, narration_duration)
        gapless_path = _extract_and_concat_keep_segments(base_composite, keep_segments, scratch_dir)

        sped_up_path = scratch_dir / "sped_up.mp4"
        _run_ffmpeg([
            "-i", str(gapless_path),
            "-filter_complex", f"[0:v]setpts=PTS/{speed_factor}[v];[0:a]atempo={speed_factor}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(sped_up_path),
        ])
        final_duration = _audio_duration(sped_up_path)

        # 6. sentence-level caption timing: Gemini estimate, snapped to real silence data, then
        # remapped through the same gap-removal + speed transform applied to the video/audio
        narration_file_uri = gemini_client.upload_file(narration_path, "audio/wav")
        gemini_client.wait_for_file_active(narration_file_uri)
        segments = gemini_client.align_sentence_timestamps(narration_file_uri, script_ko)

        boundaries: dict[str, float] = {"s0_start": 0.0}
        for seg in segments:
            idx = seg["index"]
            boundaries[f"s{idx}_end"] = dub_timing.snap_boundary_to_silences(seg["end"], silences)
        mapped = dub_timing.compute_gapless_speed_mapping(boundaries, keep_segments, speed_factor)

        sentence_windows: list[tuple[float, float]] = []
        prev_end = mapped["s0_start"]
        for i in range(1, len(script_ko) + 1):
            end = mapped.get(f"s{i}_end", final_duration)
            sentence_windows.append((prev_end, end))
            prev_end = end

        # 7. short reference-style caption chunks, rendered + composited on top
        chunks = dub_timing.layout_caption_chunks(script_ko, sentence_windows)
        overlay_inputs = []
        filter_parts = []
        prev_label = "0:v"
        for i, chunk in enumerate(chunks, start=1):
            chunk_png = scratch_dir / f"chunk_{i:03d}.png"
            dub_caption_render.render_caption_chunk(
                chunk.text, dohyeon_font_path, chunk_png, chromium_executable_path=chromium_executable_path
            )
            overlay_inputs += ["-i", str(chunk_png)]
            out_label = f"v{i}"
            filter_parts.append(
                f"[{prev_label}][{i}:v]overlay=0:0:enable='between(t,{chunk.start_s:.3f},{chunk.end_s:.3f})'[{out_label}]"
            )
            prev_label = out_label

        _run_ffmpeg([
            "-i", str(sped_up_path), *overlay_inputs,
            "-filter_complex", "; ".join(filter_parts),
            "-map", f"[{prev_label}]", "-map", "0:a",
            "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart",
            str(output_path),
        ])

        return DubResult(
            output_path=output_path,
            hook_title=hook_title,
            script_ko=script_ko,
            transcript_original=transcript_original,
            crop_applied_px=keep_height_px,
            final_duration_s=final_duration,
            warnings=warnings,
        )
    finally:
        if scratch_ctx is not None:
            scratch_ctx.cleanup()
