"""Permanent anti-repetition history ledger backed by history.json."""

from __future__ import annotations

import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import HISTORY_FILE

STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "into", "about", "what",
    "when", "where", "which", "while", "how", "why", "who", "was", "were", "are",
    "has", "have", "had", "been", "formula", "f1", "shorts", "driver", "drivers",
    "race", "racing", "car", "cars", "team", "teams", "ever", "most", "only",
}


def slugify(text: str) -> str:
    """Convert any topic phrase into a canonical kebab-case slug."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)[:90]


def _extract_keywords(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


class HistoryStore:
    """Manages history.json with atomic writes and anti-repetition validation."""

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

    def get_summary_for_prompt(self, max_items: int = 200) -> str:
        """Render a compact list of previously covered topics for LLM prompts."""
        records = self._read_raw()
        if not records:
            return "(None yet — this is a fresh channel history.)"

        lines = []
        for entry in records[-max_items:]:
            slug = entry.get("topic_slug", "")
            title = entry.get("title", "")
            cat = entry.get("category", "")
            angle = entry.get("angle", "")
            extra = f" | Angle: {angle}" if angle else ""
            lines.append(f"- [{cat}] slug={slug} | title={title}{extra}")
        return "\n".join(lines)

    def is_duplicate(
        self,
        topic_slug: str,
        title: str = "",
        summary: str = "",
        extra_reserved: list[dict[str, str]] | None = None,
    ) -> tuple[bool, str]:
        """Check whether a candidate topic overlaps with history.json or in-flight topics."""
        cand_slug = slugify(topic_slug)
        cand_title = title.lower().strip()
        cand_keywords = _extract_keywords(f"{topic_slug} {title} {summary}")

        existing = list(self._read_raw())
        if extra_reserved:
            existing.extend(extra_reserved)

        for item in existing:
            ex_slug = slugify(str(item.get("topic_slug", "")))
            ex_title = str(item.get("title", "")).lower().strip()
            ex_summary = str(item.get("angle", "") or item.get("summary", ""))

            if cand_slug and ex_slug and cand_slug == ex_slug:
                return True, f"Exact slug match with '{ex_slug}'"

            if cand_slug and ex_slug:
                slug_ratio = difflib.SequenceMatcher(None, cand_slug, ex_slug).ratio()
                if slug_ratio >= 0.74:
                    return True, f"High slug similarity ({slug_ratio:.2f}) with '{ex_slug}'"

            if cand_title and ex_title:
                title_ratio = difflib.SequenceMatcher(None, cand_title, ex_title).ratio()
                if title_ratio >= 0.72:
                    return True, f"High title similarity ({title_ratio:.2f}) with '{ex_title}'"

            ex_keywords = _extract_keywords(f"{ex_slug} {ex_title} {ex_summary}")
            if cand_keywords and ex_keywords:
                overlap = cand_keywords & ex_keywords
                smaller = min(len(cand_keywords), len(ex_keywords))
                if smaller >= 3 and (len(overlap) / smaller) >= 0.65:
                    return True, (
                        f"High fact/keyword overlap ({len(overlap)}/{smaller}: "
                        f"{sorted(overlap)}) with '{ex_slug}'"
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
        """Persist a newly uploaded Short into history.json atomically."""
        records = self._read_raw()
        record = {
            "topic_slug": slugify(topic_slug),
            "category": category,
            "title": title,
            "video_id": video_id,
            "upload_date": upload_date or datetime.now(timezone.utc).isoformat(),
        }
        if angle:
            record["angle"] = angle

        records.append(record)
        self._write_raw({"uploads": records})
        return record
