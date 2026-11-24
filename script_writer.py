"""Human Storyteller & Seamless-Loop Shorts Scriptwriter with strict anti-AI validation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from google.antigravity import Agent
from pydantic import BaseModel, Field

from config import (
    ANTIGRAVITY_MODEL,
    BANNED_AI_PHRASES,
    MAX_TITLE_LENGTH,
    MAX_WORD_COUNT,
    MIN_WORD_COUNT,
)
from topic_engine import _parse_agent_response, build_antigravity_config

logger = logging.getLogger(__name__)


class VisualBeat(BaseModel):
    """A single 3-4 second narration + visual card beat inside a YouTube Short."""

    spoken_text: str = Field(
        description="Punchy spoken narration for this 3-4 second beat (10-18 conversational words)."
    )
    card_type: Literal["story_card", "quote_callout", "big_stat", "driver_vs_driver"] = Field(
        description="Visual card template to display during this beat."
    )
    card_header: str = Field(
        description="Short, bold on-screen headline (2-7 words)."
    )
    card_subtext: str = Field(
        default="",
        description="Concise supporting visual detail (4-12 words).",
    )
    stat_value: str = Field(
        default="",
        description="Huge single number/metric when card_type=='big_stat' (e.g. '6 CARS', '67G', '1.80s').",
    )
    stat_label: str = Field(
        default="",
        description="Short label below stat_value when card_type=='big_stat'.",
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
    """Complete Shorts script, seamless loop bridge, storyboard beats, and high-CTR metadata."""

    title: str = Field(
        description="Curiosity-driven YouTube Shorts title strictly under 55 characters, naturally including #f1."
    )
    description: str = Field(
        description="2-sentence hook summary followed by #f1 #shorts #formula1 and 2-3 relevant topic hashtags."
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
        max_length=12,
        description="8-12 fast visual/story beats totaling 110-145 spoken words.",
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


def normalize_title(title: str) -> str:
    """Ensure title includes #f1 and stays strictly under 55 characters."""
    clean = " ".join(title.strip().split())
    if "#f1" not in clean.lower():
        clean = f"{clean} #f1"

    if len(clean) <= MAX_TITLE_LENGTH:
        return clean

    # Trim headline cleanly while preserving trailing #f1
    base = re.sub(r"\s*#f1\b", "", clean, flags=re.IGNORECASE).strip(" -:|,")
    max_base = MAX_TITLE_LENGTH - 4  # space + "#f1"
    trimmed = base[:max_base].rsplit(" ", 1)[0].rstrip(" -:|,")
    if not trimmed:
        trimmed = base[:max_base].strip()
    return f"{trimmed} #f1"


def normalize_description(description: str) -> str:
    """Guarantee #f1 #shorts #formula1 hashtags are present in the description."""
    clean = description.strip()
    lower = clean.lower()
    missing = [tag for tag in ("#f1", "#shorts", "#formula1") if tag not in lower]
    if missing:
        clean = f"{clean}\n\n{' '.join(missing)}"
    return clean


def validate_script(script: F1ShortScript) -> list[str]:
    """Enforce anti-AI clichés, word-count pacing, hook/loop quality, and metadata rules."""
    errors: list[str] = []

    full_spoken = " ".join(b.spoken_text.strip() for b in script.beats)
    words = count_words(full_spoken)
    if words < MIN_WORD_COUNT:
        errors.append(f"Spoken script too short ({words} words; must be {MIN_WORD_COUNT}-{MAX_WORD_COUNT}).")
    elif words > MAX_WORD_COUNT:
        errors.append(f"Spoken script too long ({words} words; must be {MIN_WORD_COUNT}-{MAX_WORD_COUNT}).")

    # Check banned clichés in spoken narration + title + description
    combined_text = f"{script.title}\n{script.description}\n{full_spoken}"
    banned_hits = find_banned_phrases(combined_text)
    if banned_hits:
        errors.append(f"Banned AI clichés detected: {', '.join(banned_hits)}")

    # Check Beat 0 hook isn't a greeting and is short/punchy (0-2.5s scroll-stopper)
    first_beat = script.beats[0].spoken_text.strip()
    if count_words(first_beat) > 22:
        errors.append(f"Opening hook beat is too long ({count_words(first_beat)} words; keep <= 20 words for 0-2.5s).")
    if re.match(r"^(hey|hello|hi|welcome|today we|in this)", first_beat.lower()):
        errors.append("Opening hook must start mid-action or with a shocking stat — never a greeting.")

    # Check visual variety (at least 2 distinct card types across the Short)
    card_types = {b.card_type for b in script.beats}
    if len(card_types) < 2:
        errors.append("Visual storyboard needs variety (use at least 2 distinct card_types across beats).")

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
    """Generate and validate a human-sounding, seamless-loop F1 Short script."""
    system_instructions = (
        "You are a charismatic human Formula 1 storyteller writing viral 45-to-52-second YouTube Shorts. "
        "VOICE & STYLE RULES:\n"
        "1. Zero Jargon Overload: Write like a sharp friend telling an insane F1 story over coffee—focus on ego, "
        "speed, danger, money, betrayal, and drama in plain, vivid conversational English.\n"
        "2. STRICTLY FORBIDDEN WORDS/PHRASES (instant failure if used anywhere): "
        + ", ".join(f'"{p}"' for p in BANNED_AI_PHRASES)
        + ".\n"
        "3. RETENTION & SEAMLESS LOOP STRUCTURE:\n"
        "   - Beat 0 (0-2.5s Scroll-Stopper): Start mid-action or with a jaw-dropping fact. Never say 'Hey guys' or 'Welcome back'.\n"
        "   - Total spoken length across ALL beats MUST be between 130 and 144 words (strictly inside 110-145, calibrated to 42-55 seconds at +6% TTS speed).\n"
        "   - Split into 9 or 10 punchy beats (every 3-4 seconds = ~12-16 words per beat) so visuals constantly refresh.\n"
        "   - Mix card_type values across beats: 'story_card', 'big_stat', 'quote_callout', and 'driver_vs_driver'.\n"
        "   - SEAMLESS LOOP ENDING: Write the final beat's last sentence so its grammar and rhythm flow DIRECTLY "
        "into Beat 0's first words when the Short repeats (for example, ending '...and that's the insane reason...' "
        "feeding directly into Beat 0: 'only six cars started the 2005 US Grand Prix!'), while also weaving a quick "
        "debate prompt right before the loop bridge (and putting the short debate question in comment_question).\n"
        "4. METADATA:\n"
        "   - title: Curiosity-driven, STRICTLY under 55 characters, naturally ending with or containing #f1.\n"
        "   - description: 2 punchy hook sentences + #f1 #shorts #formula1 + 2-3 topic hashtags.\n"
        "   - tags: 12-15 high-volume F1 tags.\n"
        "Return ONLY valid JSON matching the schema."
    )

    config = build_antigravity_config(
        system_instructions=system_instructions,
        enable_web_search=False,
        response_schema=F1ShortScript,
    )

    feedback_history: list[str] = []
    async with Agent(config) as agent:
        for attempt in range(1, max_attempts + 1):
            retry_note = ""
            if feedback_history:
                retry_note = (
                    "\n\nFIX THESE VALIDATION ERRORS FROM YOUR PREVIOUS DRAFT:\n"
                    + "\n".join(f"- {err}" for err in feedback_history)
                )

            prompt = (
                "Write the complete YouTube Short script, beat-by-beat visual storyboard, and metadata "
                "for this verified Formula 1 topic:\n\n"
                f"{json.dumps(topic, indent=2, ensure_ascii=False)}"
                f"{retry_note}\n\n"
                "Double-check that total spoken words across all beats is 118-138 words, "
                "zero banned AI phrases appear anywhere, title is under 55 characters with #f1, "
                "and the last sentence loops seamlessly back into the opening sentence."
            )

            logger.info(
                "Writing human-storyteller script via %s for '%s' (attempt %d/%d)...",
                ANTIGRAVITY_MODEL,
                topic["topic_slug"],
                attempt,
                max_attempts,
            )
            response = await agent.chat(prompt)
            script: F1ShortScript = await _parse_agent_response(response, F1ShortScript)  # type: ignore[assignment]

            # Auto-sanitize harmless formatting edges before strict validation
            script.title = normalize_title(script.title)
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
