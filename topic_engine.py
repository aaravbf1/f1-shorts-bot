"""Randomized Formula 1 Story & Trend Engine powered by Google Antigravity SDK (gemini-4-hydrogen + Google Search)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import requests
from google.antigravity import Agent, BuiltinTools, CapabilitiesConfig, LocalAgentConfig
from pydantic import BaseModel, Field

from config import (
    ANTIGRAVITY_MODEL,
    F1_CATEGORIES,
    GCP_LOCATION,
    GCP_PROJECT_ID,
    GEMINI_API_KEY,
    VERTEX_ENABLED,
)
from history_store import HistoryStore, slugify

logger = logging.getLogger(__name__)

LIVE_F1_RSS_FEEDS = [
    ("BBC Sport F1", "https://feeds.bbci.co.uk/sport/formula1/rss.xml"),
    ("ESPN F1", "https://www.espn.com/espn/rss/f1/news"),
    ("Autosport F1", "https://www.autosport.com/rss/f1/news/"),
    ("Motorsport.com F1", "https://www.motorsport.com/rss/f1/news/"),
]


def fetch_live_f1_headlines(max_per_feed: int = 5) -> str:
    """Fetch the latest verified Formula 1 news headlines and summaries from BBC, ESPN, Autosport, and Motorsport.com."""
    collected: list[str] = []
    for source_name, url in LIVE_F1_RSS_FEEDS:
        try:
            resp = requests.get(
                url,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) F1ShortsEngine/1.0"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")[:max_per_feed]
            if not items:
                continue
            collected.append(f"=== {source_name} ===")
            for item in items:
                title = (item.findtext("title") or "").strip()
                desc = re.sub(r"<[^>]+>", "", (item.findtext("description") or "")).strip()
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    line = f"- {title}"
                    if pub:
                        line += f" ({pub})"
                    if desc:
                        line += f": {desc[:220]}"
                    collected.append(line)
        except Exception as exc:
            logger.debug("RSS feed %s unavailable: %s", source_name, exc)

    if not collected:
        return "No live RSS feeds reachable right now."
    return "\n".join(collected)


class F1TopicCandidate(BaseModel):
    """Structured F1 Short topic verified via Google Search grounding or live F1 feeds."""

    topic_slug: str = Field(
        description="Short unique kebab-case identifier, e.g. '2005-us-gp-six-cars-only'."
    )
    working_title: str = Field(
        description="Punchy curiosity-driven working title under 54 characters including #f1."
    )
    angle: str = Field(
        description="One-sentence dramatic human angle focusing on ego, danger, speed, or controversy."
    )
    verified_facts: list[str] = Field(
        min_length=3,
        max_length=6,
        description="3-6 concrete, accurate F1 details (numbers, years, quotes, drivers).",
    )
    shocking_stat: str = Field(
        description="One big visual stat for a stat callout card (e.g. '6 CARS', '1.80s', '67G', '$100K')."
    )
    stat_context: str = Field(
        description="Short 3-6 word label explaining the shocking stat."
    )
    notable_quote: str = Field(
        default="",
        description="Short real or iconic team-radio/paddock quote if applicable (<= 14 words).",
    )
    quote_speaker: str = Field(
        default="",
        description="Who said the quote (e.g. 'Max Verstappen', 'Toto Wolff', 'Race Control').",
    )
    comparison_left: str = Field(
        default="",
        description="Optional left entity for a Driver vs. Driver / Team vs. Team visual card.",
    )
    comparison_right: str = Field(
        default="",
        description="Optional right entity for a Driver vs. Driver / Team vs. Team visual card.",
    )
    primary_team_color: str = Field(
        default="f1_red",
        description="One of: f1_red, ferrari, red_bull, mercedes, mclaren, aston_martin, alpine, williams, haas, sauber, rb, gold.",
    )
    secondary_team_color: str = Field(
        default="gold",
        description="Contrasting accent from the same palette list.",
    )
    source_summary: str = Field(
        description="Brief note citing the source used (Google Search, live F1 RSS feed, or established F1 record)."
    )


def build_antigravity_config(
    *,
    system_instructions: str,
    enable_web_search: bool,
    response_schema: type[BaseModel] | None = None,
    extra_tools: list[Any] | None = None,
) -> LocalAgentConfig:
    """Construct a LocalAgentConfig targeting gemini-4-hydrogen with optional Google Search grounding."""
    enabled_tools = (
        [BuiltinTools.SEARCH_WEB, BuiltinTools.READ_URL_CONTENT, BuiltinTools.VIEW_FILE]
        if enable_web_search
        else []
    )
    kwargs: dict[str, Any] = {
        "model": ANTIGRAVITY_MODEL,
        "system_instructions": system_instructions,
        "capabilities": CapabilitiesConfig(
            enable_subagents=False,
            enabled_tools=enabled_tools,
        ),
    }
    if extra_tools:
        kwargs["tools"] = extra_tools
    if response_schema is not None:
        kwargs["response_schema"] = response_schema
    if GEMINI_API_KEY:
        kwargs["api_key"] = GEMINI_API_KEY
    if VERTEX_ENABLED:
        kwargs["vertex"] = True
        if GCP_PROJECT_ID:
            kwargs["project"] = GCP_PROJECT_ID
        if GCP_LOCATION:
            kwargs["location"] = GCP_LOCATION

    return LocalAgentConfig(**kwargs)


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Extract a JSON object cleanly even if wrapped in markdown code fences."""
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            cleaned = cleaned[first_brace : last_brace + 1]
    return json.loads(cleaned)


async def _parse_agent_response(response, schema_cls: type[BaseModel]) -> BaseModel:
    """Resolve structured_output if present, otherwise parse JSON from response.text()."""
    try:
        structured = getattr(response, "structured_output", None)
        if callable(structured):
            structured = await structured()
        if isinstance(structured, schema_cls):
            return structured
        if isinstance(structured, dict):
            return schema_cls.model_validate(structured)
    except Exception:
        pass

    text = await response.text()
    data = extract_json_payload(text)
    return schema_cls.model_validate(data)


async def _discover_topic_for_category(
    category: dict[str, Any],
    history_store: HistoryStore,
    reserved_topics: list[dict[str, str]],
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Use gemini-4-hydrogen + Google Search grounding (with live RSS fallback) to find 1 unique F1 topic."""
    today_str = datetime.now().strftime("%B %d, %Y")
    history_ledger = history_store.get_summary_for_prompt()
    reserved_block = (
        "\n".join(f"- [{r['category']}] slug={r['topic_slug']} | title={r['title']}" for r in reserved_topics)
        if reserved_topics
        else "(None)"
    )

    system_instructions = (
        "You are an obsessive, viral Formula 1 YouTube Shorts producer. "
        "Pick stories with massive mainstream curiosity—human stakes, ego, chaos, speed, danger, "
        "or outrageous loopholes—never boring engineering lectures. "
        "GROUNDING WORKFLOW:\n"
        "1. First try `search_web` once to ground and verify facts with Google Search.\n"
        "2. If `search_web` fails with a 429 quota/rate-limit error, do NOT retry `search_web` repeatedly and do NOT refuse. "
        "Instead, immediately call `fetch_live_f1_headlines` (for current paddock news/drama) or use well-established, "
        "high-confidence Formula 1 history/rules facts (for evergreen categories) and note the exact source in `source_summary`.\n"
        "3. ALWAYS complete the task by returning valid JSON matching the requested schema."
    )

    config = build_antigravity_config(
        system_instructions=system_instructions,
        enable_web_search=True,
        response_schema=F1TopicCandidate,
        extra_tools=[fetch_live_f1_headlines],
    )

    rejection_notes: list[str] = []
    async with Agent(config) as agent:
        for attempt in range(1, max_attempts + 1):
            live_news_hint = (
                f"Today's date is {today_str}. Get the freshest Formula 1 paddock drama, "
                "driver clash, steward penalty, team radio flashpoint, or contract story from this week "
                "(try `search_web`, and call `fetch_live_f1_headlines` if `search_web` hits quota)."
                if category.get("requires_live_news")
                else (
                    f"Today's date is {today_str}. Pick an unforgettable, high-retention Formula 1 story "
                    "in this category. Verify details with `search_web` (or proceed with iconic verified F1 "
                    "records if `search_web` hits a 429 quota error)."
                )
            )

            retry_section = (
                "\n\nPREVIOUS REJECTED ATTEMPTS (DO NOT REPEAT):\n" + "\n".join(f"- {r}" for r in rejection_notes)
                if rejection_notes
                else ""
            )

            prompt = (
                f"Select ONE killer, non-repeating Formula 1 YouTube Short topic.\n\n"
                f"CATEGORY: {category['label']}\n"
                f"CATEGORY FOCUS: {category['description']}\n"
                f"RESEARCH INSTRUCTION: {live_news_hint}\n\n"
                f"PERMANENT UPLOAD HISTORY (STRICTLY FORBIDDEN TO REPEAT ANY TOPIC OR CORE FACT):\n"
                f"{history_ledger}\n\n"
                f"ALREADY SELECTED FOR TODAY'S RUN (DO NOT OVERLAP):\n"
                f"{reserved_block}"
                f"{retry_section}\n\n"
                "Return ONLY the structured JSON object matching F1TopicCandidate."
            )

            logger.info(
                "Querying %s + Google Search for category '%s' (attempt %d/%d)...",
                ANTIGRAVITY_MODEL,
                category["label"],
                attempt,
                max_attempts,
            )
            response = await agent.chat(prompt)
            try:
                candidate: F1TopicCandidate = await _parse_agent_response(response, F1TopicCandidate)  # type: ignore[assignment]
            except Exception as parse_err:
                logger.warning("Attempt %d returned unparseable response (%s); retrying...", attempt, parse_err)
                rejection_notes.append(f"Attempt {attempt} did not return valid JSON matching schema.")
                continue

            norm_slug = slugify(candidate.topic_slug or candidate.working_title)

            is_dup, reason = history_store.is_duplicate(
                topic_slug=norm_slug,
                title=candidate.working_title,
                summary=candidate.angle,
                extra_reserved=reserved_topics,
            )
            if not is_dup:
                topic_dict = candidate.model_dump()
                topic_dict["topic_slug"] = norm_slug
                topic_dict["category"] = category["id"]
                topic_dict["category_label"] = category["label"]
                logger.info("Verified unique topic [%s]: %s", category["id"], norm_slug)
                return topic_dict

            logger.warning("Topic '%s' rejected for overlap: %s", norm_slug, reason)
            rejection_notes.append(f"{norm_slug} ({candidate.working_title}) -> rejected: {reason}")

    raise RuntimeError(
        f"Failed to generate a non-repeating topic for category '{category['label']}' after {max_attempts} attempts."
    )


async def _generate_unique_topics_async(history_store: HistoryStore, count: int = 2) -> list[dict[str, Any]]:
    chosen_categories = random.sample(F1_CATEGORIES, k=min(count, len(F1_CATEGORIES)))
    selected: list[dict[str, Any]] = []
    reserved: list[dict[str, str]] = []

    for cat in chosen_categories:
        topic = await _discover_topic_for_category(cat, history_store, reserved_topics=reserved)
        selected.append(topic)
        reserved.append(
            {
                "category": topic["category"],
                "topic_slug": topic["topic_slug"],
                "title": topic["working_title"],
                "angle": topic["angle"],
            }
        )
    return selected


def generate_unique_topics(history_store: HistoryStore, count: int = 2) -> list[dict[str, Any]]:
    """Pick `count` distinct F1 categories and generate verified, non-repeating topics."""
    return asyncio.run(_generate_unique_topics_async(history_store=history_store, count=count))
