"""Fetches real high-res F1 photographs (Wikimedia Commons/Wikipedia), builds custom thumbnails, fonts, and audio assets."""

from __future__ import annotations

import hashlib
import logging
import math
import random
import re
import struct
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from config import (
    AUDIO_DIR,
    BACKGROUNDS_DIR,
    FONTS_DIR,
    HTTP_TIMEOUT_SEC,
    PHOTOS_DIR,
    SAFE_LEFT,
    SAFE_RIGHT,
    SAFE_WIDTH,
    TEAM_PALETTES,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    ensure_directories,
)

logger = logging.getLogger(__name__)

WIKIMEDIA_HEADERS = {
    "User-Agent": "F1ShortsEngine/2.0 (https://github.com/aaravbf1/f1-shorts-bot; autonomous-f1-shorts)"
}

# Reject non-photographic assets, maps, logos, flags, diagrams, and stock watermarks
EXCLUDED_IMAGE_PATTERNS = (
    "logo",
    "flag",
    "map",
    "circuit_layout",
    "circuit layout",
    "track_map",
    "diagram",
    "chart",
    "graph",
    "signature",
    "autograph",
    "icon",
    "emblem",
    "badge",
    "coat_of_arms",
    "poster",
    "stamp",
    "ticket",
    "trophy_icon",
    "watermark",
    "shutterstock",
    "getty",
    "alamy",
    "istock",
    "silhouette",
    "pictogram",
    "schema",
    "plan_",
    "layout",
    "helmet_design",
    "livery_template",
    "formula_e",
    "formula e",
    "formula-e",
    "e-prix",
    "eprix",
    "mini_electric",
    "motogp",
    "nascar",
    "indycar",
    "rally",
    "wrc",
    "dtm",
)

FALLBACK_F1_PHOTO_QUERIES = [
    "Formula One race car track action",
    "Formula 1 pit stop crew",
    "Formula 1 starting grid race",
    "Ferrari Formula 1 car race",
    "Red Bull Racing Formula 1 car",
    "McLaren Formula 1 race car",
    "Mercedes AMG Formula 1 car",
    "Monaco Grand Prix Formula 1",
    "Silverstone British Grand Prix Formula 1",
    "Spa Francorchamps Belgian Grand Prix Formula 1",
    "Monza Italian Grand Prix Formula 1",
    "Formula 1 driver podium celebration",
]

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
                resp = requests.get(url, timeout=HTTP_TIMEOUT_SEC)
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


def _is_valid_photo_candidate(title: str, width: int, height: int, mime: str) -> bool:
    """Filter strictly for real, high-resolution (>=800px), watermark-free photographs."""
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        return False
    if width < 800 or height < 450:
        return False
    # Avoid super-thin banners or bizarre aspect ratios
    aspect = width / max(1, height)
    if aspect > 2.6 or aspect < 0.38:
        return False
    lowered = title.lower()
    if any(bad in lowered for bad in EXCLUDED_IMAGE_PATTERNS):
        return False
    return True


def _search_wikimedia_commons(query: str, limit: int = 12) -> list[dict[str, Any]]:
    """Query Wikimedia Commons API for high-res (>=800px) real Formula 1 photographs with strict timeout."""
    clean_q = re.sub(r"[^\w\s\-']", " ", query).strip()
    if not clean_q:
        return []
    tokens = [t for t in clean_q.split() if len(t) > 1]
    if len(tokens) > 5:
        tokens = tokens[:5]
    clean_q = " ".join(tokens)
    if not any(tok in clean_q.lower() for tok in ("f1", "formula", "grand prix")):
        clean_q = f"{clean_q} Formula 1"

    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {clean_q}",
                "gsrnamespace": 6,
                "gsrlimit": limit,
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "iiurlwidth": 1600,
            },
            headers=WIKIMEDIA_HEADERS,
            timeout=(6, min(12, HTTP_TIMEOUT_SEC)),
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
    except Exception as exc:
        logger.debug("Wikimedia Commons search failed for '%s': %s", clean_q, exc)
        return []

    candidates: list[dict[str, Any]] = []
    for page in pages.values():
        title = str(page.get("title", ""))
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        w = int(ii.get("width") or 0)
        h = int(ii.get("height") or 0)
        mime = str(ii.get("mime") or "").lower()
        url = str(ii.get("thumburl") or ii.get("url") or "")
        if not url or not _is_valid_photo_candidate(title, w, h, mime):
            continue
        # Score higher for sharper photos and portrait/4:3 friendly framing
        aspect = w / max(1, h)
        portrait_bonus = 2.0 if aspect <= 1.1 else (1.2 if aspect <= 1.55 else 0.8)
        score = min(w, 2400) * portrait_bonus
        candidates.append({"title": title, "url": url, "width": w, "height": h, "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _search_wikipedia_pageimages(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Secondary search using Wikipedia API pageimages for specific drivers, circuits, and races."""
    clean_q = re.sub(r"[^\w\s\-']", " ", query).strip()
    if not clean_q:
        return []
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{clean_q} Formula One",
                "gsrlimit": limit,
                "prop": "pageimages",
                "piprop": "original|thumbnail",
                "pithumbsize": 1400,
            },
            headers=WIKIMEDIA_HEADERS,
            timeout=HTTP_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
    except Exception as exc:
        logger.debug("Wikipedia pageimages search failed for '%s': %s", clean_q, exc)
        return []

    candidates: list[dict[str, Any]] = []
    for page in pages.values():
        title = str(page.get("title", ""))
        img_info = page.get("thumbnail") or page.get("original") or {}
        url = str(img_info.get("source") or "")
        w = int(img_info.get("width") or 0)
        h = int(img_info.get("height") or 0)
        if not url or w < 800 or h < 450:
            continue
        mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        if url.lower().endswith(".svg") or not _is_valid_photo_candidate(url + " " + title, w, h, mime):
            continue
        candidates.append({"title": title, "url": url, "width": w, "height": h, "score": w})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _download_verified_photo(url: str) -> Path | None:
    """Download a photo to assets/photos/ cache and verify with Pillow."""
    ensure_directories()
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    cached_jpg = PHOTOS_DIR / f"f1_{url_hash}.jpg"

    if cached_jpg.exists() and cached_jpg.stat().st_size > 15_000:
        try:
            with Image.open(cached_jpg) as img:
                if img.width >= 700 and img.height >= 400:
                    return cached_jpg
        except Exception:
            cached_jpg.unlink(missing_ok=True)

    try:
        resp = requests.get(url, headers=WIKIMEDIA_HEADERS, timeout=(6, min(14, HTTP_TIMEOUT_SEC)))
        resp.raise_for_status()
        raw_path = PHOTOS_DIR / f"tmp_{url_hash}.bin"
        raw_path.write_bytes(resp.content)
        try:
            with Image.open(raw_path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                if im.width < 700 or im.height < 400:
                    return None
                # Cap max dimension at 1920px for crisp quality + fast MoviePy rendering
                im.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                im.save(cached_jpg, format="JPEG", quality=92, optimize=True)
        finally:
            raw_path.unlink(missing_ok=True)
        return cached_jpg
    except Exception as exc:
        logger.debug("Photo download/verify failed for %s: %s", url[:80], exc)
        return None


def _pick_photo_for_query(query: str, used_urls: set[str]) -> Path | None:
    """Search Wikimedia Commons + Wikipedia and return the first verified, unused high-res photo."""
    candidates = _search_wikimedia_commons(query, limit=12)
    if len(candidates) < 2:
        candidates.extend(_search_wikipedia_pageimages(query, limit=6))

    for cand in candidates:
        url = cand["url"]
        if url in used_urls:
            continue
        downloaded = _download_verified_photo(url)
        if downloaded:
            used_urls.add(url)
            return downloaded
    return None


def _fetch_candidate_pool_for_query(query: str, max_downloads: int = 2) -> list[Path]:
    """Fetch up to `max_downloads` verified high-res F1 photos for a query within a strict 15s cap."""
    deadline = time.monotonic() + 15.0
    candidates = _search_wikimedia_commons(query, limit=10)
    if len(candidates) < 2 and time.monotonic() < deadline - 4.0:
        candidates.extend(_search_wikipedia_pageimages(query, limit=4))

    results: list[Path] = []
    seen_urls: set[str] = set()
    for cand in candidates:
        if len(results) >= max_downloads or time.monotonic() >= deadline:
            break
        url = cand["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        downloaded = _download_verified_photo(url)
        if downloaded:
            results.append(downloaded)
    return results


def fetch_f1_photos_for_script(script_data: dict[str, Any]) -> tuple[Path, list[list[Path]]]:
    """Concurrently fetch real, high-res, watermark-free F1 photos with strict timeouts and instant fallbacks."""
    ensure_directories()
    t0 = time.monotonic()

    hero_query = str(
        script_data.get("hero_photo_query")
        or (script_data.get("beats") or [{}])[0].get("photo_query")
        or script_data.get("title")
        or "Formula One race car"
    )
    beats = script_data.get("beats", [])

    # Build deduplicated query list: hero + each beat's primary/secondary queries + 4 safety fallbacks
    query_tasks: dict[str, str] = {"hero": hero_query}
    for idx, beat in enumerate(beats):
        q1 = str(beat.get("photo_query") or hero_query).strip()
        q2 = str(beat.get("secondary_photo_query") or "").strip()
        query_tasks[f"beat_{idx}_a"] = q1
        if q2:
            query_tasks[f"beat_{idx}_b"] = q2
    for fi, fq in enumerate(FALLBACK_F1_PHOTO_QUERIES[:4]):
        query_tasks[f"fallback_{fi}"] = fq

    fetched_by_key: dict[str, list[Path]] = {}
    logger.info("Concurrently fetching real F1 photos for %d queries (15s timeout/query)...", len(query_tasks))
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {
            pool.submit(_fetch_candidate_pool_for_query, q, 2 if k.endswith("_a") or k == "hero" else 1): k
            for k, q in query_tasks.items()
        }
        try:
            for fut in as_completed(future_map, timeout=50.0):
                key = future_map[fut]
                try:
                    fetched_by_key[key] = fut.result(timeout=1.0)
                except Exception as exc:
                    logger.debug("Photo task %s skipped: %s", key, exc)
                    fetched_by_key[key] = []
        except Exception as exc:
            logger.warning("Global photo fetch budget reached (%.1fs): %s — proceeding with fetched + cached photos.", time.monotonic() - t0, exc)

    # Assemble unique fallback pool from all downloaded + cached photos
    all_valid_pool: list[Path] = []
    for paths in fetched_by_key.values():
        for p in paths:
            if p not in all_valid_pool:
                all_valid_pool.append(p)
    for cached in sorted(PHOTOS_DIR.glob("f1_*.jpg")):
        if cached.stat().st_size > 15_000 and cached not in all_valid_pool:
            all_valid_pool.append(cached)

    used_paths: set[Path] = set()
    pool_cursor = 0

    def _take_unused(preferred: list[Path]) -> Path:
        nonlocal pool_cursor
        for p in preferred:
            if p not in used_paths:
                used_paths.add(p)
                return p
        while pool_cursor < len(all_valid_pool):
            cand = all_valid_pool[pool_cursor]
            pool_cursor += 1
            if cand not in used_paths:
                used_paths.add(cand)
                return cand
        if all_valid_pool:
            return random.choice(all_valid_pool)
        return ensure_backgrounds()["f1_red"]

    hero_photo = _take_unused(fetched_by_key.get("hero", []))
    beat_photos: list[list[Path]] = []
    for idx in range(len(beats)):
        primary_list = fetched_by_key.get(f"beat_{idx}_a", [])
        secondary_list = fetched_by_key.get(f"beat_{idx}_b", [])
        p1 = _take_unused(primary_list)
        p2 = _take_unused(secondary_list + primary_list)
        beat_photos.append([p1, p2] if p2 != p1 else [p1])

    logger.info(
        "Prepared hero + %d real F1 beat photo sets in %.1fs (pool=%d verified photos).",
        len(beat_photos),
        time.monotonic() - t0,
        len(all_valid_pool),
    )
    return hero_photo, beat_photos


def compose_full_bleed_vertical_photo(
    photo_path: Path,
    target_w: int = VIDEO_WIDTH,
    target_h: int = VIDEO_HEIGHT,
) -> Image.Image:
    """Scale/crop a real F1 photo to fill 100% of the vertical frame with zero black/empty middle space.

    - Portrait/near-square photos: clean full-bleed cover crop across the entire target canvas.
    - Wide landscape F1 action photos: rich blurred color-matched photo background filling 100% of
      the frame + a huge, razor-sharp edge-to-edge hero crop across the upper-to-lower center
      feathered seamlessly into the blurred backdrop so the whole car/driver stays intact with
      zero black bars and zero empty middle space.
    """
    with Image.open(photo_path) as raw:
        src = ImageOps.exif_transpose(raw).convert("RGB")

    sw, sh = src.size
    aspect = sw / max(1, sh)

    # Full-bleed cover crop when photo aspect doesn't aggressively clip the F1 car
    if aspect <= 1.25:
        return ImageOps.fit(src, (target_w, target_h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.45))

    # For wide landscape track/pit photos:
    # 1. Richly scaled, darkened & blurred full-bleed background of the SAME photo
    bg = ImageOps.fit(src, (target_w, target_h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=34))
    bg = ImageEnhance.Brightness(bg).enhance(0.62)
    bg = ImageEnhance.Color(bg).enhance(1.22)

    # 2. Huge sharp full-width hero band filling ~68% of vertical height (edge-to-edge 1080px wide)
    hero_h = int(target_h * 0.68)
    hero = ImageOps.fit(src, (target_w, hero_h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.48))
    hero = ImageEnhance.Contrast(hero).enhance(1.08)
    hero = ImageEnhance.Sharpness(hero).enhance(1.12)

    # 3. Soft vertical feather mask at top & bottom edges of hero band for seamless studio blend
    feather_px = int(hero_h * 0.14)
    mask = Image.new("L", (target_w, hero_h), 255)
    mdraw = ImageDraw.Draw(mask)
    for y in range(feather_px):
        alpha = int(255 * (y / max(1, feather_px)))
        mdraw.line([(0, y), (target_w, y)], fill=alpha)
        mdraw.line([(0, hero_h - 1 - y), (target_w, hero_h - 1 - y)], fill=alpha)

    canvas = bg.convert("RGBA")
    hero_y = int(target_h * 0.11)
    canvas.paste(hero.convert("RGBA"), (0, hero_y), mask)
    return canvas.convert("RGB")


def _load_font(font_path: Path | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path and font_path.exists():
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def generate_high_click_thumbnail(
    *,
    hero_photo_path: Path,
    hook_text: str,
    category_label: str,
    accent_rgb: tuple[int, int, int],
    fonts: dict[str, Path],
    output_path: Path,
) -> Path:
    """Generate a vibrant, high-contrast 1080x1920 custom YouTube Shorts thumbnail (< 2 MB JPEG)."""
    # 1. Start with full-bleed dramatic real F1 photo
    base = compose_full_bleed_vertical_photo(hero_photo_path, VIDEO_WIDTH, VIDEO_HEIGHT)
    base = ImageEnhance.Contrast(base).enhance(1.24)
    base = ImageEnhance.Color(base).enhance(1.28)
    base = ImageEnhance.Sharpness(base).enhance(1.20)

    canvas = base.convert("RGBA")
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    ar, ag, ab = accent_rgb

    # 2. Dramatic top/bottom cinema vignette so bold hook text explodes off the screen
    for y in range(0, 660, 4):
        alpha = int(195 * (1.0 - y / 660.0) ** 1.35)
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(6, 8, 14, alpha))
    for y in range(1180, VIDEO_HEIGHT, 4):
        alpha = int(225 * ((y - 1180) / (VIDEO_HEIGHT - 1180)) ** 1.2)
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(6, 8, 14, alpha))

    # Vibrant edge racing glow bars
    draw.rectangle([0, 0, VIDEO_WIDTH, 18], fill=(ar, ag, ab, 255))
    draw.rectangle([0, VIDEO_HEIGHT - 18, VIDEO_WIDTH, VIDEO_HEIGHT], fill=(ar, ag, ab, 255))
    draw.rectangle([0, 0, 14, VIDEO_HEIGHT], fill=(ar, ag, ab, 235))

    # 3. Sleek F1 pill badge near top safe zone
    f_badge = _load_font(fonts["bold"], 34)
    pill_text = f"F1  •  {category_label.upper()}"
    if len(pill_text) > 30:
        pill_text = "FORMULA 1  •  MUST WATCH"
    pb = draw.textbbox((0, 0), pill_text, font=f_badge)
    pw = (pb[2] - pb[0]) + 56
    px0 = (VIDEO_WIDTH - pw) // 2
    draw.rounded_rectangle(
        [px0, 210, px0 + pw, 276],
        radius=18,
        fill=(ar, ag, ab, 245),
        outline=(255, 255, 255, 230),
        width=3,
    )
    draw.text((px0 + 28, 222), pill_text, font=f_badge, fill=(255, 255, 255, 255))

    # 4. Massive high-contrast 3-4 word hook text with thick black outline + drop shadow
    words = [w for w in hook_text.strip().upper().split() if w][:4]
    if not words:
        words = ["INSANE", "F1", "STORY"]

    # Split 3-4 words into 2 ultra-punchy stacked lines
    if len(words) <= 2:
        lines = [" ".join(words)]
    else:
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]

    # Auto-fit huge bold display font inside safe width
    max_text_w = VIDEO_WIDTH - 110
    font_size = 185
    f_hook = _load_font(fonts["display"], font_size)
    while font_size > 96:
        f_hook = _load_font(fonts["display"], font_size)
        widest = max(draw.textbbox((0, 0), ln, font=f_hook, stroke_width=10)[2] for ln in lines)
        if widest <= max_text_w:
            break
        font_size -= 8

    line_gap = int(font_size * 0.96)
    total_text_h = len(lines) * line_gap
    start_y = 1235 - total_text_h // 2

    # Soft dark glow ribbon behind hook text for guaranteed mobile legibility
    draw.rounded_rectangle(
        [36, start_y - 36, VIDEO_WIDTH - 36, start_y + total_text_h + 46],
        radius=30,
        fill=(8, 10, 16, 155),
        outline=(ar, ag, ab, 220),
        width=4,
    )

    for li, ln in enumerate(lines):
        lb = draw.textbbox((0, 0), ln, font=f_hook, stroke_width=10)
        lw = lb[2] - lb[0]
        lx = (VIDEO_WIDTH - lw) // 2
        ly = start_y + li * line_gap
        # Alternate crisp white and high-energy F1 gold/yellow for instant click appeal
        fill_color = (255, 255, 255, 255) if li == 0 else (255, 218, 45, 255)
        # Hard shadow
        draw.text((lx + 8, ly + 10), ln, font=f_hook, fill=(0, 0, 0, 230), stroke_width=10, stroke_fill=(0, 0, 0, 230))
        # Main outlined hook text
        draw.text((lx, ly), ln, font=f_hook, fill=fill_color, stroke_width=10, stroke_fill=(0, 0, 0, 255))

    final_img = Image.alpha_composite(canvas, overlay).convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_img.save(output_path, format="JPEG", quality=92, optimize=True)
    logger.info("Generated custom 1080x1920 thumbnail: %s (%d KB)", output_path.name, output_path.stat().st_size // 1024)
    return output_path


def _generate_dark_f1_background(
    path: Path,
    accent_rgb: tuple[int, int, int],
    seed: int = 42,
) -> Path:
    """Create a rich 1080x1920 dark-mode F1 carbon & racing-light fallback backdrop."""
    rng = random.Random(seed)
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT

    img = Image.new("RGB", (w, h), (9, 10, 14))
    draw = ImageDraw.Draw(img, "RGBA")

    for y in range(0, h, 4):
        t = y / h
        r = int(10 + 10 * (1 - abs(t - 0.35)))
        g = int(12 + 10 * (1 - abs(t - 0.35)))
        b = int(18 + 16 * (1 - abs(t - 0.35)))
        draw.rectangle([0, y, w, y + 4], fill=(r, g, b, 255))

    for offset in range(-h, w + h, 28):
        draw.line([(offset, 0), (offset + h, h)], fill=(255, 255, 255, 8), width=2)

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow, "RGBA")
    ar, ag, ab = accent_rgb
    gdraw.ellipse([-160, 120, 620, 920], fill=(ar, ag, ab, 44))
    gdraw.ellipse([480, 980, 1240, 1780], fill=(ar, ag, ab, 34))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=120))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay, "RGBA")
    for _ in range(9):
        y0 = rng.randint(140, h - 320)
        odraw.line(
            [(-40, y0), (w + 40, y0 - rng.randint(140, 320))],
            fill=(ar, ag, ab, rng.randint(24, 65)),
            width=rng.randint(2, 6),
        )
    odraw.rectangle([0, 0, 10, h], fill=(ar, ag, ab, 210))

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return path


def ensure_backgrounds() -> dict[str, Path]:
    """Generate dark-mode vertical F1 fallback backgrounds."""
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
    roots = [73.42, 58.27, 65.41, 55.00]
    for i in range(total):
        t = i / sr
        bar_idx = int(t / (beat_sec * 4)) % len(roots)
        root = roots[bar_idx]
        beat_phase = (t % beat_sec) / beat_sec
        pulse_env = math.exp(-beat_phase * 4.5) * 0.55 + 0.45
        sub = math.sin(2 * math.pi * root * t) * 0.30 * pulse_env
        pad = (
            math.sin(2 * math.pi * (root * 2.0) * t) * 0.14
            + math.sin(2 * math.pi * (root * 3.0) * t) * 0.08
        ) * (0.7 + 0.3 * math.sin(2 * math.pi * 0.25 * t))
        sixteenth = (t % (beat_sec / 4.0)) / (beat_sec / 4.0)
        tick = math.sin(2 * math.pi * 880.0 * t) * math.exp(-sixteenth * 28.0) * 0.045
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
        white = rng.uniform(-1.0, 1.0)
        alpha = 0.08 + 0.35 * math.sin(math.pi * u)
        prev = prev + alpha * (white - prev)
        freq = 180.0 + 780.0 * (u ** 1.4)
        sweep = math.sin(2 * math.pi * freq * (i / sr)) * 0.18
        samples[i] = (prev * 0.85 + sweep) * env * 0.72
    return _write_wav_mono(path, samples, sample_rate=sr)


def _generate_pop_sfx(path: Path, duration_sec: float = 0.14, sr: int = 22050) -> Path:
    """Synthesize a crisp UI stat/callout badge pop."""
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
    """Ensure all fonts, fallback backgrounds, and audio assets are ready."""
    return {
        "fonts": ensure_fonts(),
        "backgrounds": ensure_backgrounds(),
        "audio": ensure_audio_assets(),
    }
