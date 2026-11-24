"""Vertical 9:16 YouTube Shorts video builder using edge-tts, FFmpeg, Pillow, and MoviePy."""

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

from asset_generator import ensure_all_assets
from config import (
    FFMPEG_TIMEOUT_SEC,
    MAX_HARD_DURATION_SEC,
    SAFE_BOTTOM,
    SAFE_LEFT,
    SAFE_RIGHT,
    SAFE_TOP,
    SAFE_WIDTH,
    TARGET_MAX_DURATION_SEC,
    TEAM_PALETTES,
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

    # Check common WinGet / Gyan FFmpeg locations on Windows
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
    """Return exact duration in seconds using ffprobe or ffmpeg stderr parsing."""
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

    # Fallback via MoviePy AudioFileClip
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


def _resolve_team_rgb(name: str, default_key: str = "f1_red") -> tuple[int, int, int]:
    key = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in TEAM_PALETTES:
        return TEAM_PALETTES[key]
    for palette_key, rgb in TEAM_PALETTES.items():
        if palette_key in key or key in palette_key:
            return rgb
    return TEAM_PALETTES[default_key]


def _resolve_bg_key(team_color_name: str) -> str:
    key = (team_color_name or "").strip().lower().replace(" ", "_").replace("-", "_")
    valid = {"f1_red", "ferrari", "red_bull", "mercedes", "mclaren", "aston_martin", "gold"}
    if key in valid:
        return key
    for v in valid:
        if v in key:
            return v
    return "f1_red"


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
    """Strip dead air using FFmpeg silenceremove while keeping speech crisp and natural."""
    filter_expr = (
        "silenceremove="
        "start_periods=1:start_duration=0.015:start_threshold=-42dB:"
        "stop_periods=-1:stop_duration=0.26:stop_threshold=-42dB"
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

    # Character-weighted fallback if WordBoundary events were unavailable
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
    """Synthesize beat-by-beat narration, trim dead gaps, concatenate, and enforce <60s."""
    beats_raw = script_data.get("beats", [])
    if not beats_raw:
        raise ValueError("Script has no visual beats to render.")

    primary_rgb = _resolve_team_rgb(script_data.get("primary_team_color", "f1_red"))
    secondary_rgb = _resolve_team_rgb(script_data.get("secondary_team_color", "gold"), "gold")

    timed_beats: list[TimedBeat] = []
    beat_wavs: list[Path] = []
    cursor = 0.0
    inter_beat_gap = 0.12  # crisp natural breath between visual beats (keeps 130-145 words in 42-55s)

    for idx, beat in enumerate(beats_raw):
        spoken = str(beat.get("spoken_text", "")).strip()
        if not spoken:
            continue

        raw_mp3 = work_dir / f"beat_{idx:02d}_raw.mp3"
        trimmed_wav = work_dir / f"beat_{idx:02d}_trim.wav"

        raw_words = await _synthesize_beat_tts(spoken, raw_mp3, voice=voice, rate=rate)
        _trim_silence_ffmpeg(raw_mp3, trimmed_wav, ffmpeg_bin)
        dur = get_audio_duration(trimmed_wav, ffmpeg_bin)

        # Fallback if silenceremove stripped an ultra-short clip too aggressively
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

        accent = _resolve_team_rgb(beat.get("accent_color", ""), "f1_red") if beat.get("accent_color") else primary_rgb
        timed_beats.append(
            TimedBeat(
                index=idx,
                card_type=str(beat.get("card_type", "story_card")).strip().lower(),
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

    # Build tiny silence separator WAV and concatenate all beats cleanly
    gap_wav = work_dir / "micro_gap.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=mono:d={inter_beat_gap}",
            str(gap_wav),
        ],
        capture_output=True,
        check=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )

    concat_list = work_dir / "concat.txt"
    lines: list[str] = []
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

    # Strict < 60s enforcement (target 42-55s, hard cap MAX_HARD_DURATION_SEC = 58.8s)
    if total_duration > TARGET_MAX_DURATION_SEC + 1.5:
        target_dur = min(TARGET_MAX_DURATION_SEC, MAX_HARD_DURATION_SEC - 0.8)
        tempo = min(1.22, total_duration / target_dur)
        logger.warning(
            "Voiceover %.2fs exceeds target; applying FFmpeg atempo=%.3f to lock under 60s.",
            total_duration,
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
    total_duration: float,
    output_audio: Path,
    ffmpeg_bin: str,
) -> Path:
    """Mix normalized voiceover, -22 dB background music bed, and beat transition SFX."""
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

    # Overlay crisp whoosh/pop SFX on each visual card transition (skip beat 0 start to keep hook instant)
    for beat in timed_beats[1:]:
        sfx_file = pop_path if beat.card_type in {"big_stat", "driver_vs_driver"} else whoosh_path
        inputs.extend(["-i", str(sfx_file)])
        delay_ms = max(0, int((beat.start - 0.04) * 1000))
        label = f"sfx{input_idx}"
        filter_parts.append(
            f"[{input_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume=-14dB,adelay={delay_ms}|{delay_ms}[{label}]"
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
    max_lines: int = 4,
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
    # Append remaining words cleanly if truncated
    used = sum(len(line.split()) for line in lines)
    if used < len(words) and len(lines) == max_lines:
        lines[-1] = lines[-1].rstrip(".,!?") + "..."
    return lines


def _render_beat_overlay(
    beat: TimedBeat,
    category_label: str,
    comment_prompt: str,
    is_final_beat: bool,
    fonts: dict[str, Path],
) -> Image.Image:
    """Render a transparent 1080x1920 RGBA visual beat card respecting Shorts UI safe zones."""
    canvas = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")

    f_badge = _load_font(fonts["bold"], 28)
    f_card_title = _load_font(fonts["bold"], 52)
    f_card_sub = _load_font(fonts["regular"], 34)
    f_stat_huge = _load_font(fonts["display"], 165)
    f_vs_big = _load_font(fonts["display"], 88)
    f_vs_name = _load_font(fonts["bold"], 40)
    f_quote = _load_font(fonts["bold"], 44)
    f_prompt = _load_font(fonts["bold"], 31)

    ar, ag, ab = beat.accent_rgb
    sr, sg, sb = beat.secondary_rgb

    # Top category badge inside safe zone (y=196..256)
    badge_text = f"F1  |  {category_label.upper()}"
    if len(badge_text) > 38:
        badge_text = badge_text[:35].rstrip() + "..."
    tb = draw.textbbox((0, 0), badge_text, font=f_badge)
    badge_w = min(SAFE_WIDTH, (tb[2] - tb[0]) + 52)
    draw.rounded_rectangle(
        [SAFE_LEFT, SAFE_TOP + 10, SAFE_LEFT + badge_w, SAFE_TOP + 66],
        radius=16,
        fill=(18, 20, 28, 225),
        outline=(ar, ag, ab, 230),
        width=3,
    )
    draw.rectangle([SAFE_LEFT + 14, SAFE_TOP + 24, SAFE_LEFT + 22, SAFE_TOP + 52], fill=(ar, ag, ab, 255))
    draw.text((SAFE_LEFT + 34, SAFE_TOP + 21), badge_text, font=f_badge, fill=(242, 245, 250, 255))

    # Main visual card container strictly inside safe zone:
    # x: SAFE_LEFT (56) .. SAFE_RIGHT (896) -> completely clear of right-side YT icons
    # y: 292 .. 852 -> completely clear of top bar and bottom subtitles/description
    card_x0, card_y0, card_x1, card_y1 = SAFE_LEFT, 292, SAFE_RIGHT, 845
    card_w = card_x1 - card_x0

    # Soft drop shadow
    draw.rounded_rectangle(
        [card_x0 + 8, card_y0 + 12, card_x1 + 8, card_y1 + 12],
        radius=30,
        fill=(0, 0, 0, 120),
    )
    # Glass dark card base
    draw.rounded_rectangle(
        [card_x0, card_y0, card_x1, card_y1],
        radius=30,
        fill=(16, 19, 27, 232),
        outline=(255, 255, 255, 42),
        width=2,
    )
    # Bold team accent top bar
    draw.rounded_rectangle(
        [card_x0 + 24, card_y0 + 18, card_x1 - 24, card_y0 + 30],
        radius=6,
        fill=(ar, ag, ab, 255),
    )

    inner_x = card_x0 + 42
    inner_w = card_w - 84

    if beat.card_type == "big_stat":
        stat_val = (beat.stat_value or beat.header or "F1").upper()
        stat_lbl = (beat.stat_label or beat.subtext or beat.header).upper()

        # Fit giant stat cleanly inside card width
        stat_font_size = 165
        while stat_font_size > 82:
            f_stat_huge = _load_font(fonts["display"], stat_font_size)
            sbbox = draw.textbbox((0, 0), stat_val, font=f_stat_huge)
            if (sbbox[2] - sbbox[0]) <= inner_w:
                break
            stat_font_size -= 10

        sbbox = draw.textbbox((0, 0), stat_val, font=f_stat_huge)
        sw = sbbox[2] - sbbox[0]
        sh = sbbox[3] - sbbox[1]
        sx = card_x0 + (card_w - sw) // 2
        sy = card_y0 + 115

        draw.text((sx + 5, sy + 6), stat_val, font=f_stat_huge, fill=(0, 0, 0, 190))
        draw.text((sx, sy), stat_val, font=f_stat_huge, fill=(ar, ag, ab, 255))

        # Divider bar
        div_y = sy + sh + 42
        draw.rectangle([card_x0 + 160, div_y, card_x1 - 160, div_y + 5], fill=(255, 255, 255, 70))

        lbl_lines = _wrap_text(draw, stat_lbl, f_card_title, inner_w, max_lines=2)
        ly = div_y + 34
        for line in lbl_lines:
            lb = draw.textbbox((0, 0), line, font=f_card_title)
            lx = card_x0 + (card_w - (lb[2] - lb[0])) // 2
            draw.text((lx, ly), line, font=f_card_title, fill=(246, 248, 252, 255))
            ly += 62

    elif beat.card_type == "quote_callout":
        speaker = (beat.quote_speaker or "TEAM RADIO").upper()
        quote_body = beat.header or beat.subtext or "BOX THIS LAP!"
        if not quote_body.startswith('"'):
            quote_body = f'"{quote_body}"'

        # Radio pill header dynamically sized to speaker label
        radio_label = f"RADIO  •  {speaker[:20]}"
        rb = draw.textbbox((0, 0), radio_label, font=f_badge)
        radio_pill_w = min(inner_w - 175, (rb[2] - rb[0]) + 40)
        draw.rounded_rectangle(
            [inner_x, card_y0 + 58, inner_x + radio_pill_w, card_y0 + 108],
            radius=12,
            fill=(ar, ag, ab, 235),
        )
        draw.text((inner_x + 18, card_y0 + 67), radio_label, font=f_badge, fill=(255, 255, 255, 255))

        # Decorative telemetry waveform bars
        bar_y = card_y0 + 83
        for bi, bh in enumerate([10, 22, 34, 18, 28, 14, 30, 20, 12]):
            bx = card_x1 - 46 - bi * 16
            draw.rectangle([bx - 4, bar_y - bh // 2, bx + 4, bar_y + bh // 2], fill=(ar, ag, ab, 210))

        q_lines = _wrap_text(draw, quote_body, f_quote, inner_w, max_lines=5)
        qy = card_y0 + 150
        for line in q_lines:
            draw.text((inner_x, qy), line, font=f_quote, fill=(250, 250, 255, 255))
            qy += 58

        if beat.subtext and beat.subtext != beat.header:
            s_lines = _wrap_text(draw, beat.subtext, f_card_sub, inner_w, max_lines=2)
            sy = max(qy + 22, card_y1 - 115)
            for line in s_lines:
                draw.text((inner_x, sy), line, font=f_card_sub, fill=(195, 205, 220, 240))
                sy += 42

    elif beat.card_type == "driver_vs_driver":
        left_txt = (beat.left_label or "DRIVER A").upper()
        right_txt = (beat.right_label or "DRIVER B").upper()
        header_txt = (beat.header or "HEAD TO HEAD").upper()

        hb = draw.textbbox((0, 0), header_txt, font=f_badge)
        hx = card_x0 + (card_w - (hb[2] - hb[0])) // 2
        draw.text((hx, card_y0 + 54), header_txt, font=f_badge, fill=(210, 218, 230, 240))

        box_y0 = card_y0 + 115
        box_y1 = card_y1 - 125
        mid_x = card_x0 + card_w // 2
        gap = 26

        # Left side card
        draw.rounded_rectangle(
            [inner_x, box_y0, mid_x - gap, box_y1],
            radius=22,
            fill=(26, 30, 42, 240),
            outline=(ar, ag, ab, 245),
            width=4,
        )
        draw.rectangle([inner_x + 16, box_y0 + 16, mid_x - gap - 16, box_y0 + 26], fill=(ar, ag, ab, 255))
        l_lines = _wrap_text(draw, left_txt, f_vs_name, (mid_x - gap - inner_x) - 32, max_lines=3)
        ly = box_y0 + 68
        for line in l_lines:
            lb = draw.textbbox((0, 0), line, font=f_vs_name)
            lx = inner_x + ((mid_x - gap - inner_x) - (lb[2] - lb[0])) // 2
            draw.text((lx, ly), line, font=f_vs_name, fill=(255, 255, 255, 255))
            ly += 50

        # Right side card
        draw.rounded_rectangle(
            [mid_x + gap, box_y0, card_x1 - 42, box_y1],
            radius=22,
            fill=(26, 30, 42, 240),
            outline=(sr, sg, sb, 245),
            width=4,
        )
        draw.rectangle([mid_x + gap + 16, box_y0 + 16, card_x1 - 58, box_y0 + 26], fill=(sr, sg, sb, 255))
        r_lines = _wrap_text(draw, right_txt, f_vs_name, (card_x1 - 42 - (mid_x + gap)) - 32, max_lines=3)
        ry = box_y0 + 68
        for line in r_lines:
            rb = draw.textbbox((0, 0), line, font=f_vs_name)
            rx = (mid_x + gap) + ((card_x1 - 42 - (mid_x + gap)) - (rb[2] - rb[0])) // 2
            draw.text((rx, ry), line, font=f_vs_name, fill=(255, 255, 255, 255))
            ry += 50

        # Center VS badge
        vs_r = 46
        vs_cy = (box_y0 + box_y1) // 2
        draw.ellipse([mid_x - vs_r, vs_cy - vs_r, mid_x + vs_r, vs_cy + vs_r], fill=(ar, ag, ab, 255), outline=(255, 255, 255, 255), width=4)
        vb = draw.textbbox((0, 0), "VS", font=f_vs_big)
        f_vs_small = _load_font(fonts["display"], 58)
        vb = draw.textbbox((0, 0), "VS", font=f_vs_small)
        draw.text((mid_x - (vb[2] - vb[0]) // 2, vs_cy - (vb[3] - vb[1]) // 2 - 4), "VS", font=f_vs_small, fill=(255, 255, 255, 255))

        if beat.subtext:
            s_lines = _wrap_text(draw, beat.subtext, f_card_sub, inner_w, max_lines=2)
            sy = box_y1 + 26
            for line in s_lines:
                sb2 = draw.textbbox((0, 0), line, font=f_card_sub)
                sx = card_x0 + (card_w - (sb2[2] - sb2[0])) // 2
                draw.text((sx, sy), line, font=f_card_sub, fill=(220, 226, 236, 245))
                sy += 40

    else:
        # Default: bold story_card
        title_txt = (beat.header or "FORMULA 1").upper()
        t_lines = _wrap_text(draw, title_txt, f_card_title, inner_w, max_lines=3)
        ty = card_y0 + 78
        for line in t_lines:
            draw.text((inner_x, ty), line, font=f_card_title, fill=(255, 255, 255, 255))
            ty += 66

        # Team accent underline
        draw.rectangle([inner_x, ty + 14, inner_x + 140, ty + 22], fill=(ar, ag, ab, 255))

        if beat.subtext:
            s_lines = _wrap_text(draw, beat.subtext, f_card_sub, inner_w, max_lines=4)
            sy = ty + 48
            for line in s_lines:
                draw.text((inner_x, sy), line, font=f_card_sub, fill=(210, 218, 232, 245))
                sy += 46

    # On the final beat, display the comment-bait debate prompt safely above bottom 20% zone
    if is_final_beat and comment_prompt:
        pill_y0 = 1365
        pill_y1 = SAFE_BOTTOM - 22  # 1488, safely above 1536 (bottom 20%)
        draw.rounded_rectangle(
            [SAFE_LEFT, pill_y0, SAFE_RIGHT, pill_y1],
            radius=20,
            fill=(ar, ag, ab, 235),
            outline=(255, 255, 255, 210),
            width=2,
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
    """Group timed words into punchy 3-word mobile subtitle windows."""
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
    """Draw large center-screen word-by-word highlighted subtitles inside Shorts safe zones."""
    draw = ImageDraw.Draw(frame_img, "RGBA")
    words_upper = [w.word.upper() for w in chunk]
    space_w = 22

    # Measure each word and wrap into 1 or 2 lines inside SAFE_WIDTH
    lines: list[list[tuple[int, str, int, int]]] = [[]]
    line_w = 0
    for idx, word_str in enumerate(words_upper):
        wb = draw.textbbox((0, 0), word_str, font=subtitle_font, stroke_width=5)
        ww = wb[2] - wb[0]
        wh = wb[3] - wb[1]
        if lines[-1] and (line_w + space_w + ww) > (SAFE_WIDTH - 40):
            lines.append([])
            line_w = 0
        lines[-1].append((idx, word_str, ww, wh))
        line_w += (space_w if len(lines[-1]) > 1 else 0) + ww

    # Subtitle band centered between card bottom (845) and comment pill (1365) -> y ~ 1045
    line_h = 88
    total_h = len(lines) * line_h
    start_y = 1095 - total_h // 2

    # Subtle dark backing strip behind active caption for razor-sharp mobile readability
    draw.rounded_rectangle(
        [SAFE_LEFT + 12, start_y - 26, SAFE_RIGHT - 12, start_y + total_h + 26],
        radius=24,
        fill=(8, 10, 16, 165),
    )

    ar, ag, ab = accent_rgb
    for li, line_items in enumerate(lines):
        total_line_w = sum(item[2] for item in line_items) + space_w * max(0, len(line_items) - 1)
        x = SAFE_LEFT + (SAFE_WIDTH - total_line_w) // 2
        y = start_y + li * line_h
        for idx, word_str, ww, wh in line_items:
            is_active = idx == active_word_idx
            if is_active:
                # Use exact rendered textbbox so highlight pill centers vertically on uppercase glyphs
                wb_exact = draw.textbbox((x, y), word_str, font=subtitle_font, stroke_width=5)
                draw.rounded_rectangle(
                    [wb_exact[0] - 14, wb_exact[1] - 10, wb_exact[2] + 14, wb_exact[3] + 10],
                    radius=16,
                    fill=(ar, ag, ab, 245),
                )
                text_fill = (255, 255, 255, 255)
            else:
                text_fill = (240, 244, 250, 255)

            draw.text(
                (x, y),
                word_str,
                font=subtitle_font,
                fill=text_fill,
                stroke_width=5,
                stroke_fill=(0, 0, 0, 255),
            )
            x += ww + space_w


def build_short_video(
    script_data: dict[str, Any],
    output_mp4: Path,
    *,
    voice: str = TTS_VOICE,
    rate: str = TTS_RATE,
) -> dict[str, Any]:
    """Render a complete 9:16 1080x1920 30fps H.264 YouTube Short (< 60s)."""
    from moviepy import AudioFileClip, VideoClip

    ffmpeg_bin = _find_ffmpeg()
    assets = ensure_all_assets()
    fonts = assets["fonts"]
    backgrounds = assets["backgrounds"]
    audio_assets = assets["audio"]

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    primary_color_name = str(script_data.get("primary_team_color", "f1_red"))
    accent_rgb = _resolve_team_rgb(primary_color_name)
    bg_path = backgrounds.get(_resolve_bg_key(primary_color_name), backgrounds["f1_red"])

    with tempfile.TemporaryDirectory(prefix="f1_short_build_") as tmp:
        work_dir = Path(tmp)

        logger.info("Synthesizing voiceover (%s, rate=%s) + silenceremove...", voice, rate)
        voice_wav, timed_beats, all_words, total_duration = asyncio.run(
            _build_timed_voiceover(script_data, work_dir, ffmpeg_bin, voice=voice, rate=rate)
        )

        # Final hard safety check strictly under 60.0s
        if total_duration >= 60.0:
            raise RuntimeError(f"Duration check failed: {total_duration:.2f}s >= 60.0s")

        logger.info("Mixing voiceover + -22 dB BGM + beat SFX (duration=%.2fs)...", total_duration)
        master_audio_path = work_dir / "master_audio.m4a"
        _mix_master_audio(
            voice_wav=voice_wav,
            bgm_path=audio_assets["bgm"],
            whoosh_path=audio_assets["whoosh"],
            pop_path=audio_assets["pop"],
            timed_beats=timed_beats,
            total_duration=total_duration,
            output_audio=master_audio_path,
            ffmpeg_bin=ffmpeg_bin,
        )

        # Load tall scrollable background for subtle continuous vertical motion
        bg_base = Image.open(bg_path).convert("RGB")
        bg_oversized = bg_base.resize((VIDEO_WIDTH + 64, VIDEO_HEIGHT + 120), Image.Resampling.LANCZOS)

        # Pre-render one RGBA overlay per visual beat (super fast frame compositing!)
        category_label = str(script_data.get("category_label", "Formula 1"))
        comment_prompt = str(script_data.get("comment_question", "")).strip()
        beat_overlays: list[Image.Image] = [
            _render_beat_overlay(
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
            # Subtle smooth parallax motion over dark-mode F1 background
            progress = min(1.0, max(0.0, t / max(0.1, total_duration)))
            dx = int(32 + 28 * math.sin(t * 0.45))
            dy = int(110 * progress)
            frame = bg_oversized.crop((dx, dy, dx + VIDEO_WIDTH, dy + VIDEO_HEIGHT)).convert("RGBA")

            # Find active visual beat
            active_beat_idx = len(timed_beats) - 1
            for i, b in enumerate(timed_beats):
                if t <= b.end + 0.05:
                    active_beat_idx = i
                    break

            beat_obj = timed_beats[active_beat_idx]
            overlay = beat_overlays[active_beat_idx]

            # Quick crisp pop-in vertical slide during first 0.14s of each beat
            beat_age = max(0.0, t - beat_obj.start)
            if beat_age < 0.14:
                ease = 1.0 - (1.0 - beat_age / 0.14) ** 3
                slide_y = int((1.0 - ease) * 36)
                shifted = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
                shifted.paste(overlay, (0, slide_y))
                frame = Image.alpha_composite(frame, shifted)
            else:
                frame = Image.alpha_composite(frame, overlay)

            # Find active subtitle chunk and currently spoken word
            active_chunk: list[TimedWord] | None = None
            active_word_idx = 0
            for ch in subtitle_chunks:
                if ch[0].start - 0.05 <= t <= ch[-1].end + 0.12:
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

            # Sleek top retention progress bar (at y=166, right above safe badge)
            pdraw = ImageDraw.Draw(frame, "RGBA")
            bar_x0, bar_x1, bar_y = SAFE_LEFT, SAFE_RIGHT, 174
            pdraw.rounded_rectangle([bar_x0, bar_y, bar_x1, bar_y + 8], radius=4, fill=(255, 255, 255, 38))
            fill_x = bar_x0 + int((bar_x1 - bar_x0) * progress)
            ar, ag, ab = accent_rgb
            pdraw.rounded_rectangle([bar_x0, bar_y, max(bar_x0 + 8, fill_x), bar_y + 8], radius=4, fill=(ar, ag, ab, 245))

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
                preset="medium",
                threads=4,
                ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
                logger=None,
            )
        finally:
            audio_clip.close()
            video_clip.close()

    # Final post-export duration verification directly on the output MP4
    exported_duration = get_audio_duration(output_mp4, ffmpeg_bin)
    if exported_duration >= 60.0:
        output_mp4.unlink(missing_ok=True)
        raise RuntimeError(
            f"Exported MP4 duration ({exported_duration:.2f}s) is >= 60.0s; rejected to protect Shorts feed indexing."
        )

    logger.info(
        "Rendered Short verified: %s (1080x1920, %dfps, %.2fs)",
        output_mp4.name,
        VIDEO_FPS,
        exported_duration,
    )
    return {
        "video_path": str(output_mp4),
        "duration_sec": round(exported_duration, 2),
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "fps": VIDEO_FPS,
        "beat_count": len(timed_beats),
        "word_count": len(all_words),
    }
