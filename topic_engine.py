"""Randomized Formula 1 Story & Trend Engine powered by Google Antigravity SDK (gemini-4-hydrogen + live grounding)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, TypeVar

import requests
from google.antigravity import (
    Agent,
    BuiltinTools,
    CapabilitiesConfig,
    LocalAgentConfig,
    ModelAPIRetryConfig,
    ModelOutputRetryConfig,
    RetryConfig,
)
from pydantic import BaseModel, Field

from config import (
    ANTIGRAVITY_FALLBACK_MODELS,
    ANTIGRAVITY_MODEL,
    F1_CATEGORIES,
    GCP_LOCATION,
    GCP_PROJECT_ID,
    GEMINI_API_KEY,
    HTTP_TIMEOUT_SEC,
    LLM_CALL_TIMEOUT_SEC,
    VERTEX_ENABLED,
)
from history_store import HistoryStore, slugify

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

LIVE_F1_RSS_FEEDS = [
    ("BBC Sport F1", "https://feeds.bbci.co.uk/sport/formula1/rss.xml"),
    ("ESPN F1", "https://www.espn.com/espn/rss/f1/news"),
    ("Autosport F1", "https://www.autosport.com/rss/f1/news/"),
    ("Motorsport.com F1", "https://www.motorsport.com/rss/f1/news/"),
]


def fetch_live_f1_headlines(max_per_feed: int = 5) -> str:
    """Fetch verified live Formula 1 news headlines and summaries with strict HTTP timeouts."""
    collected: list[str] = []
    for source_name, url in LIVE_F1_RSS_FEEDS:
        try:
            resp = requests.get(
                url,
                timeout=min(8, HTTP_TIMEOUT_SEC),
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) F1ShortsEngine/1.0"},
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
        return "(Live RSS feeds temporarily unreachable; use verified recent F1 facts.)"
    return "\n".join(collected)


class F1TopicCandidate(BaseModel):
    """Structured F1 Short topic verified via live F1 sources or established F1 records."""

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
        description="Brief note citing the source used (live F1 feed, Google Search, or official F1 record)."
    )


def get_model_chain() -> list[str]:
    """Return deduplicated primary + fallback model chain."""
    chain: list[str] = []
    for m in (ANTIGRAVITY_MODEL, *ANTIGRAVITY_FALLBACK_MODELS):
        if m and m not in chain:
            chain.append(m)
    return chain


def build_antigravity_config(
    *,
    system_instructions: str,
    enable_web_search: bool,
    response_schema: type[BaseModel] | None = None,
    extra_tools: list[Any] | None = None,
    model_name: str = ANTIGRAVITY_MODEL,
) -> LocalAgentConfig:
    """Construct a fail-fast LocalAgentConfig with bounded retries so 429/503 never hangs CI."""
    enabled_tools = (
        [BuiltinTools.SEARCH_WEB, BuiltinTools.READ_URL_CONTENT, BuiltinTools.VIEW_FILE]
        if enable_web_search
        else []
    )
    kwargs: dict[str, Any] = {
        "model": model_name,
        "system_instructions": system_instructions,
        "capabilities": CapabilitiesConfig(
            enable_subagents=False,
            enabled_tools=enabled_tools,
        ),
        # Cap retries tightly so a free-tier 429 RetryInfo (e.g. 3.5h) fails fast instead of hanging!
        "retry_config": RetryConfig(
            api_retry=ModelAPIRetryConfig(
                max_retries=1,
                initial_sleep_duration_ms=2000,
                exponential_multiplier=1.5,
            ),
            model_output_retry=ModelOutputRetryConfig(max_retries=1),
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


async def _parse_agent_response(response, schema_cls: type[TModel]) -> TModel:
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


async def run_structured_agent_prompt(
    *,
    system_instructions: str,
    prompt: str,
    schema_cls: type[TModel],
    enable_web_search: bool = False,
    timeout_sec: float = LLM_CALL_TIMEOUT_SEC,
) -> TModel:
    """Execute a single-turn Antigravity SDK prompt with strict timeout + automatic model fallback.

    Prevents infinite hangs when a free-tier model returns a multi-hour 429 retryDelay.
    """
    last_error: Exception | None = None
    models = get_model_chain()

    for idx, model_name in enumerate(models):
        config = build_antigravity_config(
            system_instructions=system_instructions,
            enable_web_search=enable_web_search,
            response_schema=schema_cls,
            model_name=model_name,
        )

        async def _call_once() -> TModel:
            async with Agent(config) as agent:
                response = await agent.chat(prompt)
                return await _parse_agent_response(response, schema_cls)

        try:
            logger.info(
                "Calling Antigravity SDK model '%s' (timeout=%.0fs, attempt %d/%d)...",
                model_name,
                timeout_sec,
                idx + 1,
                len(models),
            )
            return await asyncio.wait_for(_call_once(), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            last_error = TimeoutError(
                f"Model '{model_name}' timed out after {timeout_sec:.0f}s (likely 429/503 retry wait)."
            )
            logger.warning("%s Trying fallback model if available...", last_error)
        except Exception as exc:
            last_error = exc
            logger.warning("Model '%s' failed (%s). Trying fallback model if available...", model_name, exc)

    raise RuntimeError(
        f"All Antigravity models failed ({', '.join(models)}): {last_error}"
    ) from last_error


async def _discover_topic_for_category(
    category: dict[str, Any],
    history_store: HistoryStore,
    reserved_topics: list[dict[str, str]],
    live_headlines_cache: str,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Select 1 verified, non-repeating F1 topic in a single fast quota-friendly LLM call."""
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
        "Use ONLY verified facts from the provided live F1 news feeds (for current paddock news/drama) "
        "or well-established, rock-solid Formula 1 historical/rule records (for history, facts, and debates). "
        "Return ONLY valid JSON matching the requested schema."
    )

    rejection_notes: list[str] = []
    for attempt in range(1, max_attempts + 1):
        live_news_block = (
            f"\nLIVE VERIFIED F1 NEWS FEEDS (fetched {today_str} from BBC Sport F1, ESPN F1, Autosport, Motorsport.com):\n"
            f"{live_headlines_cache}\n"
            if category.get("requires_live_news")
            else (
                f"\nToday's date is {today_str}. Pick an unforgettable, high-retention Formula 1 story "
                "from established F1 history, FIA rules, or iconic rivalries (100% accurate facts only)."
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
            f"{live_news_block}\n"
            f"PERMANENT UPLOAD HISTORY (STRICTLY FORBIDDEN TO REPEAT ANY TOPIC OR CORE FACT):\n"
            f"{history_ledger}\n\n"
            f"ALREADY SELECTED FOR TODAY'S RUN (DO NOT OVERLAP):\n"
            f"{reserved_block}"
            f"{retry_section}\n\n"
            "Output ONLY the structured JSON object matching F1TopicCandidate."
        )

        logger.info(
            "Generating unique topic for category '%s' (attempt %d/%d)...",
            category["label"],
            attempt,
            max_attempts,
        )
        try:
            candidate = await run_structured_agent_prompt(
                system_instructions=system_instructions,
                prompt=prompt,
                schema_cls=F1TopicCandidate,
                enable_web_search=False,
            )
        except Exception as exc:
            logger.warning("Topic attempt %d failed: %s", attempt, exc)
            rejection_notes.append(f"Attempt {attempt} failed: {exc}")
            if attempt == max_attempts:
                raise
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
    live_headlines_cache = ""
    if any(cat.get("requires_live_news") for cat in chosen_categories):
        logger.info("Pre-fetching live F1 headlines (BBC/ESPN/Autosport/Motorsport)...")
        live_headlines_cache = fetch_live_f1_headlines(max_per_feed=5)

    selected: list[dict[str, Any]] = []
    reserved: list[dict[str, str]] = []

    for cat in chosen_categories:
        topic = await _discover_topic_for_category(
            cat,
            history_store,
            reserved_topics=reserved,
            live_headlines_cache=live_headlines_cache,
        )
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
