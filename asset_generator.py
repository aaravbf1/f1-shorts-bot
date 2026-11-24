"""Generates vertical dark-mode F1 backgrounds, acquires bold fonts, and builds audio beds/SFX."""

from __future__ import annotations

import logging
import math
import random
import struct
import wave
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter

from config import (
    AUDIO_DIR,
    BACKGROUNDS_DIR,
    FONTS_DIR,
    TEAM_PALETTES,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    ensure_directories,
)

logger = logging.getLogger(__name__)

# Google Fonts static bold/extrabold TTF URLs (OFL licensed)
FONT_DOWNLOAD_URLS = {
    "Poppins-ExtraBold.ttf": (
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-ExtraBold.ttf"
    ),
    "Poppins-Bold.ttf": (
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf"
    ),
    "BebasNeue-Regular.ttf": (
        "https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf"
    ),
}

SYSTEM_FONT_FALLBACKS = [
    Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\impact.ttf"),
    Path(r"C:\Windows\Fonts\DUBAI-BOLD.TTF"),
    Path(r"C:\Windows\Fonts\calibrib.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]


def ensure_fonts() -> dict[str, Path]:
    """Ensure bold mobile-readable fonts exist in assets/fonts/ with local fallback."""
    ensure_directories()
    resolved: dict[str, Path] = {}

    for filename, url in FONT_DOWNLOAD_URLS.items():
        target = FONTS_DIR / filename
        if not target.exists() or target.stat().st_size < 10_000:
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                target.write_bytes(resp.content)
                logger.info("Downloaded font: %s", target.name)
            except Exception as exc:
                logger.warning("Could not download %s (%s); will use fallback.", filename, exc)
        if target.exists() and target.stat().st_size >= 10_000:
            resolved[filename] = target

    fallback = next((p for p in SYSTEM_FONT_FALLBACKS if p.exists()), None)
    return {
        "display": resolved.get("BebasNeue-Regular.ttf")
        or resolved.get("Poppins-ExtraBold.ttf")
        or fallback,
        "bold": resolved.get("Poppins-ExtraBold.ttf")
        or resolved.get("Poppins-Bold.ttf")
        or fallback,
        "regular": resolved.get("Poppins-Bold.ttf")
        or resolved.get("Poppins-ExtraBold.ttf")
        or fallback,
    }


def _generate_dark_f1_background(
    path: Path,
    accent_rgb: tuple[int, int, int],
    seed: int = 42,
) -> Path:
    """Create a rich 1080x1920 dark-mode F1 carbon & racing-light backdrop."""
    rng = random.Random(seed)
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT

    img = Image.new("RGB", (w, h), (9, 10, 14))
    draw = ImageDraw.Draw(img, "RGBA")

    # Vertical deep-charcoal atmospheric gradient
    for y in range(0, h, 4):
        t = y / h
        r = int(10 + 10 * (1 - abs(t - 0.35)))
        g = int(12 + 10 * (1 - abs(t - 0.35)))
        b = int(18 + 16 * (1 - abs(t - 0.35)))
        draw.rectangle([0, y, w, y + 4], fill=(r, g, b, 255))

    # Subtle diagonal carbon-fiber racing weave
    for offset in range(-h, w + h, 28):
        draw.line([(offset, 0), (offset + h, h)], fill=(255, 255, 255, 8), width=2)
    for offset in range(-h, w + h, 56):
        draw.line([(offset, h), (offset + h, 0)], fill=(255, 255, 255, 5), width=1)

    # Subtle telemetry grid dots on top half
    for gx in range(48, w, 72):
        for gy in range(120, h - 220, 72):
            draw.ellipse([gx - 1, gy - 1, gx + 1, gy + 1], fill=(255, 255, 255, 16))

    # Soft team-colored ambient glow blobs (blurred)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow, "RGBA")
    ar, ag, ab = accent_rgb
    gdraw.ellipse([-160, 120, 620, 920], fill=(ar, ag, ab, 44))
    gdraw.ellipse([480, 980, 1240, 1780], fill=(ar, ag, ab, 34))
    gdraw.ellipse([180, 520, 880, 1220], fill=(255, 255, 255, 14))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=120))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")

    # High-speed diagonal light streaks
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay, "RGBA")
    for _ in range(9):
        y0 = rng.randint(140, h - 320)
        thickness = rng.randint(2, 6)
        alpha = rng.randint(24, 65)
        odraw.line([(-40, y0), (w + 40, y0 - rng.randint(140, 320))], fill=(ar, ag, ab, alpha), width=thickness)

    # Left-edge subtle team racing stripe
    odraw.rectangle([0, 0, 10, h], fill=(ar, ag, ab, 210))
    odraw.rectangle([10, 0, 14, h], fill=(255, 255, 255, 70))

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return path


def ensure_backgrounds() -> dict[str, Path]:
    """Generate dark-mode vertical F1 backgrounds for key team color themes."""
    ensure_directories()
    bg_map: dict[str, Path] = {}
    themes = {
        "f1_red": (TEAM_PALETTES["f1_red"], 101),
        "ferrari": (TEAM_PALETTES["ferrari"], 102),
        "red_bull": (TEAM_PALETTES["red_bull"], 103),
        "mercedes": (TEAM_PALETTES["mercedes"], 104),
        "mclaren": (TEAM_PALETTES["mclaren"], 105),
        "aston_martin": (TEAM_PALETTES["aston_martin"], 106),
        "gold": (TEAM_PALETTES["gold"], 107),
    }
    for name, (rgb, seed) in themes.items():
        path = BACKGROUNDS_DIR / f"bg_{name}.png"
        if not path.exists():
            _generate_dark_f1_background(path, rgb, seed=seed)
        bg_map[name] = path
    return bg_map


def _write_wav_mono(path: Path, samples: list[float], sample_rate: int = 44100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        packed = bytearray()
        for s in samples:
            clamped = max(-0.98, min(0.98, s))
            packed.extend(struct.pack("<h", int(clamped * 32767)))
        wf.writeframes(bytes(packed))
    return path


def _generate_bgm_bed(path: Path, duration_sec: float = 64.0, sr: int = 22050) -> Path:
    """Synthesize a clean, subtle dark-electronic racing tension pulse bed (royalty-free)."""
    total = int(duration_sec * sr)
    samples: list[float] = [0.0] * total
    bpm = 124.0
    beat_sec = 60.0 / bpm

    # Subtle dark minor chord progression: D minor -> Bb -> C -> A
    roots = [73.42, 58.27, 65.41, 55.00]
    for i in range(total):
        t = i / sr
        bar_idx = int(t / (beat_sec * 4)) % len(roots)
        root = roots[bar_idx]

        # Warm sub-bass pulse
        beat_phase = (t % beat_sec) / beat_sec
        pulse_env = math.exp(-beat_phase * 4.5) * 0.55 + 0.45
        sub = math.sin(2 * math.pi * root * t) * 0.30 * pulse_env

        # Soft fifth pad harmonic
        pad = (
            math.sin(2 * math.pi * (root * 2.0) * t) * 0.14
            + math.sin(2 * math.pi * (root * 3.0) * t) * 0.08
        ) * (0.7 + 0.3 * math.sin(2 * math.pi * 0.25 * t))

        # Tight muted 16th-note telemetry tick
        sixteenth = (t % (beat_sec / 4.0)) / (beat_sec / 4.0)
        tick = math.sin(2 * math.pi * 880.0 * t) * math.exp(-sixteenth * 28.0) * 0.045

        # Gentle fade-in / fade-out
        fade = min(1.0, t / 0.8) * min(1.0, max(0.0, (duration_sec - t) / 1.0))
        samples[i] = (sub + pad + tick) * fade * 0.65

    return _write_wav_mono(path, samples, sample_rate=sr)


def _generate_whoosh_sfx(path: Path, duration_sec: float = 0.28, sr: int = 22050) -> Path:
    """Synthesize a crisp filtered-noise + frequency-sweep transition whoosh."""
    rng = random.Random(77)
    total = int(duration_sec * sr)
    samples: list[float] = [0.0] * total
    prev = 0.0
    for i in range(total):
        u = i / max(1, total - 1)
        env = math.sin(math.pi * u) ** 1.6
        # Low-pass smoothed noise + rising tonal air sweep
        white = rng.uniform(-1.0, 1.0)
        alpha = 0.08 + 0.35 * math.sin(math.pi * u)
        prev = prev + alpha * (white - prev)
        freq = 180.0 + 780.0 * (u ** 1.4)
        sweep = math.sin(2 * math.pi * freq * (i / sr)) * 0.18
        samples[i] = (prev * 0.85 + sweep) * env * 0.72
    return _write_wav_mono(path, samples, sample_rate=sr)


def _generate_pop_sfx(path: Path, duration_sec: float = 0.14, sr: int = 22050) -> Path:
    """Synthesize a crisp UI stat/card pop click."""
    total = int(duration_sec * sr)
    samples: list[float] = [0.0] * total
    for i in range(total):
        t = i / sr
        u = i / max(1, total - 1)
        env = math.exp(-u * 10.5)
        freq = 540.0 * math.exp(-u * 2.2) + 120.0
        tone = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(2 * math.pi * freq * 2.1 * t)
        samples[i] = tone * env * 0.65
    return _write_wav_mono(path, samples, sample_rate=sr)


def ensure_audio_assets() -> dict[str, Path]:
    """Ensure subtle background music bed and whoosh/pop transition SFX exist."""
    ensure_directories()
    bgm_path = AUDIO_DIR / "f1_dark_pulse_bgm.wav"
    whoosh_path = AUDIO_DIR / "sfx_whoosh.wav"
    pop_path = AUDIO_DIR / "sfx_pop.wav"

    if not bgm_path.exists():
        logger.info("Generating royalty-free F1 background music bed...")
        _generate_bgm_bed(bgm_path)
    if not whoosh_path.exists():
        _generate_whoosh_sfx(whoosh_path)
    if not pop_path.exists():
        _generate_pop_sfx(pop_path)

    return {"bgm": bgm_path, "whoosh": whoosh_path, "pop": pop_path}


def ensure_all_assets() -> dict[str, dict[str, Path]]:
    """Ensure all fonts, dark-mode vertical backgrounds, and audio assets are ready."""
    return {
        "fonts": ensure_fonts(),
        "backgrounds": ensure_backgrounds(),
        "audio": ensure_audio_assets(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    assets = ensure_all_assets()
    print("Assets verified:", {k: {ik: str(iv) for ik, iv in v.items()} for k, v in assets.items()})
