"""Shorts v4 Scene-by-Scene Visual Storyboard Scriptwriter with strict anti-AI & anti-jargon validation."""

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


class SceneBeat(BaseModel):
    """A single narration-locked visual scene (~3-5 seconds, 1-2 sentences) with exact photo queries."""

    narration: str = Field(
        description=(
            "Exact spoken narration lines for this scene (1-2 crisp, casual-friendly sentences, "
            "~10-18 everyday words, ~3-5 seconds spoken, zero unexplained engineering jargon)."
        )
    )
    visual_query: str = Field(
        description=(
            "Highly specific real-photograph search query matching the EXACT subject, setting, and "
            "race/event mentioned in this scene's narration (e.g. 'Max Verstappen post race interview "
            "press conference Las Vegas F1', 'Ferrari F1 pit stop crew mechanics race', "
            "'Lewis Hamilton Fernando Alonso McLaren 2007 Formula 1')."
        )
    )
    fallback_query: str = Field(
        description=(
            "Slightly broader, still context-accurate real-photo F1 query for the same driver, team, "
            "or setting used if visual_query returns fewer than 2 valid high-res photos "
            "(e.g. 'Max Verstappen Red Bull Formula 1', 'Ferrari F1 pit stop')."
        )
    )
    stat_badge: str = Field(
        default="",
        description=(
            "Optional short 2-4 word on-screen callout badge ONLY when this scene mentions a concrete "
            "stat, quote, year, or lap number (e.g. '350+ KM/H', 'LAP 58', '1.80s STOP', '$100M FINE'). "
            "Leave empty string '' if no concrete stat/quote/lap is mentioned."
        ),
    )
    card_type: Literal["story_card", "quote_callout", "big_stat", "driver_vs_driver"] = Field(
        default="story_card",
        description="Overlay emphasis style for this scene.",
    )
    card_header: str = Field(
        default="",
        description="Short bold 2-5 word sub-label or quote line when stat_badge/quote is shown.",
    )
    stat_label: str = Field(
        default="",
        description="Optional 2-4 word label under stat_badge.",
    )
    left_label: str = Field(
        default="",
        description="Left driver/team label when card_type=='driver_vs_driver'.",
    )
    right_label: str = Field(
        default="",
        description="Right driver/team label when card_type=='driver_vs_driver'.",
    )
    quote_speaker: str = Field(
        default="",
        description="Speaker name when card_type=='quote_callout'.",
    )
    accent_color: str = Field(
        default="",
        description="Optional team color override (f1_red, ferrari, red_bull, mercedes, mclaren, aston_martin, gold).",
    )


class F1ShortScript(BaseModel):
    """Complete Shorts v4 scene-by-scene storyboard, visual photo plan, thumbnail hook, and metadata."""

    title: str = Field(
        description="Clickable curiosity-gap YouTube Shorts title strictly under 60 characters, ending with #shorts."
    )
    thumbnail_hook: str = Field(
        description="Bold, punchy 3-4 word high-click thumbnail hook text in ALL CAPS (e.g. 'ONLY 6 CARS!', '$100M PHOTOCOPY', '350 KM/H CHAOS')."
    )
    hero_photo_query: str = Field(
        description="Most dramatic, specific Wikimedia Commons F1 action photo query for the main thumbnail."
    )
    description: str = Field(
        description="SEO-rich 2-sentence casual hook summary followed by #f1 #shorts #formula1 and 2-3 relevant topic hashtags."
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
        description="Brief check showing how the final scene's closing narration flows directly into Scene 0's opening hook."
    )
    scenes: list[SceneBeat] = Field(
        min_length=6,
        max_length=10,
        description="Ordered list of 6-10 short narration-matched visual scenes (~3-5s each, totaling 95-135 spoken words).",
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


def find_unexplained_jargon(scenes: list[SceneBeat]) -> list[str]:
    """Detect banned engineering jargon unless immediately translated into everyday language."""
    translation_cues = ("like a ", "like ", "basically ", "meaning ", "which feels ", "imagine ")
    hits: list[str] = []
    for scene in scenes:
        lowered = scene.narration.lower()
        for term in BANNED_JARGON_TERMS:
            pattern = r"\b" + re.escape(term.lower()) + r"\b"
            if re.search(pattern, lowered):
                if not any(cue in lowered for cue in translation_cues):
                    hits.append(term)
    return sorted(set(hits))


def normalize_title(title: str) -> str:
    """Ensure title is strictly under 60 characters and ends cleanly with #shorts."""
    clean = " ".join(title.strip().split())
    base = re.sub(r"\s*#shorts\b", "", clean, flags=re.IGNORECASE).strip(" -:|,")
    base = re.sub(r"\s*#f1\b", "", base, flags=re.IGNORECASE).strip(" -:|,")
    suffix = " #shorts"
    max_base = MAX_TITLE_LENGTH - len(suffix)
    if len(base) > max_base:
        trimmed = base[:max_base].rsplit(" ", 1)[0].rstrip(" -:|,")
        base = trimmed if trimmed else base[:max_base].strip()
    return f"{base}{suffix}"


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
    """Enforce scene-by-scene narration/visual alignment, casual storytelling, pacing, and metadata rules."""
    errors: list[str] = []

    if not (6 <= len(script.scenes) <= 10):
        errors.append(f"Script must have 6-10 scenes (got {len(script.scenes)}).")

    full_spoken = " ".join(s.narration.strip() for s in script.scenes)
    words = count_words(full_spoken)
    max_words_v4 = max(MAX_WORD_COUNT, 135)
    if words < MIN_WORD_COUNT:
        errors.append(f"Spoken script too short ({words} words; must be {MIN_WORD_COUNT}-{max_words_v4}).")
    elif words > max_words_v4:
        errors.append(f"Spoken script too long ({words} words; must be {MIN_WORD_COUNT}-{max_words_v4}).")

    combined_text = f"{script.title}\n{script.description}\n{full_spoken}"
    banned_hits = find_banned_phrases(combined_text)
    if banned_hits:
        errors.append(f"Banned AI clichés detected: {', '.join(banned_hits)}")

    jargon_hits = find_unexplained_jargon(script.scenes)
    if jargon_hits:
        errors.append(
            f"Unexplained technical jargon detected ({', '.join(jargon_hits)}). "
            "Remove it or translate it immediately into a vivid everyday comparison."
        )

    # Check Scene 0 hook (0-2s curiosity-gap opener)
    first_scene = script.scenes[0].narration.strip()
    if count_words(first_scene) > 20:
        errors.append(f"Opening hook scene is too long ({count_words(first_scene)} words; keep <= 18 words).")
    if re.match(r"^(hey|hello|hi|welcome|today we|in this)", first_scene.lower()):
        errors.append("Opening hook must start mid-action or with a curiosity gap — never a greeting.")

    # Check every scene has a specific visual_query AND a broader fallback_query
    weak_visual = [i for i, s in enumerate(script.scenes) if len(s.visual_query.strip().split()) < 3]
    if weak_visual:
        errors.append(
            f"Scenes with overly vague visual_query (<3 words): {weak_visual}. "
            "Include specific driver/team, action/setting, and track/event."
        )
    weak_fallback = [i for i, s in enumerate(script.scenes) if len(s.fallback_query.strip().split()) < 2]
    if weak_fallback:
        errors.append(f"Scenes missing context-accurate fallback_query: {weak_fallback}.")

    # Ensure visual_query values aren't copy-pasted across scenes
    unique_queries = {s.visual_query.strip().lower() for s in script.scenes}
    if len(unique_queries) < max(4, len(script.scenes) - 1):
        errors.append("Each scene must have a distinct visual_query matching that exact scene's narration.")

    # Check title length (< 60 chars) & #shorts ending
    if len(script.title.strip()) >= 60:
        errors.append(f"Title too long ({len(script.title.strip())} chars; must be strictly under 60 chars).")
    if not script.title.strip().lower().endswith("#shorts"):
        errors.append("Title must end with '#shorts'.")

    clean_tags = [t.strip().lstrip("#") for t in script.tags if t.strip()]
    if not (12 <= len(clean_tags) <= 15):
        errors.append(f"Expected 12-15 tags, got {len(clean_tags)}.")

    last_scene = script.scenes[-1].narration.strip().lower()
    for bad_end in ("subscribe", "like and follow", "thanks for watching", "see you next time"):
        if bad_end in last_scene:
            errors.append(f"Final scene contains non-loop sign-off ('{bad_end}'); end with a seamless loop bridge.")

    return errors


async def _write_validated_script_async(topic: dict[str, Any], max_attempts: int = 3) -> dict[str, Any]:
    """Generate and validate a Shorts v4 scene-by-scene narration-matched visual storyboard."""
    system_instructions = (
        "You are a charismatic, everyday Formula 1 storyteller and visual director writing viral "
        "44-to-54-second YouTube Shorts (Shorts v4) for casual F1 fans.\n\n"
        "CASUAL-FRIENDLY VOICE RULES:\n"
        "1. ZERO NERD JARGON: Never use unexplained engineering terms like CFD, venturi tunnels, bargeboards, "
        "porpoising, telemetry, brake migration, differential maps, or aero efficiency. If you touch a physical "
        "sensation, translate it immediately into a vivid everyday comparison (e.g., 'the car bouncing like a "
        "jackhammer at 320 km/h', 'braking so hard it feels like five people sitting on your chest', "
        "'changing four tires faster than you can blink twice').\n"
        "2. HUMAN STAKES FIRST: Focus on ego, rivalries, sheer speed, danger, pit-stop pressure, heartbreak, "
        "and relatable real-world comparisons.\n"
        "3. STRICTLY FORBIDDEN AI CLICHÉS (instant failure if used anywhere): "
        + ", ".join(f'"{p}"' for p in BANNED_AI_PHRASES)
        + ".\n\n"
        "SHORTS v4 SCENE-BY-SCENE VISUAL STORYBOARDING RULES:\n"
        "4. Return an ordered list of **7 to 9 scenes** (`scenes`, strictly inside 6-10). Each scene is "
        "1-2 punchy sentences (~3-5 seconds of narration, ~11-16 words), totaling **105 to 128 spoken words** "
        "(strictly inside 95-135 words).\n"
        "5. Scene 0 (0-2s Curiosity-Gap Hook): Open with an immediate curiosity gap or shocking contrast in the "
        "first 2 seconds—never a greeting.\n"
        "6. STRICT NARRATION-TO-IMAGE MATCHING (CRITICAL): For EVERY SINGLE scene:\n"
        "   - `narration`: The exact spoken lines for that scene.\n"
        "   - `visual_query`: A highly specific real-photograph search query matching the EXACT subject, "
        "setting, and race/event spoken in that scene's `narration`! Examples:\n"
        "       * Driver speaking post-race -> 'Max Verstappen post race interview press conference Las Vegas F1'\n"
        "       * Ferrari pit stop mistake -> 'Ferrari F1 pit stop crew mechanics race'\n"
        "       * Specific crash or overtake -> include driver names, teams, and track/year (e.g. 'Lewis Hamilton Max Verstappen Silverstone 2021 F1')\n"
        "       * Every scene MUST have a unique, fresh `visual_query`—never repeat generic queries!\n"
        "   - `fallback_query`: A slightly broader, still context-accurate F1 real-photo query for the same "
        "driver/team/setting in case `visual_query` returns fewer than 2 valid photos (e.g. 'Max Verstappen Red Bull F1', 'Ferrari F1 pit lane').\n"
        "   - `stat_badge`: Optional short 2-4 word on-screen callout ONLY when this scene mentions a concrete "
        "stat, quote, year, or lap number (e.g. '350+ KM/H', 'LAP 58', '1.80s STOP', '$100M FINE'). Leave '' when no stat/quote/lap is mentioned.\n"
        "7. SEAMLESS LOOP ENDING: Write the final scene's closing narration so its grammar and rhythm flow "
        "directly back into Scene 0's opening words, paired with a crisp debate question in `comment_question`.\n"
        "8. HIGH-CLICK THUMBNAIL & METADATA:\n"
        "   - `thumbnail_hook`: Bold 3-4 word ALL CAPS hook (e.g. 'ONLY 6 CARS!', '350 KM/H CHAOS').\n"
        "   - `hero_photo_query`: Most dramatic real F1 photo query for the custom thumbnail.\n"
        "   - `title`: Clickable curiosity-gap title STRICTLY under 60 characters, ending with `#shorts`.\n"
        "   - `description`: SEO-rich 2-sentence casual hook summary + `#f1 #shorts #formula1` + 2-3 topic hashtags.\n"
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
            "Write the Shorts v4 scene-by-scene narration-matched visual storyboard, thumbnail hook, "
            "and YouTube metadata for this verified F1 topic:\n\n"
            f"{json.dumps(topic, indent=2, ensure_ascii=False)}"
            f"{retry_note}\n\n"
            "Checklist before outputting JSON:\n"
            "- 7 to 9 ordered `scenes` (each 1-2 sentences, 105-128 total spoken words).\n"
            "- Every scene has a unique, specific `visual_query` matching its exact narration + a broader `fallback_query`.\n"
            "- Include `stat_badge` ONLY on scenes that mention a concrete stat, quote, year, or lap number.\n"
            "- Zero unexplained engineering jargon, zero AI clichés.\n"
            "- Title strictly under 60 characters ending with `#shorts`, `thumbnail_hook` is 3-4 bold words.\n"
            "- Final scene loops seamlessly back into Scene 0."
        )

        logger.info(
            "Writing Shorts v4 scene-by-scene storyboard for '%s' (attempt %d/%d)...",
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

        script.title = normalize_title(script.title)
        script.thumbnail_hook = normalize_thumbnail_hook(script.thumbnail_hook)
        script.description = normalize_description(script.description)
        script.tags = [t.strip().lstrip("#") for t in script.tags if t.strip()][:15]

        errors = validate_script(script)
        if not errors:
            full_spoken = " ".join(s.narration.strip() for s in script.scenes)
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
    """Synchronous entry point to generate and validate a Shorts v4 scene-by-scene script."""
    return asyncio.run(_write_validated_script_async(topic))
