"""Central configuration, constants, safe zones, and team palettes for F1 Shorts Engine."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Directories
OUTPUT_DIR = BASE_DIR / "output"
UPLOADED_DIR = BASE_DIR / "uploaded"
LOGS_DIR = BASE_DIR / "logs"
ASSETS_DIR = BASE_DIR / "assets"
BACKGROUNDS_DIR = ASSETS_DIR / "backgrounds"
FONTS_DIR = ASSETS_DIR / "fonts"
AUDIO_DIR = ASSETS_DIR / "audio"

HISTORY_FILE = BASE_DIR / "history.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / "token.json"

# Antigravity SDK / Model configuration
ANTIGRAVITY_MODEL = os.getenv("ANTIGRAVITY_MODEL", "gemini-4-hydrogen").strip() or "gemini-4-hydrogen"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
VERTEX_ENABLED = os.getenv("VERTEX_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "").strip()
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1").strip()

# TTS configuration
TTS_VOICE = os.getenv("TTS_VOICE", "en-GB-RyanNeural").strip()
TTS_RATE = os.getenv("TTS_RATE", "+6%").strip()
AVAILABLE_VOICES = ("en-GB-RyanNeural", "en-US-AndrewNeural")

# YouTube Data API v3 configuration
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
YOUTUBE_CATEGORY_ID = "17"  # Sports
YOUTUBE_PRIVACY_STATUS = (
    os.getenv("PRIVACY_STATUS") or os.getenv("YOUTUBE_PRIVACY_STATUS") or "public"
).strip().lower()
if YOUTUBE_PRIVACY_STATUS not in {"private", "unlisted", "public"}:
    YOUTUBE_PRIVACY_STATUS = "public"

SHORTS_PER_RUN = max(1, int(os.getenv("SHORTS_PER_RUN", "2")))

# Video geometry & YouTube Shorts UI Safe Zones (strict 9:16)
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30

# Keep key graphics/text strictly inside these safe bounds:
# - Top 180px clear of Shorts top navigation/camera bar
# - Bottom 20% (384px -> y >= 1536) clear of channel handle, title & audio ticker
# - Right 16% (172px -> x >= 908) clear of Like/Dislike/Comment/Share/Remix column
SAFE_LEFT = 56
SAFE_RIGHT = 896
SAFE_TOP = 185
SAFE_BOTTOM = 1510
SAFE_WIDTH = SAFE_RIGHT - SAFE_LEFT

# Duration guardrails (strictly < 60s for YouTube Shorts feed indexing)
MIN_DURATION_SEC = 40.0
TARGET_MIN_DURATION_SEC = 42.0
TARGET_MAX_DURATION_SEC = 55.0
MAX_HARD_DURATION_SEC = 58.8

# Script guardrails
MIN_WORD_COUNT = 110
MAX_WORD_COUNT = 145
MAX_TITLE_LENGTH = 54  # strictly under 55 chars

BANNED_AI_PHRASES = [
    "delve",
    "tapestry",
    "testament",
    "buckle up",
    "in the fast-paced world of formula 1",
    "in the fast-paced world of f1",
    "let's dive in",
    "lets dive in",
    "dive in",
    "moreover",
    "in conclusion",
    "hey guys",
    "welcome back",
    "what's up guys",
    "in this video",
    "game-changer",
    "game changer",
    "it's worth noting",
    "needless to say",
]

# Category pool for randomized daily selection
F1_CATEGORIES = [
    {
        "id": "trending_paddock_drama",
        "label": "Trending Paddock Drama & News",
        "description": (
            "Latest F1 headlines, driver contract rumors, fiery team radio, "
            "steward penalties, team boss clashes, or recent race weekend drama."
        ),
        "requires_live_news": True,
    },
    {
        "id": "crazy_f1_history",
        "label": "Crazy F1 History & Untold Stories",
        "description": (
            "Legendary rivalries, infamous crashes, banned technical loopholes, "
            "shocking underdog wins, and wild paddock scandals."
        ),
        "requires_live_news": False,
    },
    {
        "id": "did_you_know_facts",
        "label": '"Did You Know?" F1 Facts & Rules',
        "description": (
            "Surprising facts everyday fans love: extreme G-forces drivers survive, "
            "steering wheel costs, insane pit stop records, cockpit heat, or weird FIA rules."
        ),
        "requires_live_news": False,
    },
    {
        "id": "what_if_goat_debates",
        "label": 'Bold "What If?" & GOAT Debates',
        "description": (
            "Spicy cross-era comparisons, luckiest championship wins, "
            "biggest team strategy blunders, or career-changing sliding-doors moments."
        ),
        "requires_live_news": False,
    },
]

# F1 Team Accent Color Palettes (RGB)
TEAM_PALETTES: dict[str, tuple[int, int, int]] = {
    "f1_red": (225, 6, 0),
    "ferrari": (232, 0, 45),
    "red_bull": (54, 113, 198),
    "mercedes": (39, 244, 210),
    "mclaren": (255, 128, 0),
    "aston_martin": (34, 153, 113),
    "alpine": (0, 147, 204),
    "williams": (100, 196, 255),
    "haas": (182, 186, 189),
    "sauber": (82, 226, 82),
    "rb": (102, 146, 255),
    "gold": (255, 200, 45),
}


def ensure_directories() -> None:
    """Ensure all workspace runtime and asset directories exist."""
    for directory in (
        OUTPUT_DIR,
        UPLOADED_DIR,
        LOGS_DIR,
        ASSETS_DIR,
        BACKGROUNDS_DIR,
        FONTS_DIR,
        AUDIO_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
