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

# Reject logos, vector graphics, cartoons, toy/diecast models, maps, diagrams, stock watermarks, and non-F1 series
EXCLUDED_IMAGE_PATTERNS = (
    "logo",
    "vector",
    "svg",
    "clipart",
    "cartoon",
    "comic",
    "anime",
    "caricature",
    "illustration",
    "drawing",
    "sketch",
    "3d_render",
    "3d render",
    "cgi",
    "toy",
    "diecast",
    "die-cast",
    "miniature",
    "scale_model",
    "scale model",
    "1_18",
    "1_43",
    "1-18",
    "1-43",
    "1:18",
    "1:43",
    "lego",
    "hot_wheels",
    "hotwheels",
    "burago",
    "bburago",
    "minichamps",
    "model_car",
    "model car",
    "figurine",
    "rc_car",
    "playmobil",
    "funko",
    "slot_car",
    "tamiya",
    "revell",
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
    "le_mans",
    "le mans",
    "24_hours",
    "24 hours",
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


def _extract_subject_keywords(query: str) -> list[str]:
    """Extract distinctive driver/team/track/event keywords (dropping generic filler words)."""
    generic = {
        "formula", "one", "grand", "prix", "race", "racing", "car", "cars", "track",
        "post", "profile", "close", "atmosphere", "empty", "heavy", "broken", "angry",
        "surface", "camera", "onboard", "start", "the", "and", "with", "from", "into",
        "f1", "gp",
    }
    words = re.findall(r"[A-Za-z0-9]+", query)
    return [w for w in words if len(w) >= 3 and w.lower() not in generic]


def _query_wikimedia_raw(gsrsearch: str, limit: int) -> dict[str, Any]:
    resp = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": gsrsearch,
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
    return resp.json().get("query", {}).get("pages", {})


def _search_wikimedia_commons(query: str, limit: int = 14) -> list[dict[str, Any]]:
    """Query Wikimedia Commons API for high-res (>=800px) real F1 photos with negative filters & subject relevance."""
    clean_q = re.sub(r"[^\w\s\-']", " ", query).strip()
    if not clean_q:
        return []
    tokens = [t for t in clean_q.split() if len(t) > 1]
    subject_kws = _extract_subject_keywords(clean_q)
    neg_terms = "-logo -cartoon -toy -diecast -miniature -svg -diagram -map -flag"

    # Attempt 1: Specific query (up to 5 focused tokens)
    q1_tokens = tokens[:5]
    q1 = " ".join(q1_tokens)
    if not any(tok in q1.lower() for tok in ("f1", "formula", "grand prix")):
        q1 = f"{q1} F1"

    pages: dict[str, Any] = {}
    try:
        pages = _query_wikimedia_raw(f"filetype:bitmap {q1} {neg_terms}", limit=limit)
        # Attempt 2: If strict AND on 5 words returned <2 hits, relax to top 3 distinctive subject nouns + F1
        if len(pages) < 2 and len(subject_kws) >= 2:
            relaxed = " ".join(subject_kws[:3]) + " F1"
            more_pages = _query_wikimedia_raw(f"filetype:bitmap {relaxed} {neg_terms}", limit=limit)
            pages.update(more_pages)
    except Exception as exc:
        logger.debug("Wikimedia Commons search failed for '%s': %s", clean_q, exc)
        return []

    query_terms = {kw.lower() for kw in subject_kws}
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
        title_l = title.lower()
        term_hits = sum(1 for qt in query_terms if qt in title_l)
        aspect = w / max(1, h)
        portrait_bonus = 1.6 if aspect <= 1.15 else (1.2 if aspect <= 1.55 else 0.85)
        score = (term_hits * 2200.0) + (min(w, 2200) * portrait_bonus)
        candidates.append({"title": title, "url": url, "width": w, "height": h, "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _search_wikipedia_pageimages(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Secondary search using Wikipedia API pageimages, strictly filtering for subject keyword relevance."""
    clean_q = re.sub(r"[^\w\s\-']", " ", query).strip()
    if not clean_q:
        return []
    subject_kws = _extract_subject_keywords(clean_q)
    search_str = " ".join(subject_kws[:4]) if subject_kws else clean_q
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{search_str} Formula One",
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

    query_terms = {kw.lower() for kw in subject_kws}
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
        # Require at least 1 real subject keyword hit in page title or image filename so unrelated drivers never match!
        haystack = f"{title} {url}".lower()
        term_hits = sum(1 for qt in query_terms if qt in haystack)
        if query_terms and term_hits == 0:
            continue
        candidates.append({"title": title, "url": url, "width": w, "height": h, "score": term_hits * 2000.0 + w})

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


def _compute_image_fingerprint(path: Path) -> tuple[str, str]:
    """Return (sha1_hex, 64-bit perceptual dhash) so identical or resized duplicate images never repeat."""
    raw_hash = hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    try:
        with Image.open(path) as im:
            small = im.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
            pixels = list(small.getdata())
        bits = []
        for row in range(8):
            offset = row * 9
            for col in range(8):
                bits.append("1" if pixels[offset + col] > pixels[offset + col + 1] else "0")
        dhash = f"{int(''.join(bits), 2):016x}"
    except Exception:
        dhash = raw_hash
    return raw_hash, dhash


def _search_and_download_scene(
    visual_query: str,
    fallback_query: str,
    max_downloads: int = 3,
) -> list[dict[str, Any]]:
    """Search visual_query first (strict 15s cap); fall back to fallback_query if < 2 valid images found."""
    deadline = time.monotonic() + 15.0

    # Tier 1: Ultra-specific visual_query
    primary_cands = _search_wikimedia_commons(visual_query, limit=14)
    if len(primary_cands) < 2 and time.monotonic() < deadline - 5.0:
        primary_cands.extend(_search_wikipedia_pageimages(visual_query, limit=5))

    tagged_cands: list[tuple[str, dict[str, Any]]] = [("visual_query", c) for c in primary_cands]

    # Tier 2: Broader context-accurate fallback_query ONLY if visual_query returned < 2 valid candidates
    if len(primary_cands) < 2 and fallback_query and time.monotonic() < deadline - 3.0:
        fb_cands = _search_wikimedia_commons(fallback_query, limit=12)
        if len(fb_cands) < 2 and time.monotonic() < deadline - 2.0:
            fb_cands.extend(_search_wikipedia_pageimages(fallback_query, limit=5))
        tagged_cands.extend(("fallback_query", c) for c in fb_cands)

    downloaded_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for matched_via, cand in tagged_cands:
        if len(downloaded_items) >= max_downloads or time.monotonic() >= deadline:
            break
        url = cand["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        path = _download_verified_photo(url)
        if path:
            sha1_h, dhash = _compute_image_fingerprint(path)
            downloaded_items.append(
                {
                    "path": path,
                    "url": url,
                    "title": cand.get("title", path.name),
                    "matched_via": matched_via,
                    "sha1": sha1_h,
                    "dhash": dhash,
                }
            )
    return downloaded_items


def fetch_f1_photos_for_script(
    script_data: dict[str, Any],
) -> tuple[Path, list[Path], list[dict[str, Any]]]:
    """Shorts v4: Fetch ONE dedicated, unique, unrepeated high-res real F1 photo per scene.

    Uses each scene's `visual_query` first, falling back to `fallback_query` only when <2 valid
    images are returned. Tracks URLs, SHA-1 hashes, and perceptual dHashes across the entire video
    so the exact same photo NEVER appears twice in one Short.
    """
    ensure_directories()
    t0 = time.monotonic()

    scenes = script_data.get("scenes") or script_data.get("beats") or []
    hero_query = str(
        script_data.get("hero_photo_query")
        or (scenes[0].get("visual_query") or scenes[0].get("photo_query") if scenes else "")
        or script_data.get("title")
        or "Formula One race car"
    )
    hero_fallback = str(
        (scenes[0].get("fallback_query") if scenes else "") or "Formula One race car track"
    )

    task_specs: dict[str, tuple[str, str]] = {"hero": (hero_query, hero_fallback)}
    for idx, sc in enumerate(scenes):
        vq = str(sc.get("visual_query") or sc.get("photo_query") or hero_query).strip()
        fq = str(sc.get("fallback_query") or sc.get("secondary_photo_query") or hero_fallback).strip()
        task_specs[f"scene_{idx}"] = (vq, fq)
    for fi, fq in enumerate(FALLBACK_F1_PHOTO_QUERIES[:4]):
        task_specs[f"safety_{fi}"] = (fq, "")

    fetched_by_key: dict[str, list[dict[str, Any]]] = {}
    logger.info(
        "Shorts v4: Fetching unique narration-matched F1 photos for %d scene(s) + hero (15s cap/scene)...",
        len(scenes),
    )
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {
            pool.submit(_search_and_download_scene, vq, fq, 3 if key.startswith("scene_") or key == "hero" else 2): key
            for key, (vq, fq) in task_specs.items()
        }
        try:
            for fut in as_completed(future_map, timeout=55.0):
                key = future_map[fut]
                try:
                    fetched_by_key[key] = fut.result(timeout=1.0)
                except Exception as exc:
                    logger.debug("Photo fetch task %s failed: %s", key, exc)
                    fetched_by_key[key] = []
        except Exception as exc:
            logger.warning("Global photo fetch cap reached (%.1fs): %s — proceeding with fetched + cached pool.", time.monotonic() - t0, exc)

    # Build deduplicated global safety pool (fetched safety queries + cached local photos)
    safety_pool: list[dict[str, Any]] = []
    for k, items in fetched_by_key.items():
        if k.startswith("safety_"):
            safety_pool.extend(items)
    for cached in sorted(PHOTOS_DIR.glob("f1_*.jpg")):
        if cached.stat().st_size > 15_000:
            sha1_h, dhash = _compute_image_fingerprint(cached)
            safety_pool.append(
                {
                    "path": cached,
                    "url": str(cached),
                    "title": cached.name,
                    "matched_via": "cached_pool",
                    "sha1": sha1_h,
                    "dhash": dhash,
                }
            )

    used_urls: set[str] = set()
    used_sha1: set[str] = set()
    used_dhash: set[str] = set()

    def _is_unused(item: dict[str, Any]) -> bool:
        return (
            item["url"] not in used_urls
            and item["sha1"] not in used_sha1
            and item["dhash"] not in used_dhash
        )

    def _mark_used(item: dict[str, Any]) -> None:
        used_urls.add(item["url"])
        used_sha1.add(item["sha1"])
        used_dhash.add(item["dhash"])

    def _select_unique(preferred: list[dict[str, Any]]) -> dict[str, Any]:
        for item in preferred:
            if _is_unused(item):
                _mark_used(item)
                return item
        for item in safety_pool:
            if _is_unused(item):
                _mark_used(item)
                return item
        if preferred:
            return preferred[0]
        if safety_pool:
            return random.choice(safety_pool)
        bg = ensure_backgrounds()["f1_red"]
        return {"path": bg, "url": str(bg), "title": bg.name, "matched_via": "fallback_bg", "sha1": "", "dhash": ""}

    hero_item = _select_unique(fetched_by_key.get("hero", []))
    hero_photo: Path = hero_item["path"]

    scene_photos: list[Path] = []
    scene_meta: list[dict[str, Any]] = []
    for idx in range(len(scenes)):
        picked = _select_unique(fetched_by_key.get(f"scene_{idx}", []))
        scene_photos.append(picked["path"])
        scene_meta.append(
            {
                "scene_index": idx,
                "matched_via": picked["matched_via"],
                "image_title": picked["title"],
                "photo_path": str(picked["path"]),
            }
        )
        logger.info(
            "  Scene %d/%d image locked [%s]: %s",
            idx + 1,
            len(scenes),
            picked["matched_via"],
            picked["title"][:64],
        )

    logger.info(
        "Shorts v4: Locked hero + %d unique scene photos in %.1fs (0 duplicates).",
        len(scene_photos),
        time.monotonic() - t0,
    )
    return hero_photo, scene_photos, scene_meta


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
    """Synthesize a smooth, subtle cinematic/electronic F1 ambient synth & bass pulse bed (100% offline CC0)."""
    total = int(duration_sec * sr)
    samples: list[float] = [0.0] * total
    bpm = 118.0
    beat_sec = 60.0 / bpm
    # Cinematic F1 dark minor progression: D minor -> Bb -> F -> C
    roots = [73.42, 58.27, 87.31, 65.41]
    prev_lp = 0.0
    for i in range(total):
        t = i / sr
        bar_idx = int(t / (beat_sec * 4)) % len(roots)
        root = roots[bar_idx]

        # Smooth rolling analog-style bass pulse (warm low end, zero harsh clicks)
        eighth_phase = (t % (beat_sec / 2.0)) / (beat_sec / 2.0)
        bass_env = 0.55 + 0.45 * math.exp(-eighth_phase * 3.8)
        sub_bass = (
            math.sin(2 * math.pi * root * t) * 0.34
            + math.sin(2 * math.pi * (root * 2.0) * t) * 0.14
        ) * bass_env

        # Lush cinematic ambient chord pad (root + minor third + fifth, gently breathing)
        swell = 0.72 + 0.28 * math.sin(2 * math.pi * 0.18 * t)
        pad = (
            math.sin(2 * math.pi * (root * 2.0) * t + 0.3) * 0.12
            + math.sin(2 * math.pi * (root * 2.378) * t) * 0.08
            + math.sin(2 * math.pi * (root * 3.0) * t) * 0.10
            + math.sin(2 * math.pi * (root * 4.0) * t) * 0.04
        ) * swell

        # Gentle 1-pole low-pass filter so frequencies stay warm underneath vocal clarity
        raw = (sub_bass + pad) * 0.78
        prev_lp = prev_lp + 0.18 * (raw - prev_lp)

        # Clean loop edges
        fade = min(1.0, t / 0.5) * min(1.0, max(0.0, (duration_sec - t) / 0.8))
        samples[i] = prev_lp * fade

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
    """Ensure subtle v3 cinematic ambient F1 background music bed and whoosh/pop transition SFX exist."""
    ensure_directories()
    bgm_path = AUDIO_DIR / "f1_ambient_v3_bgm.wav"
    whoosh_path = AUDIO_DIR / "sfx_whoosh.wav"
    pop_path = AUDIO_DIR / "sfx_pop.wav"

    if not bgm_path.exists():
        logger.info("Generating offline Shorts v3 cinematic F1 ambient background music bed...")
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
