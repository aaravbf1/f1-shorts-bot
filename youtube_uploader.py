"""YouTube Data API v3 OAuth 2.0 authentication, channel verification, and Shorts uploader."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import (
    CLIENT_SECRET_FILE,
    TOKEN_FILE,
    UPLOADED_DIR,
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_HTTP_TIMEOUT_SEC,
    YOUTUBE_PRIVACY_STATUS,
    YOUTUBE_SCOPES,
)
from history_store import HistoryStore

logger = logging.getLogger(__name__)


class QuotaExceededError(RuntimeError):
    """Raised immediately when YouTube Data API v3 returns HTTP 403 quotaExceeded."""


@dataclass
class ConnectedChannel:
    channel_id: str
    channel_title: str


def materialize_credentials_from_env(
    client_secret_path: Path = CLIENT_SECRET_FILE,
    token_path: Path = TOKEN_FILE,
) -> None:
    """Write temporary client_secret.json and token.json from env vars if absent on disk."""
    client_secret_env = os.getenv("CLIENT_SECRET_JSON", "").strip()
    if client_secret_env and not client_secret_path.exists():
        # Validate JSON before writing to disk
        json.loads(client_secret_env)
        client_secret_path.write_text(client_secret_env, encoding="utf-8")
        logger.info("Materialized %s from CLIENT_SECRET_JSON environment variable.", client_secret_path.name)

    token_env = os.getenv("TOKEN_JSON", "").strip()
    if token_env and not token_path.exists():
        json.loads(token_env)
        token_path.write_text(token_env, encoding="utf-8")
        logger.info("Materialized %s from TOKEN_JSON environment variable.", token_path.name)


def _is_headless_environment() -> bool:
    """Return True when running in GitHub Actions, CI, or a non-interactive headless shell."""
    return (
        os.getenv("GITHUB_ACTIONS", "").lower() == "true"
        or os.getenv("CI", "").lower() == "true"
        or os.getenv("HEADLESS", "").lower() in {"1", "true", "yes"}
        or not sys.stdin.isatty()
    )


def authenticate_youtube(
    client_secret_path: Path = CLIENT_SECRET_FILE,
    token_path: Path = TOKEN_FILE,
):
    """Authenticate via OAuth 2.0 and return an authorized YouTube Data API v3 service client."""
    socket.setdefaulttimeout(YOUTUBE_HTTP_TIMEOUT_SEC)
    materialize_credentials_from_env(client_secret_path=client_secret_path, token_path=token_path)
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired YouTube OAuth token...")
            creds.refresh(Request())
        else:
            if _is_headless_environment():
                raise RuntimeError(
                    "Refusing to launch interactive OAuth run_local_server() in headless/cloud mode. "
                    "Ensure the TOKEN_JSON secret contains a valid token.json with a refresh_token."
                )
            if not client_secret_path.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client secrets file: {client_secret_path}\n"
                    "Download your Desktop App OAuth 2.0 credentials JSON from Google Cloud Console "
                    "(with YouTube Data API v3 enabled) and save it as client_secret.json."
                )
            logger.info("Starting OAuth 2.0 browser flow using %s...", client_secret_path.name)
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent")

        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Saved YouTube OAuth token to %s", token_path.name)

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def verify_connected_channel(youtube) -> ConnectedChannel:
    """Query channels().list(part='snippet', mine=True) and print/log exact target channel."""
    try:
        response = youtube.channels().list(part="snippet", mine=True).execute(num_retries=2)
    except HttpError as err:
        _check_quota_error(err)
        raise

    items = response.get("items", [])
    if not items:
        raise RuntimeError(
            "Authenticated Google account has no associated YouTube channel. "
            "Create or select a YouTube channel and re-run with --auth-only."
        )

    channel = items[0]
    channel_id = channel["id"]
    channel_title = channel.get("snippet", {}).get("title", "<Unknown Channel>")

    banner = f"Connected YouTube Channel: {channel_title} (Channel ID: {channel_id})"
    print("=" * len(banner), flush=True)
    print(banner, flush=True)
    print("=" * len(banner), flush=True)
    logger.info(banner)
    return ConnectedChannel(channel_id=channel_id, channel_title=channel_title)


def _check_quota_error(err: HttpError) -> None:
    """Inspect HttpError and raise QuotaExceededError immediately on HTTP 403 quotaExceeded."""
    status = getattr(err.resp, "status", None)
    body = ""
    try:
        body = err.content.decode("utf-8", errors="ignore") if err.content else str(err)
    except Exception:
        body = str(err)

    if status == 403 and ("quotaExceeded" in body or "dailyLimitExceeded" in body):
        logger.error("HTTP 403 quotaExceeded from YouTube Data API v3! Stopping immediately.")
        raise QuotaExceededError(
            "YouTube Data API v3 returned HTTP 403 quotaExceeded. "
            "Upload halted immediately to prevent failed retries."
        ) from err


def upload_short(
    youtube,
    *,
    video_path: Path,
    metadata: dict[str, Any],
    history_store: HistoryStore,
    privacy_status: str = YOUTUBE_PRIVACY_STATUS,
    metadata_json_path: Path | None = None,
) -> str:
    """Upload a single 9:16 Short via resumable videos().insert, record to history, and archive.

    NOTE: Explicitly does NOT call thumbnails().set because YouTube Data API v3
    rejects custom thumbnails on Shorts uploads.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"Rendered Short not found: {video_path}")

    title = str(metadata["title"]).strip()
    description = str(metadata["description"]).strip()
    tags = [str(t).strip() for t in metadata.get("tags", []) if str(t).strip()][:15]

    request_body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,  # "17" = Sports
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=4 * 1024 * 1024,  # 4 MB resumable chunks
        resumable=True,
    )

    logger.info(
        "Starting resumable Shorts upload: '%s' (%s, privacy=%s, categoryId=%s)",
        title,
        video_path.name,
        privacy_status,
        YOUTUBE_CATEGORY_ID,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media,
    )

    response = None
    try:
        while response is None:
            status, response = request.next_chunk(num_retries=2)
            if status:
                logger.info("Upload progress for %s: %d%%", video_path.name, int(status.progress() * 100))
    except HttpError as err:
        _check_quota_error(err)
        raise

    video_id = response["id"]
    logger.info("Upload succeeded! Video ID: %s | URL: https://youtube.com/shorts/%s", video_id, video_id)
    print(f"Uploaded Short: {title} -> https://youtube.com/shorts/{video_id} (privacy={privacy_status})", flush=True)

    # Record permanently in history.json
    history_store.record_upload(
        topic_slug=metadata["topic_slug"],
        category=metadata["category"],
        title=title,
        video_id=video_id,
        angle=metadata.get("angle", ""),
    )

    # Archive rendered video + metadata JSON into uploaded/
    UPLOADED_DIR.mkdir(parents=True, exist_ok=True)
    archived_video = UPLOADED_DIR / f"{video_id}_{video_path.name}"
    shutil.move(str(video_path), str(archived_video))
    logger.info("Archived video to %s", archived_video)

    if metadata_json_path and metadata_json_path.exists():
        meta_copy = json.loads(metadata_json_path.read_text(encoding="utf-8"))
        meta_copy["video_id"] = video_id
        meta_copy["youtube_url"] = f"https://youtube.com/shorts/{video_id}"
        archived_meta = UPLOADED_DIR / f"{video_id}_{metadata_json_path.name}"
        archived_meta.write_text(json.dumps(meta_copy, indent=2, ensure_ascii=False), encoding="utf-8")
        metadata_json_path.unlink(missing_ok=True)
        logger.info("Archived metadata to %s", archived_meta)

    return video_id
