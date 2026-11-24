"""Vertical 9:16 YouTube Shorts video builder with full-bleed real F1 photos, Ken Burns motion, mid-video badges, and custom thumbnails."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import edge_tts
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from asset_generator import (
    compose_full_bleed_vertical_photo,
    ensure_all_assets,
    fetch_f1_photos_for_script,
    generate_high_click_thumbnail,
)
from config import (
    FFMPEG_TIMEOUT_SEC,
    INTER_BEAT_GAP_SEC,
    MAX_HARD_DURATION_SEC,
    PHOTO_SWAP_INTERVAL_SEC,
    SAFE_BOTTOM,
    SAFE_LEFT,
    SAFE_RIGHT,
    SAFE_TOP,
    SAFE_WIDTH,
    TARGET_MAX_DURATION_SEC,
    TEAM_PALETTES,
    THUMBNAIL_FLASH_SEC,
    TTS_RATE,
    TTS_TIMEOUT_SEC,
    TTS_VOICE,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
)

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str:
    """Locate ffmpeg executable on PATH, winget install folders, or imageio-ffmpeg fallback."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    local_winget = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if local_winget.exists():
        for candidate in local_winget.rglob("ffmpeg.exe"):
            return str(candidate)

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg not found on PATH or via imageio-ffmpeg.") from exc


def _find_ffprobe(ffmpeg_bin: str) -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    for name in ("ffprobe", "ffprobe.exe"):
        sibling = Path(ffmpeg_bin).with_name(name)
        if sibling.exists():
            return str(sibling)
    return None


def get_audio_duration(path: Path, ffmpeg_bin: str) -> float:
    """Return exact duration in seconds using ffprobe or MoviePy fallback."""
    ffprobe = _find_ffprobe(ffmpeg_bin)
    if ffprobe:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        return float(proc.stdout.strip())

    from moviepy import AudioFileClip

    with AudioFileClip(str(path)) as clip:
        return float(clip.duration)


@dataclass
class TimedWord:
    word: str
    start: float
    end: float


@dataclass
class TimedBeat:
    index: int
    card_type: str
    callout_badge: str
    header: str
    subtext: str
    stat_value: str
    stat_label: str
    left_label: str
    right_label: str
    quote_speaker: str
    accent_rgb: tuple[int, int, int]
    secondary_rgb: tuple[int, int, int]
    start: float
    end: float
    audio_path: Path
    words: list[TimedWord]


@dataclass
class PhotoShot:
    start: float
    end: float
    image: Image.Image  # Pre-composed oversized full-bleed vertical F1 photo
    zoom_in: bool
    pan_dir: int  # -1 left, +1 right, 0 center vertical


def _resolve_team_rgb(name: str, default_key: str = "f1_red") -> tuple[int, int, int]:
    key = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in TEAM_PALETTES:
        return TEAM_PALETTES[key]
    for palette_key, rgb in TEAM_PALETTES.items():
        if palette_key in key or key in palette_key:
            return rgb
    return TEAM_PALETTES[default_key]


async def _synthesize_beat_tts(
    text: str,
    out_mp3: Path,
    voice: str,
    rate: str,
) -> list[TimedWord]:
    """Generate TTS audio with edge-tts and extract WordBoundary timestamps with strict timeout."""
    kwargs: dict[str, Any] = {"voice": voice, "rate": rate}
    sig = inspect.signature(edge_tts.Communicate.__init__)
    if "boundary" in sig.parameters:
        kwargs["boundary"] = "WordBoundary"

    async def _run_tts() -> list[TimedWord]:
        communicator = edge_tts.Communicate(text, **kwargs)
        words: list[TimedWord] = []
        with out_mp3.open("wb") as f:
            async for chunk in communicator.stream():
                ctype = chunk.get("type")
                if ctype == "audio":
                    f.write(chunk["data"])
                elif ctype == "WordBoundary":
                    w_text = str(chunk.get("text", "")).strip()
                    if not w_text:
                        continue
                    offset_s = float(chunk.get("offset", 0)) / 10_000_000.0
                    dur_s = max(0.05, float(chunk.get("duration", 0)) / 10_000_000.0)
                    words.append(TimedWord(word=w_text, start=offset_s, end=offset_s + dur_s))
        return words

    last_err: Exception | None = None
    for attempt in range(1, 3):
        try:
            return await asyncio.wait_for(_run_tts(), timeout=TTS_TIMEOUT_SEC)
        except Exception as exc:
            last_err = exc
            logger.warning("edge-tts beat attempt %d/2 failed (%s); retrying...", attempt, exc)
    raise RuntimeError(f"edge-tts failed after 2 attempts: {last_err}") from last_err


def _trim_silence_ffmpeg(raw_audio: Path, trimmed_wav: Path, ffmpeg_bin: str) -> None:
    """Strip leading/trailing dead air while preserving natural slower narration clarity."""
    filter_expr = (
        "silenceremove="
        "start_periods=1:start_duration=0.02:start_threshold=-44dB:"
        "stop_periods=-1:stop_duration=0.30:stop_threshold=-44dB"
    )
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(raw_audio),
        "-af",
        filter_expr,
        "-ar",
        "44100",
        "-ac",
        "1",
        str(trimmed_wav),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=FFMPEG_TIMEOUT_SEC)


def _align_words_to_trimmed_duration(
    text: str,
    raw_words: list[TimedWord],
    trimmed_duration: float,
) -> list[TimedWord]:
    """Map word boundaries cleanly onto the silence-trimmed beat audio."""
    if trimmed_duration <= 0.05:
        return []

    if raw_words:
        first_start = raw_words[0].start
        last_end = max(w.end for w in raw_words)
        span = max(0.08, last_end - first_start)
        scale = trimmed_duration / span
        aligned: list[TimedWord] = []
        for w in raw_words:
            s = max(0.0, (w.start - first_start) * scale)
            e = min(trimmed_duration, max(s + 0.04, (w.end - first_start) * scale))
            aligned.append(TimedWord(word=w.word, start=s, end=e))
        return aligned

    tokens = [tok for tok in text.split() if tok.strip()]
    if not tokens:
        return []
    weights = [max(2, len(tok)) for tok in tokens]
    total_weight = sum(weights)
    cursor = 0.0
    fallback: list[TimedWord] = []
    for tok, wt in zip(tokens, weights):
        dur = trimmed_duration * (wt / total_weight)
        fallback.append(TimedWord(word=tok, start=cursor, end=cursor + dur))
        cursor += dur
    return fallback


async def _build_timed_voiceover(
    script_data: dict[str, Any],
    work_dir: Path,
    ffmpeg_bin: str,
    voice: str = TTS_VOICE,
    rate: str = TTS_RATE,
) -> tuple[Path, list[TimedBeat], list[TimedWord], float]:
    """Synthesize beat-by-beat narration (-10% pace, 0.35s breathing pauses, 0.15s thumbnail flash)."""
    beats_raw = script_data.get("beats", [])
    if not beats_raw:
        raise ValueError("Script has no visual beats to render.")

    primary_rgb = _resolve_team_rgb(script_data.get("primary_team_color", "f1_red"))
    secondary_rgb = _resolve_team_rgb(script_data.get("secondary_team_color", "gold"), "gold")

    timed_beats: list[TimedBeat] = []
    beat_wavs: list[Path] = []
    lead_in_sec = THUMBNAIL_FLASH_SEC  # 0.15s dedicated thumbnail hook frame at t=0
    inter_beat_gap = INTER_BEAT_GAP_SEC  # 0.35s breathing pause between 1-sentence beats
    cursor = lead_in_sec

    for idx, beat in enumerate(beats_raw):
        spoken = str(beat.get("spoken_text", "")).strip()
        if not spoken:
            continue

        raw_mp3 = work_dir / f"beat_{idx:02d}_raw.mp3"
        trimmed_wav = work_dir / f"beat_{idx:02d}_trim.wav"

        raw_words = await _synthesize_beat_tts(spoken, raw_mp3, voice=voice, rate=rate)
        _trim_silence_ffmpeg(raw_mp3, trimmed_wav, ffmpeg_bin)
        dur = get_audio_duration(trimmed_wav, ffmpeg_bin)

        if dur < 0.25:
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(raw_mp3), "-ar", "44100", "-ac", "1", str(trimmed_wav)],
                capture_output=True,
                check=True,
                timeout=FFMPEG_TIMEOUT_SEC,
            )
            dur = get_audio_duration(trimmed_wav, ffmpeg_bin)

        local_words = _align_words_to_trimmed_duration(spoken, raw_words, dur)
        global_words = [
            TimedWord(word=w.word, start=cursor + w.start, end=cursor + w.end)
            for w in local_words
        ]

        accent = (
            _resolve_team_rgb(beat.get("accent_color", ""), "f1_red")
            if beat.get("accent_color")
            else primary_rgb
        )
        timed_beats.append(
            TimedBeat(
                index=idx,
                card_type=str(beat.get("card_type", "story_card")).strip().lower(),
                callout_badge=str(beat.get("callout_badge", "")).strip(),
                header=str(beat.get("card_header", "")).strip(),
                subtext=str(beat.get("card_subtext", "")).strip(),
                stat_value=str(beat.get("stat_value", "")).strip(),
                stat_label=str(beat.get("stat_label", "")).strip(),
                left_label=str(beat.get("left_label", "")).strip(),
                right_label=str(beat.get("right_label", "")).strip(),
                quote_speaker=str(beat.get("quote_speaker", "")).strip(),
                accent_rgb=accent,
                secondary_rgb=secondary_rgb,
                start=cursor,
                end=cursor + dur,
                audio_path=trimmed_wav,
                words=global_words,
            )
        )
        beat_wavs.append(trimmed_wav)
        cursor += dur + inter_beat_gap

    # Build lead-in silence (0.15s thumbnail flash) + 0.35s breathing pause separator WAVs
    lead_wav = work_dir / "lead_thumb.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=mono:d={lead_in_sec:.3f}",
            str(lead_wav),
        ],
        capture_output=True,
        check=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )

    gap_wav = work_dir / "breath_gap.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=mono:d={inter_beat_gap:.3f}",
            str(gap_wav),
        ],
        capture_output=True,
        check=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )

    concat_list = work_dir / "concat.txt"
    lines: list[str] = [f"file '{str(lead_wav).replace(chr(92), '/')}'"]
    for i, wav_path in enumerate(beat_wavs):
        safe_p = str(wav_path).replace("\\", "/")
        lines.append(f"file '{safe_p}'")
        if i < len(beat_wavs) - 1:
            safe_g = str(gap_wav).replace("\\", "/")
            lines.append(f"file '{safe_g}'")
    concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

    voice_wav = work_dir / "voiceover_full.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-af",
            "loudnorm=I=-15:TP=-1.5:LRA=9",
            "-ar",
            "44100",
            "-ac",
            "1",
            str(voice_wav),
        ],
        capture_output=True,
        check=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )

    total_duration = get_audio_duration(voice_wav, ffmpeg_bin)

    # Strict < 60s enforcement (keeps every Short strictly inside YouTube Shorts feed limits)
    if total_duration > MAX_HARD_DURATION_SEC:
        target_dur = min(TARGET_MAX_DURATION_SEC + 2.0, MAX_HARD_DURATION_SEC - 0.8)
        tempo = min(1.18, total_duration / target_dur)
        logger.warning(
            "Voiceover %.2fs exceeds %.1fs limit; applying subtle FFmpeg atempo=%.3f.",
            total_duration,
            MAX_HARD_DURATION_SEC,
            tempo,
        )
        sped_wav = work_dir / "voiceover_sped.wav"
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(voice_wav),
                "-af",
                f"atempo={tempo:.4f}",
                "-ar",
                "44100",
                "-ac",
                "1",
                str(sped_wav),
            ],
            capture_output=True,
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        voice_wav = sped_wav
        new_duration = get_audio_duration(voice_wav, ffmpeg_bin)
        scale = new_duration / total_duration
        for b in timed_beats:
            b.start *= scale
            b.end *= scale
            for w in b.words:
                w.start *= scale
                w.end *= scale
        total_duration = new_duration

    all_words = [w for b in timed_beats for w in b.words]
    return voice_wav, timed_beats, all_words, total_duration


def _mix_master_audio(
    *,
    voice_wav: Path,
    bgm_path: Path,
    whoosh_path: Path,
    pop_path: Path,
    timed_beats: list[TimedBeat],
    photo_shots: list[PhotoShot],
    total_duration: float,
    output_audio: Path,
    ffmpeg_bin: str,
) -> Path:
    """Mix normalized narration, -22 dB BGM bed, and crisp pop/whoosh SFX on badge & photo transitions."""
    inputs = ["-i", str(voice_wav), "-stream_loop", "-1", "-i", str(bgm_path)]
    filter_parts = [
        "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.0[vo]",
        (
            f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{total_duration:.3f},volume=-22dB[bgm]"
        ),
    ]
    mix_labels = ["[vo]", "[bgm]"]
    input_idx = 2

    # Crisp pop SFX on every beat with a mid-video callout badge; soft whoosh on other transitions
    for beat in timed_beats[1:]:
        has_badge = bool(beat.callout_badge or beat.stat_value or beat.card_type in {"big_stat", "driver_vs_driver"})
        sfx_file = pop_path if has_badge else whoosh_path
        vol = "-13dB" if has_badge else "-16dB"
        inputs.extend(["-i", str(sfx_file)])
        delay_ms = max(0, int((beat.start - 0.08) * 1000))
        label = f"sfx{input_idx}"
        filter_parts.append(
            f"[{input_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume={vol},adelay={delay_ms}|{delay_ms}[{label}]"
        )
        mix_labels.append(f"[{label}]")
        input_idx += 1

    filter_parts.append(
        f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95[out]"
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[out]",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_audio),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=FFMPEG_TIMEOUT_SEC)
    return output_audio


def _load_font(font_path: Path | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path and font_path.exists():
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 3,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for w in words[1:]:
        trial = f"{current} {w}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = w
            if len(lines) >= max_lines - 1:
                break
    lines.append(current)
    used = sum(len(line.split()) for line in lines)
    if used < len(words) and len(lines) == max_lines:
        lines[-1] = lines[-1].rstrip(".,!?") + "..."
    return lines


def _build_cinema_scrim() -> Image.Image:
    """Create a subtle top + lower-third dark cinema gradient so full-bleed F1 photos stay vibrant while text pops."""
    scrim = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scrim, "RGBA")

    # Gentle top gradient for header + pop-up badge readability (y=0..460)
    for y in range(0, 460, 4):
        alpha = int(170 * (1.0 - y / 460.0) ** 1.4)
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(6, 8, 14, alpha))

    # Smooth lower gradient for crystal-clear captions & comment pill (y=1020..1920)
    for y in range(1020, VIDEO_HEIGHT, 4):
        t = (y - 1020) / (VIDEO_HEIGHT - 1020)
        alpha = int(205 * (t ** 1.15))
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(6, 8, 14, alpha))

    return scrim


def _render_beat_hud_overlay(
    beat: TimedBeat,
    category_label: str,
    comment_prompt: str,
    is_final_beat: bool,
    fonts: dict[str, Path],
) -> Image.Image:
    """Render sleek upper-zone mid-video pop-up detail badges & HUD while leaving center F1 photography wide open."""
    canvas = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")

    f_badge = _load_font(fonts["bold"], 26)
    f_callout_big = _load_font(fonts["display"], 86)
    f_callout_sub = _load_font(fonts["bold"], 30)
    f_quote = _load_font(fonts["bold"], 34)
    f_vs_name = _load_font(fonts["bold"], 32)
    f_prompt = _load_font(fonts["bold"], 31)

    ar, ag, ab = beat.accent_rgb
    sr, sg, sb = beat.secondary_rgb

    # Top compact category tag inside safe zone (y=194..246)
    cat_txt = f"F1  |  {category_label.upper()}"
    if len(cat_txt) > 34:
        cat_txt = cat_txt[:31].rstrip() + "..."
    tb = draw.textbbox((0, 0), cat_txt, font=f_badge)
    cat_w = min(SAFE_WIDTH, (tb[2] - tb[0]) + 46)
    draw.rounded_rectangle(
        [SAFE_LEFT, SAFE_TOP + 8, SAFE_LEFT + cat_w, SAFE_TOP + 58],
        radius=14,
        fill=(12, 14, 20, 205),
        outline=(ar, ag, ab, 235),
        width=3,
    )
    draw.rectangle([SAFE_LEFT + 12, SAFE_TOP + 20, SAFE_LEFT + 19, SAFE_TOP + 46], fill=(ar, ag, ab, 255))
    draw.text((SAFE_LEFT + 30, SAFE_TOP + 18), cat_txt, font=f_badge, fill=(246, 248, 252, 255))

    # Mid-video attention pop-up detail badge in upper safe zone (y=268..435)
    # Deliberately compact so the middle of the 1080x1920 real F1 photo stays 100% visible!
    badge_headline = (beat.callout_badge or beat.stat_value or "").strip().upper()
    badge_sub = (beat.stat_label or beat.header or "").strip().upper()

    if beat.card_type == "driver_vs_driver" and beat.left_label and beat.right_label:
        box_y0, box_y1 = 270, 398
        mid_x = SAFE_LEFT + SAFE_WIDTH // 2
        draw.rounded_rectangle(
            [SAFE_LEFT, box_y0, mid_x - 28, box_y1],
            radius=20,
            fill=(14, 17, 25, 222),
            outline=(ar, ag, ab, 245),
            width=4,
        )
        draw.rounded_rectangle(
            [mid_x + 28, box_y0, SAFE_RIGHT, box_y1],
            radius=20,
            fill=(14, 17, 25, 222),
            outline=(sr, sg, sb, 245),
            width=4,
        )
        l_lines = _wrap_text(draw, beat.left_label.upper(), f_vs_name, (mid_x - 28 - SAFE_LEFT) - 24, max_lines=2)
        ly = box_y0 + (box_y1 - box_y0 - len(l_lines) * 40) // 2
        for ln in l_lines:
            lb = draw.textbbox((0, 0), ln, font=f_vs_name)
            lx = SAFE_LEFT + ((mid_x - 28 - SAFE_LEFT) - (lb[2] - lb[0])) // 2
            draw.text((lx, ly), ln, font=f_vs_name, fill=(255, 255, 255, 255))
            ly += 40

        r_lines = _wrap_text(draw, beat.right_label.upper(), f_vs_name, (SAFE_RIGHT - (mid_x + 28)) - 24, max_lines=2)
        ry = box_y0 + (box_y1 - box_y0 - len(r_lines) * 40) // 2
        for ln in r_lines:
            rb = draw.textbbox((0, 0), ln, font=f_vs_name)
            rx = (mid_x + 28) + ((SAFE_RIGHT - (mid_x + 28)) - (rb[2] - rb[0])) // 2
            draw.text((rx, ry), ln, font=f_vs_name, fill=(255, 255, 255, 255))
            ry += 40

        # VS circle
        vs_cy = (box_y0 + box_y1) // 2
        draw.ellipse([mid_x - 36, vs_cy - 36, mid_x + 36, vs_cy + 36], fill=(ar, ag, ab, 255), outline=(255, 255, 255, 255), width=3)
        f_vs_s = _load_font(fonts["display"], 46)
        vb = draw.textbbox((0, 0), "VS", font=f_vs_s)
        draw.text((mid_x - (vb[2] - vb[0]) // 2, vs_cy - (vb[3] - vb[1]) // 2 - 4), "VS", font=f_vs_s, fill=(255, 255, 255, 255))

    elif beat.card_type == "quote_callout" and beat.header:
        box_y0, box_y1 = 270, 442
        draw.rounded_rectangle(
            [SAFE_LEFT, box_y0, SAFE_RIGHT, box_y1],
            radius=22,
            fill=(12, 15, 23, 222),
            outline=(ar, ag, ab, 240),
            width=4,
        )
        speaker = (beat.quote_speaker or "TEAM RADIO").upper()[:20]
        radio_lbl = f"RADIO  •  {speaker}"
        rb = draw.textbbox((0, 0), radio_lbl, font=f_badge)
        r_pw = min(SAFE_WIDTH - 190, (rb[2] - rb[0]) + 34)
        draw.rounded_rectangle([SAFE_LEFT + 22, box_y0 + 18, SAFE_LEFT + 22 + r_pw, box_y0 + 60], radius=10, fill=(ar, ag, ab, 240))
        draw.text((SAFE_LEFT + 38, box_y0 + 25), radio_lbl, font=f_badge, fill=(255, 255, 255, 255))
        for bi, bh in enumerate([10, 20, 30, 16, 26, 14, 24]):
            bx = SAFE_RIGHT - 32 - bi * 14
            draw.rectangle([bx - 3, box_y0 + 39 - bh // 2, bx + 3, box_y0 + 39 + bh // 2], fill=(ar, ag, ab, 220))

        q_txt = beat.header if beat.header.startswith('"') else f'"{beat.header}"'
        q_lines = _wrap_text(draw, q_txt, f_quote, SAFE_WIDTH - 48, max_lines=2)
        qy = box_y0 + 74
        for ln in q_lines:
            draw.text((SAFE_LEFT + 24, qy), ln, font=f_quote, fill=(255, 255, 255, 255))
            qy += 42

    elif badge_headline:
        # Bold high-energy mid-video stat / detail pop-up badge (e.g. "350+ KM/H", "5G BRAKING", "1.8s PIT STOP")
        # Fit headline cleanly
        h_size = 86
        while h_size > 48:
            f_callout_big = _load_font(fonts["display"], h_size)
            hb = draw.textbbox((0, 0), badge_headline, font=f_callout_big)
            if (hb[2] - hb[0]) <= SAFE_WIDTH - 90:
                break
            h_size -= 6

        hb = draw.textbbox((0, 0), badge_headline, font=f_callout_big)
        hw = hb[2] - hb[0]
        hh = hb[3] - hb[1]

        sub_clean = badge_sub if (badge_sub and badge_sub != badge_headline) else ""
        if len(sub_clean) > 36:
            sub_clean = sub_clean[:33].rstrip() + "..."
        sw = 0
        if sub_clean:
            sb_box = draw.textbbox((0, 0), sub_clean, font=f_callout_sub)
            sw = sb_box[2] - sb_box[0]

        pill_w = min(SAFE_WIDTH, max(hw, sw) + 84)
        pill_h = hh + (58 if sub_clean else 36)
        px0 = SAFE_LEFT
        py0 = 272

        # Glow + dark glass badge with bold neon accent bar
        draw.rounded_rectangle(
            [px0 + 6, py0 + 8, px0 + pill_w + 6, py0 + pill_h + 8],
            radius=22,
            fill=(0, 0, 0, 145),
        )
        draw.rounded_rectangle(
            [px0, py0, px0 + pill_w, py0 + pill_h],
            radius=22,
            fill=(14, 17, 26, 232),
            outline=(ar, ag, ab, 250),
            width=4,
        )
        draw.rounded_rectangle(
            [px0 + 14, py0 + 16, px0 + 26, py0 + pill_h - 16],
            radius=6,
            fill=(ar, ag, ab, 255),
        )
        draw.text(
            (px0 + 44, py0 + 14),
            badge_headline,
            font=f_callout_big,
            fill=(255, 218, 45, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        if sub_clean:
            draw.text(
                (px0 + 44, py0 + hh + 24),
                sub_clean,
                font=f_callout_sub,
                fill=(242, 246, 252, 255),
            )

    # Final beat comment-bait prompt pill safely above YouTube bottom 20% UI zone (y=1375..1485)
    if is_final_beat and comment_prompt:
        pill_y0 = 1375
        pill_y1 = SAFE_BOTTOM - 22  # 1488 < 1536
        draw.rounded_rectangle(
            [SAFE_LEFT, pill_y0, SAFE_RIGHT, pill_y1],
            radius=20,
            fill=(ar, ag, ab, 238),
            outline=(255, 255, 255, 225),
            width=3,
        )
        p_lines = _wrap_text(draw, comment_prompt, f_prompt, SAFE_WIDTH - 48, max_lines=2)
        py = pill_y0 + (pill_y1 - pill_y0 - len(p_lines) * 38) // 2
        for line in p_lines:
            pb = draw.textbbox((0, 0), line, font=f_prompt)
            px = SAFE_LEFT + (SAFE_WIDTH - (pb[2] - pb[0])) // 2
            draw.text((px, py), line, font=f_prompt, fill=(255, 255, 255, 255))
            py += 38

    return canvas


def _build_subtitle_chunks(words: list[TimedWord], chunk_size: int = 3) -> list[list[TimedWord]]:
    """Group timed words into crisp 3-word mobile subtitle windows."""
    chunks: list[list[TimedWord]] = []
    cur: list[TimedWord] = []
    for w in words:
        cur.append(w)
        ends_clause = w.word.endswith((".", "!", "?", ",", ";", ":"))
        if len(cur) >= chunk_size or (ends_clause and len(cur) >= 2):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _draw_active_subtitle(
    frame_img: Image.Image,
    chunk: list[TimedWord],
    active_word_idx: int,
    accent_rgb: tuple[int, int, int],
    subtitle_font: ImageFont.ImageFont,
) -> None:
    """Draw crisp, high-contrast captions with highlighted active keyword inside Shorts safe zones."""
    draw = ImageDraw.Draw(frame_img, "RGBA")
    words_upper = [w.word.upper() for w in chunk]
    space_w = 22

    lines: list[list[tuple[int, str, int, int]]] = [[]]
    line_w = 0
    for idx, word_str in enumerate(words_upper):
        wb = draw.textbbox((0, 0), word_str, font=subtitle_font, stroke_width=6)
        ww = wb[2] - wb[0]
        wh = wb[3] - wb[1]
        if lines[-1] and (line_w + space_w + ww) > (SAFE_WIDTH - 36):
            lines.append([])
            line_w = 0
        lines[-1].append((idx, word_str, ww, wh))
        line_w += (space_w if len(lines[-1]) > 1 else 0) + ww

    # Positioned cleanly in lower-center safe zone (y~1215), keeping center F1 photo wide open
    line_h = 86
    total_h = len(lines) * line_h
    start_y = 1225 - total_h // 2

    # Compact translucent backing pill for razor-sharp readability over any photo
    draw.rounded_rectangle(
        [SAFE_LEFT + 8, start_y - 22, SAFE_RIGHT - 8, start_y + total_h + 22],
        radius=24,
        fill=(8, 10, 16, 178),
    )

    ar, ag, ab = accent_rgb
    for li, line_items in enumerate(lines):
        total_line_w = sum(item[2] for item in line_items) + space_w * max(0, len(line_items) - 1)
        x = SAFE_LEFT + (SAFE_WIDTH - total_line_w) // 2
        y = start_y + li * line_h
        for idx, word_str, ww, wh in line_items:
            is_active = idx == active_word_idx
            if is_active:
                wb_exact = draw.textbbox((x, y), word_str, font=subtitle_font, stroke_width=6)
                draw.rounded_rectangle(
                    [wb_exact[0] - 14, wb_exact[1] - 10, wb_exact[2] + 14, wb_exact[3] + 10],
                    radius=16,
                    fill=(ar, ag, ab, 248),
                )
                text_fill = (255, 255, 255, 255)
            else:
                text_fill = (246, 248, 252, 255)

            draw.text(
                (x, y),
                word_str,
                font=subtitle_font,
                fill=text_fill,
                stroke_width=6,
                stroke_fill=(0, 0, 0, 255),
            )
            x += ww + space_w


def _build_photo_shots(
    timed_beats: list[TimedBeat],
    beat_photo_paths: list[list[Path]],
    total_duration: float,
) -> list[PhotoShot]:
    """Build a timeline of oversized full-bleed real F1 photos swapping every ~2.5-3.5s with Ken Burns motion."""
    oversize_w = int(VIDEO_WIDTH * 1.12)
    oversize_h = int(VIDEO_HEIGHT * 1.12)
    shots: list[PhotoShot] = []
    shot_idx = 0

    for i, beat in enumerate(timed_beats):
        seg_start = beat.start if i > 0 else THUMBNAIL_FLASH_SEC
        seg_end = timed_beats[i + 1].start if i + 1 < len(timed_beats) else total_duration
        seg_dur = max(0.5, seg_end - seg_start)
        paths = beat_photo_paths[i] if i < len(beat_photo_paths) else beat_photo_paths[-1]

        # Swap photo mid-beat when beat > ~3.6s and we have a second real F1 photo (2.5-3.5s cuts!)
        if len(paths) >= 2 and seg_dur >= max(3.5, PHOTO_SWAP_INTERVAL_SEC + 0.4):
            mid = seg_start + seg_dur * 0.5
            sub_pairs = [(seg_start, mid, paths[0]), (mid, seg_end, paths[1])]
        else:
            sub_pairs = [(seg_start, seg_end, paths[0])]

        for s_start, s_end, p_path in sub_pairs:
            composed = compose_full_bleed_vertical_photo(p_path, oversize_w, oversize_h)
            shots.append(
                PhotoShot(
                    start=s_start,
                    end=s_end,
                    image=composed,
                    zoom_in=(shot_idx % 2 == 0),
                    pan_dir=(-1 if shot_idx % 3 == 0 else (1 if shot_idx % 3 == 1 else 0)),
                )
            )
            shot_idx += 1

    return shots


def _render_ken_burns_frame(shot: PhotoShot, t: float) -> Image.Image:
    """Apply smooth slow Ken Burns zoom/pan over a pre-composed full-bleed 1080x1920 F1 photo."""
    dur = max(0.1, shot.end - shot.start)
    u = min(1.0, max(0.0, (t - shot.start) / dur))
    # Smooth ease-in-out
    u_smooth = u * u * (3.0 - 2.0 * u)

    ow, oh = shot.image.size  # 1210 x 2150 (~1.12x)
    # Zoom between 1.00x and 1.09x crop window inside oversized source
    scale = (1.09 - 0.08 * u_smooth) if shot.zoom_in else (1.01 + 0.08 * u_smooth)
    crop_w = min(ow, int(VIDEO_WIDTH * scale))
    crop_h = min(oh, int(VIDEO_HEIGHT * scale))

    max_dx = max(0, ow - crop_w)
    max_dy = max(0, oh - crop_h)

    if shot.pan_dir > 0:
        cx = int(max_dx * (0.15 + 0.70 * u_smooth))
    elif shot.pan_dir < 0:
        cx = int(max_dx * (0.85 - 0.70 * u_smooth))
    else:
        cx = max_dx // 2

    cy = int(max_dy * (0.25 + 0.50 * u_smooth))
    cropped = shot.image.crop((cx, cy, cx + crop_w, cy + crop_h))
    if cropped.size != (VIDEO_WIDTH, VIDEO_HEIGHT):
        cropped = cropped.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.BILINEAR)
    return cropped


def build_short_video(
    script_data: dict[str, Any],
    output_mp4: Path,
    *,
    voice: str = TTS_VOICE,
    rate: str = TTS_RATE,
    thumbnail_path: Path | None = None,
) -> dict[str, Any]:
    """Render a complete 9:16 1080x1920 30fps H.264 YouTube Short (< 60s) + custom high-click thumbnail."""
    from moviepy import AudioFileClip, VideoClip

    ffmpeg_bin = _find_ffmpeg()
    assets = ensure_all_assets()
    fonts = assets["fonts"]
    audio_assets = assets["audio"]

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    if thumbnail_path is None:
        thumbnail_path = output_mp4.with_name(f"{output_mp4.stem}_thumbnail.jpg")

    primary_color_name = str(script_data.get("primary_team_color", "f1_red"))
    accent_rgb = _resolve_team_rgb(primary_color_name)
    category_label = str(script_data.get("category_label", "Formula 1"))

    # 1. Fetch real, high-resolution, watermark-free F1 photos matched to every beat + hero thumbnail
    hero_photo_path, beat_photo_paths = fetch_f1_photos_for_script(script_data)

    # 2. Generate vibrant high-contrast 1080x1920 custom thumbnail image
    thumb_hook = str(script_data.get("thumbnail_hook") or script_data.get("title") or "INSANE F1 STORY")
    generate_high_click_thumbnail(
        hero_photo_path=hero_photo_path,
        hook_text=thumb_hook,
        category_label=category_label,
        accent_rgb=accent_rgb,
        fonts=fonts,
        output_path=thumbnail_path,
    )
    thumb_frame_np = np.array(Image.open(thumbnail_path).convert("RGB"))

    with tempfile.TemporaryDirectory(prefix="f1_short_build_") as tmp:
        work_dir = Path(tmp)

        logger.info(
            "Synthesizing slower, clearer voiceover (%s, rate=%s, pause=%.2fs)...",
            voice,
            rate,
            INTER_BEAT_GAP_SEC,
        )
        voice_wav, timed_beats, all_words, total_duration = asyncio.run(
            _build_timed_voiceover(script_data, work_dir, ffmpeg_bin, voice=voice, rate=rate)
        )

        if total_duration >= 60.0:
            raise RuntimeError(f"Duration check failed: {total_duration:.2f}s >= 60.0s")

        # 3. Build full-bleed real F1 photo shot timeline (swapping every ~2.5-3.5s)
        photo_shots = _build_photo_shots(timed_beats, beat_photo_paths, total_duration)
        logger.info("Prepared %d full-bleed real F1 photo shots with Ken Burns motion.", len(photo_shots))

        logger.info("Mixing voiceover + -22 dB BGM + pop/whoosh SFX (duration=%.2fs)...", total_duration)
        master_audio_path = work_dir / "master_audio.m4a"
        _mix_master_audio(
            voice_wav=voice_wav,
            bgm_path=audio_assets["bgm"],
            whoosh_path=audio_assets["whoosh"],
            pop_path=audio_assets["pop"],
            timed_beats=timed_beats,
            photo_shots=photo_shots,
            total_duration=total_duration,
            output_audio=master_audio_path,
            ffmpeg_bin=ffmpeg_bin,
        )

        # Pre-render reusable cinema scrim & per-beat mid-video HUD overlays
        cinema_scrim = _build_cinema_scrim()
        comment_prompt = str(script_data.get("comment_question", "")).strip()
        beat_overlays: list[Image.Image] = [
            _render_beat_hud_overlay(
                beat=b,
                category_label=category_label,
                comment_prompt=comment_prompt,
                is_final_beat=(i == len(timed_beats) - 1),
                fonts=fonts,
            )
            for i, b in enumerate(timed_beats)
        ]

        subtitle_chunks = _build_subtitle_chunks(all_words, chunk_size=3)
        subtitle_font = _load_font(fonts["bold"], 64)

        def make_frame(t: float) -> np.ndarray:
            # First 0.15s: embed the high-click custom thumbnail directly as the opening frame!
            if t < THUMBNAIL_FLASH_SEC:
                return thumb_frame_np

            progress = min(1.0, max(0.0, t / max(0.1, total_duration)))

            # Select active real F1 photo shot & render smooth Ken Burns motion
            active_shot = photo_shots[-1]
            for sh in photo_shots:
                if t < sh.end:
                    active_shot = sh
                    break

            photo_rgb = _render_ken_burns_frame(active_shot, t)
            frame = Image.alpha_composite(photo_rgb.convert("RGBA"), cinema_scrim)

            # Find active story beat & pop in mid-video attention badge
            active_beat_idx = len(timed_beats) - 1
            for i, b in enumerate(timed_beats):
                if t <= b.end + INTER_BEAT_GAP_SEC * 0.5:
                    active_beat_idx = i
                    break

            beat_obj = timed_beats[active_beat_idx]
            overlay = beat_overlays[active_beat_idx]

            beat_age = max(0.0, t - beat_obj.start)
            if beat_age < 0.16:
                ease = 1.0 - (1.0 - beat_age / 0.16) ** 3
                slide_y = int((1.0 - ease) * 32)
                shifted = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
                shifted.paste(overlay, (0, slide_y))
                frame = Image.alpha_composite(frame, shifted)
            else:
                frame = Image.alpha_composite(frame, overlay)

            # Crisp word-by-word highlighted captions
            active_chunk: list[TimedWord] | None = None
            active_word_idx = 0
            for ch in subtitle_chunks:
                if ch[0].start - 0.06 <= t <= ch[-1].end + 0.16:
                    active_chunk = ch
                    for wi, w in enumerate(ch):
                        if t >= w.start - 0.03:
                            active_word_idx = wi
                    break

            if active_chunk:
                _draw_active_subtitle(
                    frame,
                    active_chunk,
                    active_word_idx,
                    beat_obj.accent_rgb,
                    subtitle_font,
                )

            # Sleek top retention progress bar (y=172)
            pdraw = ImageDraw.Draw(frame, "RGBA")
            bar_x0, bar_x1, bar_y = SAFE_LEFT, SAFE_RIGHT, 172
            pdraw.rounded_rectangle([bar_x0, bar_y, bar_x1, bar_y + 8], radius=4, fill=(255, 255, 255, 55))
            fill_x = bar_x0 + int((bar_x1 - bar_x0) * progress)
            ar, ag, ab = accent_rgb
            pdraw.rounded_rectangle([bar_x0, bar_y, max(bar_x0 + 8, fill_x), bar_y + 8], radius=4, fill=(ar, ag, ab, 250))

            return np.array(frame.convert("RGB"))

        logger.info("Rendering 1080x1920 30fps H.264 MP4 -> %s", output_mp4.name)
        video_clip = VideoClip(make_frame, duration=total_duration)
        audio_clip = AudioFileClip(str(master_audio_path))
        try:
            final_clip = video_clip.with_audio(audio_clip)
            final_clip.write_videofile(
                str(output_mp4),
                fps=VIDEO_FPS,
                codec="libx264",
                audio_codec="aac",
                audio_bitrate="192k",
                preset="veryfast",
                threads=4,
                ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
                logger=None,
            )
        finally:
            audio_clip.close()
            video_clip.close()

    exported_duration = get_audio_duration(output_mp4, ffmpeg_bin)
    if exported_duration >= 60.0:
        output_mp4.unlink(missing_ok=True)
        raise RuntimeError(
            f"Exported MP4 duration ({exported_duration:.2f}s) is >= 60.0s; rejected to protect Shorts feed indexing."
        )

    logger.info(
        "Rendered Short verified: %s (1080x1920, %dfps, %.2fs, %d real F1 photo shots, thumbnail=%s)",
        output_mp4.name,
        VIDEO_FPS,
        exported_duration,
        len(photo_shots),
        thumbnail_path.name,
    )
    return {
        "video_path": str(output_mp4),
        "thumbnail_path": str(thumbnail_path),
        "duration_sec": round(exported_duration, 2),
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "fps": VIDEO_FPS,
        "beat_count": len(timed_beats),
        "photo_shot_count": len(photo_shots),
        "word_count": len(all_words),
    }
