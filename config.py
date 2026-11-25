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
PHOTOS_DIR = ASSETS_DIR / "photos"

HISTORY_FILE = BASE_DIR / "history.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / "token.json"

# Antigravity SDK / Direct Gemini API configuration
ANTIGRAVITY_MODEL = os.getenv("ANTIGRAVITY_MODEL", "gemini-4-hydrogen").strip() or "gemini-4-hydrogen"
ANTIGRAVITY_FALLBACK_MODELS = tuple(
    m.strip()
    for m in os.getenv(
        "ANTIGRAVITY_FALLBACK_MODELS",
        "gemini-3.6-flash,gemini-3-flash-preview,gemini-flash-lite-latest,gemini-flash-latest,gemini-3.5-flash,gemini-3.5-flash-lite",
    ).split(",")
    if m.strip()
)
USE_DIRECT_GEMINI_API = (
    os.getenv("USE_DIRECT_GEMINI_API", "").strip().lower() in {"1", "true", "yes"}
    or os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    or os.getenv("CI", "").strip().lower() == "true"
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
VERTEX_ENABLED = os.getenv("VERTEX_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "").strip()
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1").strip()

# Strict runtime timeouts (prevent any hang in headless CI / GitHub Actions)
LLM_CALL_TIMEOUT_SEC = float(os.getenv("LLM_CALL_TIMEOUT_SEC", "30"))
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC", "15"))
TTS_TIMEOUT_SEC = float(os.getenv("TTS_TIMEOUT_SEC", "45"))
FFMPEG_TIMEOUT_SEC = int(os.getenv("FFMPEG_TIMEOUT_SEC", "120"))
YOUTUBE_HTTP_TIMEOUT_SEC = int(os.getenv("YOUTUBE_HTTP_TIMEOUT_SEC", "60"))

# TTS configuration (Shorts v3: warm conversational multilingual neural narrator + 0.25s breath pauses)
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AndrewMultilingualNeural").strip()
TTS_RATE = os.getenv("TTS_RATE", "-6%").strip()
TTS_PITCH = os.getenv("TTS_PITCH", "+0Hz").strip()
INTER_BEAT_GAP_SEC = float(os.getenv("INTER_BEAT_GAP_SEC", "0.25"))
THUMBNAIL_FLASH_SEC = float(os.getenv("THUMBNAIL_FLASH_SEC", "0.15"))
PHOTO_SWAP_INTERVAL_SEC = float(os.getenv("PHOTO_SWAP_INTERVAL_SEC", "3.0"))
AVAILABLE_VOICES = ("en-US-AndrewMultilingualNeural", "en-US-BrianMultilingualNeural")

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

# Script guardrails (calibrated for -10% TTS rate + 0.35s pauses across 8-10 1-sentence beats -> ~44-54s)
MIN_WORD_COUNT = 95
MAX_WORD_COUNT = 128
MAX_TITLE_LENGTH = 59  # strictly under 60 chars, ending with #shorts

BANNED_JARGON_TERMS = [
    "cfd",
    "venturi tunnel",
    "venturi tunnels",
    "bargeboard",
    "bargeboards",
    "porpoising",
    "telemetry",
    "ground effect vortex",
    "brake migration",
    "differential map",
    "thermal degradation",
    "aerodynamic efficiency",
    "aero efficiency",
    "floor edge vortex",
    "beam wing",
    "diffuser stall",
]

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

# Anti-repetition threshold (reject and regenerate if topic/title overlaps > 40% with history.json)
TOPIC_OVERLAP_THRESHOLD = 0.40

# Diverse 7-category F1 Shorts pool for strong randomization so consecutive Shorts never feel similar
F1_CATEGORIES = [
    {
        "id": "wild_f1_rules_loopholes",
        "label": "Wild F1 Rules & Loopholes",
        "description": (
            "Clever rulebook loopholes, six-wheeled cars, banned inventions, bizarre penalties, "
            "or sneaky tricks teams pulled off before the FIA shut them down."
        ),
        "requires_live_news": False,
    },
    {
        "id": "pit_stop_strategy_gambles",
        "label": "Crazy Pit-Stop & Strategy Gambles",
        "description": (
            "Sub-2-second pit stop records, disastrous pit-lane blunders, genius tire calls, "
            "last-lap gambles, or legendary team radio strategy arguments."
        ),
        "requires_live_news": False,
    },
    {
        "id": "historic_rivalries_controversies",
        "label": "Historic Rivalries & Controversies",
        "description": (
            "Explosive teammate feuds, title-deciding collisions, mind games, paddock betrayals, "
            "team-order rebellions, and unforgettable championship showdowns."
        ),
        "requires_live_news": False,
    },
    {
        "id": "driver_fitness_gforce_extremes",
        "label": "Driver Fitness & G-Force Extremes",
        "description": (
            "What an F1 driver's body actually endures: 5G braking, neck-snapping corners, "
            "50C cockpit heat, losing 3-4 kg of sweat in one race, and superhuman reaction times."
        ),
        "requires_live_news": False,
    },
    {
        "id": "bizarre_f1_records_crashes",
        "label": "Bizarre F1 Records & Wild Crashes",
        "description": (
            "Unbelievable F1 records, shock underdog wins, freak mechanical moments, "
            "impossible comebacks from last to first, and jaw-dropping survival stories."
        ),
        "requires_live_news": False,
    },
    {
        "id": "current_2026_season_drama",
        "label": "2026 F1 Season Drama & Headlines",
        "description": (
            "Fresh 2026 Formula 1 paddock drama, driver clashes, spicy team radio, "
            "contract tension, steward penalties, and this week's biggest F1 talking points."
        ),
        "requires_live_news": True,
    },
    {
        "id": "iconic_tracks_weather_chaos",
        "label": "Iconic Tracks & Weather Chaos",
        "description": (
            "Chaotic rain-soaked classics, Monaco street-circuit madness, Monza top-speed extremes, "
            "Spa unpredictability, and races flipped upside down by sudden weather."
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
        PHOTOS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
