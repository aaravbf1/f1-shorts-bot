"""Casual-Friendly Human Storyteller & Seamless-Loop Shorts Scriptwriter with strict anti-AI & anti-jargon validation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from config import (
    BANNED_AI_PHRASES,
    BANNED_JARGON_TERMS,
    MAX_TITLE_LENGTH,
    MAX_WORD_COUNT,
    MIN_WORD_COUNT,
)
from topic_engine import run_structured_agent_prompt

logger = logging.getLogger(__name__)


class VisualBeat(BaseModel):
    """A single punchy 1-sentence narration + matched real F1 photo + mid-video callout beat."""

    spoken_text: str = Field(
        description="ONE crisp, casual-friendly spoken sentence (9-15 everyday words, zero engineer jargon)."
    )
    photo_query: str = Field(
        description=(
            "Specific Wikimedia Commons / Wikipedia F1 photograph search query for this exact beat's "
            "driver, car, track, or race moment (e.g. 'Max Verstappen Red Bull Formula 1', "
            "'Ferrari F1 pit stop', 'Monaco Grand Prix Formula 1', 'Lewis Hamilton Mercedes F1')."
        )
    )
    secondary_photo_query: str = Field(
        default="",
        description=(
            "Optional second distinct F1 photo search query for a fast 2.5-3.5s visual cut mid-beat "
            "(e.g. 'Formula 1 cockpit steering wheel', 'Silverstone Grand Prix F1 race')."
        ),
    )
    callout_badge: str = Field(
        default="",
        description=(
            "Punchy 2-4 word mid-video stat/detail pop-up badge (e.g. '350+ KM/H', '5G BRAKING', "
            "'1.8s PIT STOP', '$100M FINE', 'ONLY 6 CARS'). Leave blank only on beats without a highlight stat."
        ),
    )
    card_type: Literal["story_card", "quote_callout", "big_stat", "driver_vs_driver"] = Field(
        default="story_card",
        description="Overlay emphasis style for this beat.",
    )
    card_header: str = Field(
        description="Short bold 2-5 word on-screen visual kicker or quote."
    )
    card_subtext: str = Field(
        default="",
        description="Short 3-8 word detail or context.",
    )
    stat_value: str = Field(
        default="",
        description="Single huge number/metric if applicable (e.g. '350 KM/H', '5G', '1.8s').",
    )
    stat_label: str = Field(
        default="",
        description="2-4 word label for stat_value.",
    )
    left_label: str = Field(
        default="",
        description="Left driver/team for driver_vs_driver beat.",
    )
    right_label: str = Field(
        default="",
        description="Right driver/team for driver_vs_driver beat.",
    )
    quote_speaker: str = Field(
        default="",
        description="Speaker name for quote_callout beat.",
    )
    accent_color: str = Field(
        default="",
        description="Optional team color override (f1_red, ferrari, red_bull, mercedes, mclaren, aston_martin, gold).",
    )


class F1ShortScript(BaseModel):
    """Complete casual-friendly F1 Short script, visual photo plan, thumbnail hook, and metadata."""

    title: str = Field(
        description="Curiosity-driven YouTube Shorts title strictly under 55 characters, naturally including #f1."
    )
    thumbnail_hook: str = Field(
        description="Bold, punchy 3-4 word high-click thumbnail hook text in ALL CAPS (e.g. 'ONLY 6 CARS!', '$100M PHOTOCOPY', '350 KM/H CHAOS')."
    )
    hero_photo_query: str = Field(
        description="Best dramatic Wikimedia Commons F1 action photo search query for the main thumbnail."
    )
    description: str = Field(
        description="2-sentence casual hook summary followed by #f1 #shorts #formula1 and 2-3 relevant topic hashtags."
    )
    tags: list[str] = Field(
        min_length=12,
        max_length=15,
        description="12-15 high-volume Formula 1 YouTube tags (no # symbols).",
    )
    comment_question: str = Field(
        description="Short spicy comment-bait debate question shown on-screen at the end (<= 9 words)."
    )
    loop_explanation: str = Field(
        description="Brief check showing how the final spoken sentence flows directly into Beat 0's opening hook."
    )
    beats: list[VisualBeat] = Field(
        min_length=8,
        max_length=10,
        description="8-10 punchy 1-sentence beats totaling 95-128 spoken words.",
    )


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def find_banned_phrases(text: str) -> list[str]:
    """Return any banned AI clichés found in text (case-insensitive word/phrase match)."""
    lowered = text.lower()
    hits: list[str] = []
    for phrase in BANNED_AI_PHRASES:
        pattern = r"\b" + re.escape(phrase.lower()) + r"\b"
        if re.search(pattern, lowered):
            hits.append(phrase)
    return hits


def find_unexplained_jargon(beats: list[VisualBeat]) -> list[str]:
    """Detect banned engineering jargon unless immediately translated into everyday language."""
    translation_cues = ("like a ", "like ", "basically ", "meaning ", "which feels ", "imagine ")
    hits: list[str] = []
    for beat in beats:
        lowered = beat.spoken_text.lower()
        for term in BANNED_JARGON_TERMS:
            pattern = r"\b" + re.escape(term.lower()) + r"\b"
            if re.search(pattern, lowered):
                if not any(cue in lowered for cue in translation_cues):
                    hits.append(term)
    return sorted(set(hits))


def normalize_title(title: str) -> str:
    """Ensure title includes #f1 and stays strictly under 55 characters."""
    clean = " ".join(title.strip().split())
    if "#f1" not in clean.lower():
        clean = f"{clean} #f1"

    if len(clean) <= MAX_TITLE_LENGTH:
        return clean

    base = re.sub(r"\s*#f1\b", "", clean, flags=re.IGNORECASE).strip(" -:|,")
    max_base = MAX_TITLE_LENGTH - 4  # space + "#f1"
    trimmed = base[:max_base].rsplit(" ", 1)[0].rstrip(" -:|,")
    if not trimmed:
        trimmed = base[:max_base].strip()
    return f"{trimmed} #f1"


def normalize_thumbnail_hook(hook: str) -> str:
    """Keep thumbnail hook punchy (2-4 words, uppercase)."""
    words = [w for w in hook.strip().split() if w]
    if len(words) > 4:
        words = words[:4]
    return " ".join(words).upper() or "INSANE F1 MOMENT"


def normalize_description(description: str) -> str:
    """Guarantee #f1 #shorts #formula1 hashtags are present in the description."""
    clean = description.strip()
    lower = clean.lower()
    missing = [tag for tag in ("#f1", "#shorts", "#formula1") if tag not in lower]
    if missing:
        clean = f"{clean}\n\n{' '.join(missing)}"
    return clean


def validate_script(script: F1ShortScript) -> list[str]:
    """Enforce casual-friendly storytelling, zero unexplained jargon, pacing, and metadata rules."""
    errors: list[str] = []

    if not (8 <= len(script.beats) <= 10):
        errors.append(f"Script must have 8-10 punchy 1-sentence beats (got {len(script.beats)}).")

    full_spoken = " ".join(b.spoken_text.strip() for b in script.beats)
    words = count_words(full_spoken)
    if words < MIN_WORD_COUNT:
        errors.append(f"Spoken script too short ({words} words; must be {MIN_WORD_COUNT}-{MAX_WORD_COUNT}).")
    elif words > MAX_WORD_COUNT:
        errors.append(f"Spoken script too long ({words} words; must be {MIN_WORD_COUNT}-{MAX_WORD_COUNT}).")

    # Check banned AI clichés across title, description, and narration
    combined_text = f"{script.title}\n{script.description}\n{full_spoken}"
    banned_hits = find_banned_phrases(combined_text)
    if banned_hits:
        errors.append(f"Banned AI clichés detected: {', '.join(banned_hits)}")

    # Check unexplained engineering jargon
    jargon_hits = find_unexplained_jargon(script.beats)
    if jargon_hits:
        errors.append(
            f"Unexplained technical jargon detected ({', '.join(jargon_hits)}). "
            "Remove it or translate it immediately into a vivid everyday comparison."
        )

    # Check Beat 0 hook is punchy and not a greeting
    first_beat = script.beats[0].spoken_text.strip()
    if count_words(first_beat) > 18:
        errors.append(f"Opening hook beat is too long ({count_words(first_beat)} words; keep <= 17 words).")
    if re.match(r"^(hey|hello|hi|welcome|today we|in this)", first_beat.lower()):
        errors.append("Opening hook must start mid-action or with a shocking fact — never a greeting.")

    # Check every beat has a concrete real F1 photo query and enough mid-video pop-up badges
    missing_photos = [i for i, b in enumerate(script.beats) if len(b.photo_query.strip()) < 4]
    if missing_photos:
        errors.append(f"Beats missing real F1 photo_query: {missing_photos}")

    badge_count = sum(1 for b in script.beats if b.callout_badge.strip() or b.stat_value.strip())
    if badge_count < 4:
        errors.append(
            f"Include at least 4 mid-video stat/detail pop-up badges across beats (found {badge_count})."
        )

    # Check title length & #f1 tag
    if len(script.title.strip()) >= 55:
        errors.append(f"Title too long ({len(script.title.strip())} chars; must be strictly under 55 chars).")
    if "#f1" not in script.title.lower():
        errors.append("Title must naturally include '#f1'.")

    # Check tag count
    clean_tags = [t.strip().lstrip("#") for t in script.tags if t.strip()]
    if not (12 <= len(clean_tags) <= 15):
        errors.append(f"Expected 12-15 tags, got {len(clean_tags)}.")

    # Check seamless loop ending doesn't end with dead sign-off language
    last_beat = script.beats[-1].spoken_text.strip().lower()
    for bad_end in ("subscribe", "like and follow", "thanks for watching", "see you next time"):
        if bad_end in last_beat:
            errors.append(f"Final beat contains non-loop sign-off ('{bad_end}'); end with a seamless loop bridge.")

    return errors


async def _write_validated_script_async(topic: dict[str, Any], max_attempts: int = 3) -> dict[str, Any]:
    """Generate and validate a casual-friendly, real-photo storyboarded F1 Short script."""
    system_instructions = (
        "You are a charismatic, everyday Formula 1 storyteller writing viral 45-to-54-second YouTube Shorts "
        "for casual F1 fans and non-engineers.\n\n"
        "CASUAL-FRIENDLY VOICE RULES:\n"
        "1. ZERO NERD JARGON: Never use unexplained engineering terms like CFD, venturi tunnels, bargeboards, "
        "porpoising, telemetry, brake migration, differential maps, or aero efficiency. If you touch a physical "
        "sensation, translate it immediately into a vivid everyday comparison (e.g., 'the car bouncing like a "
        "jackhammer at 320 km/h', 'braking so hard it feels like five people sitting on your chest', "
        "'changing four tires faster than you can blink twice').\n"
        "2. HUMAN STAKES FIRST: Focus on ego, rivalries, sheer speed, danger, pit-stop pressure, heartbreak, "
        "and real-world comparisons anyone instantly feels.\n"
        "3. STRICTLY FORBIDDEN AI CLICHÉS (instant failure if used anywhere): "
        + ", ".join(f'"{p}"' for p in BANNED_AI_PHRASES)
        + ".\n\n"
        "PACING, VISUALS & SEAMLESS LOOP RULES:\n"
        "4. Break the story into **8 to 10 punchy 1-sentence beats** (each beat = ONE clear, conversational "
        "sentence of ~10-14 words, because TTS runs at a relaxed -10% speed with 0.35s breathing pauses between beats).\n"
        "5. Total spoken words across ALL beats MUST be **105 to 122 words** (strictly inside 95-128 words).\n"
        "6. Beat 0 (Scroll-Stopper Hook): Start mid-action or with a jaw-dropping stat—never a greeting.\n"
        "7. REAL F1 PHOTO QUERIES: For EVERY beat, provide a clean Wikimedia Commons search query in `photo_query` "
        "naming the specific driver, team, car, or track (plus a distinct `secondary_photo_query` for fast mid-beat visual cuts). "
        "Examples: 'Max Verstappen Red Bull Formula 1', 'Ferrari F1 pit stop', 'Lewis Hamilton Mercedes Formula 1', "
        "'Monaco Grand Prix Formula 1', 'Ayrton Senna McLaren Formula 1'.\n"
        "8. MID-VIDEO ATTENTION BADGES: Put a punchy 2-4 word stat/detail pop-up in `callout_badge` on at least "
        "5 beats (e.g. '350+ KM/H', '5G BRAKING', '1.8s PIT STOP', '$100M FINE', 'ONLY 6 CARS').\n"
        "9. SEAMLESS LOOP ENDING: Write the final beat's closing words so they flow grammatically and naturally "
        "straight back into Beat 0's opening words, paired with a quick comment-bait question.\n"
        "10. HIGH-CLICK THUMBNAIL & METADATA:\n"
        "   - `thumbnail_hook`: Bold 3-4 word ALL CAPS hook (e.g. 'ONLY 6 CARS!', '350 KM/H CHAOS').\n"
        "   - `hero_photo_query`: Most dramatic real F1 photo query for the thumbnail.\n"
        "   - `title`: Curiosity-driven, STRICTLY under 55 characters, naturally containing `#f1`.\n"
        "   - `description`: 2 punchy casual sentences + `#f1 #shorts #formula1` + 2-3 topic hashtags.\n"
        "   - `tags`: 12-15 high-volume F1 tags.\n"
        "Return ONLY valid JSON matching the schema."
    )

    feedback_history: list[str] = []
    for attempt in range(1, max_attempts + 1):
        retry_note = ""
        if feedback_history:
            retry_note = (
                "\n\nFIX THESE VALIDATION ERRORS FROM YOUR PREVIOUS DRAFT:\n"
                + "\n".join(f"- {err}" for err in feedback_history)
            )

        prompt = (
            "Write the casual-friendly YouTube Short script, beat-by-beat real F1 photo queries, "
            "mid-video pop-up callout badges, thumbnail hook, and metadata for this verified F1 topic:\n\n"
            f"{json.dumps(topic, indent=2, ensure_ascii=False)}"
            f"{retry_note}\n\n"
            "Checklist before outputting JSON:\n"
            "- 8 to 10 beats, ONE punchy sentence per beat, 105-122 total spoken words.\n"
            "- Zero unexplained engineering jargon, zero AI clichés.\n"
            "- Every beat has a specific Wikimedia Commons F1 `photo_query`, and at least 5 beats have a punchy `callout_badge`.\n"
            "- Title strictly under 55 characters with `#f1`, `thumbnail_hook` is 3-4 bold words.\n"
            "- Final beat loops seamlessly back into Beat 0."
        )

        logger.info(
            "Writing casual-friendly storyteller script for '%s' (attempt %d/%d)...",
            topic["topic_slug"],
            attempt,
            max_attempts,
        )
        script = await run_structured_agent_prompt(
            system_instructions=system_instructions,
            prompt=prompt,
            schema_cls=F1ShortScript,
            enable_web_search=False,
        )

        # Normalize harmless formatting edges before strict validation
        script.title = normalize_title(script.title)
        script.thumbnail_hook = normalize_thumbnail_hook(script.thumbnail_hook)
        script.description = normalize_description(script.description)
        script.tags = [t.strip().lstrip("#") for t in script.tags if t.strip()][:15]

        errors = validate_script(script)
        if not errors:
            full_spoken = " ".join(b.spoken_text.strip() for b in script.beats)
            payload = script.model_dump()
            payload.update(
                {
                    "topic_slug": topic["topic_slug"],
                    "category": topic["category"],
                    "category_label": topic["category_label"],
                    "angle": topic.get("angle", ""),
                    "primary_team_color": topic.get("primary_team_color", "f1_red"),
                    "secondary_team_color": topic.get("secondary_team_color", "gold"),
                    "full_script": full_spoken,
                    "word_count": count_words(full_spoken),
                }
            )
            return payload

        logger.warning("Script draft failed validation: %s", "; ".join(errors))
        feedback_history = errors

    raise RuntimeError(
        f"Script generation for '{topic['topic_slug']}' failed validation after {max_attempts} attempts: "
        + "; ".join(feedback_history)
    )


def write_validated_script(topic: dict[str, Any]) -> dict[str, Any]:
    """Synchronous entry point to generate and validate a Shorts script."""
    return asyncio.run(_write_validated_script_async(topic))
