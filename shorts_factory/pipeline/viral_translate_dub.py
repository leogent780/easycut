"""Strategy 2 (완성 소스 재구성형 / "viral_translate_dub"): given ONE user-supplied reference
video file, produce a Korean-dubbed short end-to-end — this is the codified, reusable version
of the manual process validated by hand across two passes (see
samples/xhs_dish_brush_ko_dub/README.md and .../xhs_dish_brush_ko_dub_v2/README.md).

Deliberately takes a single local video file, not an account/URL to auto-discover from: per
the plan (§레퍼런스 발굴 전략), platforms without an official trending/search API (TikTok,
Instagram, Xiaohongshu) are NOT auto-discovered — a human picks the one reference video and
hands it over, same as they would a YouTube Shorts URL for strategy 2's YouTube-sourced path.

Two entry points share one per-segment worker (`_dub_segment`):
 - `run_manual` — the whole reference video is one Shorts (single product/topic).
 - `run_manual_multi_segment` — the reference video is a multi-product compilation. Each
   product gets its OWN Gemini script + TTS + gap-removal pass, scoped to just that product's
   sub-clip and its own real duration, then the per-product clips are concatenated and a single
   shared hook banner is overlaid across the whole thing. This is what actually keeps narration
   in sync with what's on screen — a single Gemini call over the whole multi-product clip has
   no way to guarantee sentence N's audio lines up with product N's footage, since the two are
   paced completely independently (the LLM's Korean phrasing tempo vs. the original footage's
   edit tempo). Splitting by product and generating/pacing each independently removes the
   possibility of drift entirely, at the cost of one Gemini+TTS pass per product instead of one
   for the whole clip.

Per-segment worker steps (see each module for detail):
 1. subtitle_crop.detect_bottom_crop_px — best-effort auto-crop to hide a burned-in caption/watermark
    (detected ONCE on the full reference video and reused for every product sub-clip, since the
    source recording's caption/watermark position doesn't change mid-video)
 2. gemini_client.generate_script_from_video — Gemini watches the actual (sub-)clip and writes the
    transcript + Korean script + hook title (per explicit user requirement: Gemini, not this
    pipeline's own text, authors the script)
 3. tts_client.synthesize — narration audio (retries once, compressed, if too long for the source)
 4. base composite (crop/scale + narration audio [+ hook banner if a single-segment run]) — if the
    (possibly still-long) narration runs past the available footage, the last frame is frozen
    (ffmpeg `tpad`) to cover the extra audio rather than silently truncating the closing line
 5. silence-gap jump-cut removal + speed-up (ffmpeg silencedetect + dub_timing)
 6. gemini_client.align_sentence_timestamps — sentence-level caption timing, cross-checked
    against real silence data (dub_timing.snap_boundary_to_silences)
 7. dub_caption_render — short reference-style caption chunks (hook banner only for single-segment
    runs; multi-segment runs add ONE shared banner across the final concatenated video instead)

Failure handling follows the plan's stated principle (§실패 처리 원칙): each external call
(Gemini script gen, TTS) gets at most one retry with adjusted parameters; if a step still
fails, the exception propagates with enough context for the caller (dashboard route) to log
it via `state.log_audit` and leave the channel/job in a failed state rather than silently
producing a broken clip.
"""

from __future__ import annotations

import json
import shutil
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


@dataclass
class _SegmentDubResult:
    captioned_path: Path
    hook_title: dict
    script_ko: list[str]
    transcript_original: list[str]
    warnings: list[str]


def _dub_segment(
    segment_video_path: Path,
    keep_height_px: int,
    pretendard_font_path: str | Path,
    dohyeon_font_path: str | Path,
    scratch_dir: Path,
    tone_hint: str | None,
    speed_factor: float,
    tts_provider: str,
    chromium_executable_path: str | None,
    add_banner: bool,
) -> _SegmentDubResult:
    """Run the full script->TTS->gap-removal->caption pipeline on ONE video clip (either the
    whole reference video, for a single-topic run, or one product's sub-clip, for a
    multi-segment run). `add_banner` controls whether the hook banner is burned in here
    (single-segment runs) or left for the caller to composite once across a concatenated
    multi-segment output.
    """
    warnings: list[str] = []
    source_w, source_h, source_duration = _probe(segment_video_path)
    crop_filter = _build_crop_scale_filter(source_w, source_h, keep_height_px)

    video_file_uri = gemini_client.upload_file(segment_video_path, "video/mp4")
    gemini_client.wait_for_file_active(video_file_uri)
    script_result = gemini_client.generate_script_from_video(video_file_uri, source_duration, tone_hint=tone_hint)
    script_ko: list[str] = script_result["script_ko"]
    hook_title: dict = script_result["hook_title"]
    transcript_original: list[str] = script_result.get("transcript_original", [])

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

    # If the narration STILL runs past the available footage, extend the video by freezing its
    # last frame (ffmpeg tpad) instead of letting the mux silently truncate the closing line —
    # a frozen last frame during the sign-off reads far better than the video just stopping
    # mid-sentence.
    extension_s = max(0.0, narration_duration - source_duration)
    extend_suffix = f",tpad=stop_mode=clone:stop_duration={extension_s:.3f}" if extension_s > 0 else ""
    if extension_s > 0:
        warnings.append(
            f"나레이션({narration_duration:.1f}초)이 소스 영상({source_duration:.1f}초)보다 길어 "
            f"마지막 프레임을 {extension_s:.1f}초 정지시켜 늘림"
        )

    banner_path = scratch_dir / "banner.png"
    base_composite = scratch_dir / "base_composite.mp4"
    if add_banner:
        dub_caption_render.render_hook_banner(
            hook_title["line1"], hook_title["line2"], hook_title.get("emphasis_word"),
            pretendard_font_path, banner_path, chromium_executable_path=chromium_executable_path,
        )
        _run_ffmpeg([
            "-i", str(segment_video_path), "-i", str(narration_path), "-i", str(banner_path),
            "-filter_complex", f"[0:v]{crop_filter}{extend_suffix}[bg];[bg][2:v]overlay=0:0[outv]",
            "-map", "[outv]", "-map", "1:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(base_composite),
        ])
    else:
        _run_ffmpeg([
            "-i", str(segment_video_path), "-i", str(narration_path),
            "-filter_complex", f"[0:v]{crop_filter}{extend_suffix}[outv]",
            "-map", "[outv]", "-map", "1:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(base_composite),
        ])

    # remove inter-sentence silence gaps (jump cut) + speed up
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

    # sentence-level caption timing: Gemini estimate, snapped to real silence data, then
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

    # short reference-style caption chunks, rendered + composited on top
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

    captioned_path = scratch_dir / "captioned.mp4"
    if filter_parts:
        _run_ffmpeg([
            "-i", str(sped_up_path), *overlay_inputs,
            "-filter_complex", "; ".join(filter_parts),
            "-map", f"[{prev_label}]", "-map", "0:a",
            "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart",
            str(captioned_path),
        ])
    else:
        shutil.copy2(sped_up_path, captioned_path)

    return _SegmentDubResult(
        captioned_path=captioned_path,
        hook_title=hook_title,
        script_ko=script_ko,
        transcript_original=transcript_original,
        warnings=warnings,
    )


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
    """Single-topic reference video -> one Korean-dubbed Shorts clip."""
    reference_video_path = Path(reference_video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    own_scratch = scratch_dir is None
    scratch_ctx = tempfile.TemporaryDirectory() if own_scratch else None
    scratch_dir = Path(scratch_ctx.name) if own_scratch else Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        keep_height_px = subtitle_crop.detect_bottom_crop_px(reference_video_path)
        result = _dub_segment(
            reference_video_path, keep_height_px, pretendard_font_path, dohyeon_font_path,
            scratch_dir, tone_hint, speed_factor, tts_provider, chromium_executable_path,
            add_banner=True,
        )
        final_duration = _audio_duration(result.captioned_path)
        shutil.copy2(result.captioned_path, output_path)
        return DubResult(
            output_path=output_path,
            hook_title=result.hook_title,
            script_ko=result.script_ko,
            transcript_original=result.transcript_original,
            crop_applied_px=keep_height_px,
            final_duration_s=final_duration,
            warnings=result.warnings,
        )
    finally:
        if scratch_ctx is not None:
            scratch_ctx.cleanup()


_MULTI_SEGMENT_ROLE_HINTS = {
    "first": (
        "이 영상은 여러 제품을 연달아 소개하는 영상의 첫 번째 제품 구간입니다. 자연스럽게 이 "
        "제품을 소개하되, 아직 전체 영상이 끝나는 게 아니므로 마무리 추천 문구는 넣지 마세요. "
        "마지막 문장은 다음 제품으로 자연스럽게 넘어갈 여지를 남기고, 문장이 뚝 끊기지 않고 "
        "자연스럽게 맺어져야 합니다."
    ),
    "middle": (
        "이 영상은 여러 제품을 연달아 소개하는 영상 중간의 한 제품 구간입니다. '다음은' 같은 "
        "전환구로 자연스럽게 시작해 이 제품을 소개하되, 아직 전체 영상이 끝나는 게 아니므로 "
        "마무리 추천 문구는 넣지 마세요. 마지막 문장은 다음 제품으로 자연스럽게 넘어갈 여지를 "
        "남기고 문장이 뚝 끊기지 않아야 합니다."
    ),
    "last": (
        "이 영상은 여러 제품을 연달아 소개하는 영상의 마지막 제품 구간입니다. '다음은' 같은 "
        "전환구로 자연스럽게 시작해 이 제품을 소개하고, 끝에는 전체 영상을 마무리하는 확실한 "
        "끝맺음 문장(예: 이런 사람들에게 필수/추천이라는 식)으로 끝내세요."
    ),
}


def _segment_role(index: int, count: int) -> str:
    if count == 1:
        return "last"  # shouldn't normally happen (multi-segment implies >=2), but be safe
    if index == 0:
        return "first"
    if index == count - 1:
        return "last"
    return "middle"


def run_manual_multi_segment(
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
    """Multi-product reference video -> one Korean-dubbed Shorts clip covering ALL products,
    with each product's narration generated, TTS'd, and gap-removed independently against its
    OWN sub-clip and OWN real duration (via `gemini_client.detect_product_segments`), then
    concatenated with a single shared hook banner over the whole thing.

    This is what keeps the narration in sync with what's on screen: a single Gemini call over
    the whole multi-product clip has no mechanism to guarantee sentence N's audio lines up with
    product N's footage, since the LLM's Korean phrasing tempo and the original footage's edit
    tempo are paced completely independently of each other.
    """
    reference_video_path = Path(reference_video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    own_scratch = scratch_dir is None
    scratch_ctx = tempfile.TemporaryDirectory() if own_scratch else None
    scratch_dir = Path(scratch_ctx.name) if own_scratch else Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    all_warnings: list[str] = []

    try:
        keep_height_px = subtitle_crop.detect_bottom_crop_px(reference_video_path)
        _, _, full_duration = _probe(reference_video_path)

        video_file_uri = gemini_client.upload_file(reference_video_path, "video/mp4")
        gemini_client.wait_for_file_active(video_file_uri)
        product_segments = gemini_client.detect_product_segments(video_file_uri)
        if not product_segments:
            raise RuntimeError("detect_product_segments returned no segments for a multi-segment run")

        # One overall hook title covering the whole (multi-product) clip; its transcript/script
        # are discarded since each product gets its own script from its own sub-clip below.
        overall = gemini_client.generate_script_from_video(video_file_uri, full_duration, tone_hint=tone_hint)
        hook_title = overall["hook_title"]

        count = len(product_segments)
        seg_captioned_paths: list[Path] = []
        all_script_ko: list[str] = []
        all_transcript: list[str] = []

        for i, seg in enumerate(product_segments):
            label = seg.get("label") or f"segment_{i + 1}"
            start_s = float(seg["start"])
            end_s = float(seg["end"])
            seg_scratch = scratch_dir / f"seg{i + 1:02d}"
            seg_scratch.mkdir(parents=True, exist_ok=True)

            seg_raw = seg_scratch / "raw.mp4"
            _run_ffmpeg([
                "-i", str(reference_video_path), "-ss", str(start_s), "-to", str(end_s),
                "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-avoid_negative_ts", "make_zero",
                str(seg_raw),
            ])

            role_hint = _MULTI_SEGMENT_ROLE_HINTS[_segment_role(i, count)]
            seg_tone_hint = (tone_hint + " " if tone_hint else "") + role_hint

            seg_result = _dub_segment(
                seg_raw, keep_height_px, pretendard_font_path, dohyeon_font_path, seg_scratch,
                seg_tone_hint, speed_factor, tts_provider, chromium_executable_path,
                add_banner=False,
            )
            seg_captioned_paths.append(seg_result.captioned_path)
            all_script_ko.extend(seg_result.script_ko)
            all_transcript.extend(seg_result.transcript_original)
            all_warnings.extend(f"[{label}] {w}" for w in seg_result.warnings)

        concat_list = scratch_dir / "group_concat_list.txt"
        concat_list.write_text("".join(f"file '{p}'\n" for p in seg_captioned_paths))
        concatenated = scratch_dir / "group_concatenated.mp4"
        _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(concatenated)])

        banner_path = scratch_dir / "group_banner.png"
        dub_caption_render.render_hook_banner(
            hook_title["line1"], hook_title["line2"], hook_title.get("emphasis_word"),
            pretendard_font_path, banner_path, chromium_executable_path=chromium_executable_path,
        )
        _run_ffmpeg([
            "-i", str(concatenated), "-i", str(banner_path),
            "-filter_complex", "[0:v][1:v]overlay=0:0[outv]",
            "-map", "[outv]", "-map", "0:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(output_path),
        ])
        final_duration = _audio_duration(output_path)

        return DubResult(
            output_path=output_path,
            hook_title=hook_title,
            script_ko=all_script_ko,
            transcript_original=all_transcript,
            crop_applied_px=keep_height_px,
            final_duration_s=final_duration,
            warnings=all_warnings,
        )
    finally:
        if scratch_ctx is not None:
            scratch_ctx.cleanup()
