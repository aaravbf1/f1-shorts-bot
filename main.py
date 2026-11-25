"""CLI orchestrator for the Autonomous F1 YouTube Shorts Pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from asset_generator import ensure_all_assets
from config import (
    LOGS_DIR,
    OUTPUT_DIR,
    SHORTS_PER_RUN,
    TTS_PITCH,
    TTS_RATE,
    TTS_VOICE,
    YOUTUBE_HTTP_TIMEOUT_SEC,
    YOUTUBE_PRIVACY_STATUS,
    ensure_directories,
)
from history_store import HistoryStore
from script_writer import write_validated_script
from topic_engine import generate_unique_topics
from video_builder import build_short_video
from youtube_uploader import (
    QuotaExceededError,
    authenticate_youtube,
    materialize_credentials_from_env,
    upload_short,
    verify_connected_channel,
)


def setup_logging() -> Path:
    """Configure unbuffered live console + daily file logging under logs/."""
    ensure_directories()
    socket.setdefaulttimeout(YOUTUBE_HTTP_TIMEOUT_SEC)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    log_file = LOGS_DIR / f"f1_shorts_{datetime.now().strftime('%Y-%m-%d')}.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return log_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous Formula 1 YouTube Shorts Engine (9:16, 40-58s, Gemini 4 Hydrogen + YouTube API v3)"
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Run YouTube OAuth 2.0 login, print Connected Channel Name & Channel ID, save token.json, and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate topics, scripts, and 9:16 MP4 videos into output/ without uploading to YouTube or updating history.json.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=SHORTS_PER_RUN,
        help=f"Number of Shorts to generate per run (default: {SHORTS_PER_RUN}).",
    )
    parser.add_argument(
        "--voice",
        type=str,
        default=TTS_VOICE,
        help=f"edge-tts voice name (default: {TTS_VOICE}; options: en-US-AndrewMultilingualNeural, en-US-BrianMultilingualNeural).",
    )
    parser.add_argument(
        "--privacy",
        type=str,
        choices=["private", "unlisted", "public"],
        default=YOUTUBE_PRIVACY_STATUS,
        help=f"YouTube privacyStatus override (default from .env: {YOUTUBE_PRIVACY_STATUS}).",
    )
    return parser.parse_args()


def run_pipeline(args: argparse.Namespace) -> int:
    logger = logging.getLogger("main")
    logger.info("[Shorts v3] Pipeline initialized")
    ensure_directories()
    materialize_credentials_from_env()
    ensure_all_assets()
    history_store = HistoryStore()

    # 1. Auth-only mode: verify OAuth 2.0 + print Connected Channel Name & ID, then exit
    if args.auth_only:
        logger.info("Running in --auth-only mode...")
        youtube = authenticate_youtube()
        channel = verify_connected_channel(youtube)
        logger.info(
            "Authentication complete. Target channel verified: %s (%s)",
            channel.channel_title,
            channel.channel_id,
        )
        return 0

    # 2. For live runs, authenticate & verify connected YouTube channel upfront (fail fast)
    youtube = None
    if not args.dry_run:
        logger.info("Authenticating YouTube Data API v3 and verifying target channel...")
        youtube = authenticate_youtube()
        verify_connected_channel(youtube)
    else:
        logger.info("Running in --dry-run mode (YouTube upload skipped; history.json untouched).")

    count = max(1, min(4, args.count))
    past_records = history_store.get_all()
    logger.info(
        "Loaded %d historical topic(s) from history.json for zero-repeat exclusion.",
        len(past_records),
    )
    logger.info("Starting F1 Shorts run for %d Short(s)...", count)

    # 3. Discover unique, non-repeating F1 topics from distinct randomized categories
    topics = generate_unique_topics(history_store=history_store, count=count)
    logger.info("Selected %d non-repeating topic(s): %s", len(topics), [t["topic_slug"] for t in topics])

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results: list[dict[str, Any]] = []
    rendered_this_run: list[dict[str, str]] = []

    for idx, topic in enumerate(topics, start=1):
        # 4. Write & validate script + enforce final pre-render >40% similarity guard against history.json
        script_data = None
        current_topic = topic
        for regen_attempt in range(1, 4):
            slug = current_topic["topic_slug"]
            logger.info(
                "=== Short %d/%d: [%s] %s (script check %d/3) ===",
                idx,
                len(topics),
                current_topic["category_label"],
                slug,
                regen_attempt,
            )
            candidate_script = write_validated_script(current_topic)
            is_dup, dup_reason = history_store.is_duplicate(
                topic_slug=candidate_script["topic_slug"],
                title=candidate_script["title"],
                summary=candidate_script.get("angle", ""),
                extra_reserved=rendered_this_run,
            )
            if not is_dup:
                script_data = candidate_script
                break

            logger.warning(
                "Pre-render similarity guard rejected '%s' (%s); regenerating a brand-new topic (%d/3)...",
                candidate_script["title"],
                dup_reason,
                regen_attempt,
            )
            replacement = generate_unique_topics(history_store=history_store, count=1)[0]
            current_topic = replacement

        if script_data is None:
            raise RuntimeError(f"Could not produce a non-overlapping script for Short {idx} after 3 attempts.")

        slug = script_data["topic_slug"]
        rendered_this_run.append(
            {
                "category": script_data["category"],
                "topic_slug": slug,
                "title": script_data["title"],
                "angle": script_data.get("angle", ""),
            }
        )
        logger.info(
            "Validated unique script: '%s' (%d words, %d beats)",
            script_data["title"],
            script_data["word_count"],
            len(script_data["beats"]),
        )

        # 5. Render strict 9:16 1080x1920 30fps MP4 (< 60s) + custom high-click thumbnail
        base_name = f"{run_stamp}_{idx}_{slug[:42]}"
        video_path = OUTPUT_DIR / f"{base_name}.mp4"
        thumb_path = OUTPUT_DIR / f"{base_name}_thumbnail.jpg"
        meta_path = OUTPUT_DIR / f"{base_name}.json"

        render_info = build_short_video(
            script_data=script_data,
            output_mp4=video_path,
            voice=args.voice,
            rate=TTS_RATE,
            pitch=TTS_PITCH,
            thumbnail_path=thumb_path,
        )

        full_metadata = {
            **script_data,
            "render_info": render_info,
            "privacy_status": args.privacy,
            "generated_at": datetime.now().isoformat(),
        }
        meta_path.write_text(json.dumps(full_metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved rendered Short, thumbnail, and metadata sidecar to output/: %s", video_path.name)

        if args.dry_run:
            print(
                f"\n[DRY-RUN Short {idx}/{len(topics)}]\n"
                f"  Category  : {script_data['category_label']}\n"
                f"  Title     : {script_data['title']} ({len(script_data['title'])} chars)\n"
                f"  Thumb Hook: {script_data.get('thumbnail_hook', '')}\n"
                f"  Duration  : {render_info['duration_sec']}s ({render_info['width']}x{render_info['height']} @ {render_info['fps']}fps)\n"
                f"  Photos    : {render_info.get('photo_shot_count', 0)} full-bleed real F1 shots\n"
                f"  Words     : {script_data['word_count']}\n"
                f"  Video     : {video_path}\n"
                f"  Thumbnail : {thumb_path}\n"
                f"  Metadata  : {meta_path}\n",
                flush=True,
            )
            results.append(
                {
                    "status": "dry_run",
                    "video": str(video_path),
                    "thumbnail": str(thumb_path),
                    "title": script_data["title"],
                }
            )
            continue

        # 6. Live upload via resumable videos().insert + custom thumbnail (stops immediately on HTTP 403 quotaExceeded)
        try:
            video_id = upload_short(
                youtube,
                video_path=video_path,
                metadata=full_metadata,
                history_store=history_store,
                privacy_status=args.privacy,
                metadata_json_path=meta_path,
                thumbnail_path=thumb_path,
            )
            results.append({"status": "uploaded", "video_id": video_id, "title": script_data["title"]})
        except QuotaExceededError as exc:
            logger.error("Stopping run immediately due to YouTube API quota limit: %s", exc)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    logger.info("Pipeline run finished cleanly: %s", json.dumps(results))
    return 0


def main() -> None:
    log_file = setup_logging()
    args = parse_args()
    try:
        exit_code = run_pipeline(args)
    except KeyboardInterrupt:
        logging.getLogger("main").warning("Interrupted by user.")
        exit_code = 130
    except Exception as exc:
        logging.getLogger("main").exception("Pipeline failed: %s (see %s)", exc, log_file)
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
