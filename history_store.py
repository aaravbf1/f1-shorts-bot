"""Permanent anti-repetition history ledger backed by history.json with strict >40% similarity blocking."""

from __future__ import annotations

import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import HISTORY_FILE, TOPIC_OVERLAP_THRESHOLD

STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "into", "about", "what",
    "when", "where", "which", "while", "how", "why", "who", "was", "were", "are",
    "has", "have", "had", "been", "formula", "f1", "shorts", "driver", "drivers",
    "race", "racing", "car", "cars", "team", "teams", "ever", "most", "only",
    "grand", "prix", "one", "its", "their", "they", "over", "after", "once",
    "just", "than", "then", "during", "wild", "crazy", "insane", "story", "stories",
    "exposed", "banned", "cost", "win", "won", "history", "rules", "rule",
}


def slugify(text: str) -> str:
    """Convert any topic phrase into a canonical kebab-case slug."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)[:90]


def extract_keywords(text: str) -> set[str]:
    """Extract normalized topical keywords (excluding generic F1 stopwords)."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


class HistoryStore:
    """Manages history.json with atomic writes and strict >40% anti-repetition validation."""

    def __init__(self, path: Path = HISTORY_FILE) -> None:
        self.path = path
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not self.path.exists():
            self._write_raw({"uploads": []})

    def _read_raw(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("uploads"), list):
            return data["uploads"]
        return []

    def _write_raw(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)

    def get_all(self) -> list[dict[str, Any]]:
        """Return all recorded Shorts entries."""
        return self._read_raw()

    def get_recent_categories(self, limit: int = 4) -> list[str]:
        """Return the most recently used category IDs so runs diversify across fresh categories."""
        records = self._read_raw()
        return [str(r.get("category", "")).strip() for r in records[-limit:] if r.get("category")]

    def get_all_banned_keywords(self, max_items: int = 250) -> list[str]:
        """Return sorted unique topical keywords from every past entry in history.json."""
        kw_set: set[str] = set()
        for entry in self._read_raw()[-max_items:]:
            kw_set.update(
                extract_keywords(
                    " ".join(
                        [
                            str(entry.get("topic_slug", "")),
                            str(entry.get("title", "")),
                            str(entry.get("angle", "")),
                            " ".join(str(k) for k in (entry.get("keywords") or [])),
                        ]
                    )
                )
            )
        return sorted(kw_set)

    def get_summary_for_prompt(self, max_items: int = 250) -> str:
        """Render the complete exclusion list (titles, slugs, angles, keywords) for Gemini prompts."""
        records = self._read_raw()
        if not records:
            return "(None yet — fresh channel history.)"

        lines = ["PREVIOUSLY COVERED TOPICS & TITLES (PERMANENTLY BANNED):"]
        for entry in records[-max_items:]:
            slug = entry.get("topic_slug", "")
            title = entry.get("title", "")
            cat = entry.get("category", "")
            angle = entry.get("angle", "")
            extra = f" | Angle: {angle}" if angle else ""
            lines.append(f"- [{cat}] slug={slug} | title={title}{extra}")

        banned_kws = self.get_all_banned_keywords(max_items=max_items)
        if banned_kws:
            lines.append("")
            lines.append("BANNED TOPIC KEYWORDS / STORIES ALREADY USED (DO NOT REUSE SAME ANGLE OR DRIVER-INCIDENT):")
            lines.append(", ".join(banned_kws[:120]))
        return "\n".join(lines)

    def is_duplicate(
        self,
        topic_slug: str,
        title: str = "",
        summary: str = "",
        extra_reserved: list[dict[str, str]] | None = None,
        overlap_threshold: float = TOPIC_OVERLAP_THRESHOLD,
    ) -> tuple[bool, str]:
        """Return (True, reason) if candidate topic/title overlaps >40% with any entry in history.json."""
        cand_slug = slugify(topic_slug)
        cand_title = re.sub(r"#f1\b", "", title.lower()).strip()
        cand_title_kw = extract_keywords(f"{topic_slug} {cand_title}")
        cand_all_kw = extract_keywords(f"{topic_slug} {cand_title} {summary}")

        existing = list(self._read_raw())
        if extra_reserved:
            existing.extend(extra_reserved)

        for item in existing:
            ex_slug = slugify(str(item.get("topic_slug", "")))
            ex_title = re.sub(r"#f1\b", "", str(item.get("title", "")).lower()).strip()
            ex_summary = str(item.get("angle", "") or item.get("summary", ""))
            ex_explicit_kw = " ".join(str(k) for k in (item.get("keywords") or []))

            # 1. Exact slug match
            if cand_slug and ex_slug and cand_slug == ex_slug:
                return True, f"Exact slug match with '{ex_slug}'"

            # 2. Title or slug sequence similarity > 40% (strict check on normalized strings)
            if cand_slug and ex_slug:
                slug_tokens = extract_keywords(cand_slug.replace("-", " "))
                ex_slug_tokens = extract_keywords(ex_slug.replace("-", " "))
                if slug_tokens and ex_slug_tokens:
                    overlap = slug_tokens & ex_slug_tokens
                    ratio = len(overlap) / max(1, min(len(slug_tokens), len(ex_slug_tokens)))
                    if ratio > overlap_threshold:
                        return True, (
                            f"Slug keyword overlap {ratio:.0%} (> {overlap_threshold:.0%}: "
                            f"{sorted(overlap)}) with '{ex_slug}'"
                        )

            if cand_title and ex_title:
                title_seq = difflib.SequenceMatcher(None, cand_title, ex_title).ratio()
                if title_seq > 0.52:
                    return True, f"Title similarity {title_seq:.0%} with '{ex_title}'"

            # 3. Title+slug keyword overlap > 40%
            ex_title_kw = extract_keywords(f"{ex_slug} {ex_title} {ex_explicit_kw}")
            if cand_title_kw and ex_title_kw:
                overlap_t = cand_title_kw & ex_title_kw
                denom_t = min(len(cand_title_kw), len(ex_title_kw))
                if denom_t >= 2 and (len(overlap_t) / denom_t) > overlap_threshold:
                    return True, (
                        f"Title/topic keyword overlap {len(overlap_t)}/{denom_t} "
                        f"({len(overlap_t) / denom_t:.0%} > {overlap_threshold:.0%}: {sorted(overlap_t)}) "
                        f"with '{ex_slug}'"
                    )

            # 4. Full topic+angle keyword overlap > 40%
            ex_all_kw = extract_keywords(f"{ex_slug} {ex_title} {ex_summary} {ex_explicit_kw}")
            if cand_all_kw and ex_all_kw:
                overlap_all = cand_all_kw & ex_all_kw
                denom_all = min(len(cand_all_kw), len(ex_all_kw))
                if denom_all >= 3 and (len(overlap_all) / denom_all) > overlap_threshold:
                    return True, (
                        f"Topic/angle keyword overlap {len(overlap_all)}/{denom_all} "
                        f"({len(overlap_all) / denom_all:.0%} > {overlap_threshold:.0%}: {sorted(overlap_all)}) "
                        f"with '{ex_slug}'"
                    )

        return False, ""

    def record_upload(
        self,
        *,
        topic_slug: str,
        category: str,
        title: str,
        video_id: str,
        angle: str = "",
        upload_date: str | None = None,
    ) -> dict[str, Any]:
        """Persist a newly uploaded Short (with extracted keywords) into history.json atomically."""
        records = self._read_raw()
        kw = sorted(extract_keywords(f"{topic_slug} {title} {angle}"))[:14]
        record: dict[str, Any] = {
            "topic_slug": slugify(topic_slug),
            "category": category,
            "title": title,
            "video_id": video_id,
            "upload_date": upload_date or datetime.now(timezone.utc).isoformat(),
        }
        if angle:
            record["angle"] = angle
        if kw:
            record["keywords"] = kw

        records.append(record)
        self._write_raw({"uploads": records})
        return record
