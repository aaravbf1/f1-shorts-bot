# F1 Shorts Engine (`f1_shorts_engine`)

Autonomous Python pipeline that generates, renders, and uploads **2 non-repeating, human-sounding Formula 1 YouTube Shorts per day** (`1080x1920`, 40–58 seconds, seamless loop) using the **Google Antigravity SDK (`gemini-4-hydrogen` + Google Search grounding)** and the **YouTube Data API v3**.

## Features

- **Anti-Repetition Ledger (`history.json`):** Permanently records every uploaded Short (`topic_slug`, `category`, `title`, `video_id`, `upload_date`) and rejects exact or near-duplicate topics before scriptwriting.
- **Randomized 4-Category F1 Engine (`topic_engine.py`):** Uses `gemini-4-hydrogen` with live Google Search grounding to pick 2 distinct categories per run:
  1. Trending Paddock Drama & News
  2. Crazy F1 History & Untold Stories
  3. "Did You Know?" F1 Facts & Rules
  4. Bold "What If?" & GOAT Debates
- **Human Storyteller & Seamless Loop (`script_writer.py`):** Enforces conversational storytelling, blocks banned AI clichés (`delve`, `tapestry`, `testament`, `buckle up`, etc.), locks spoken length to **110–145 words**, structures 3–4s visual beats, creates a seamless loop ending + comment-bait question, and keeps titles strictly under **55 characters** with `#f1`.
- **Mobile-First 9:16 Video Builder (`video_builder.py`):**
  - `edge-tts` (`en-GB-RyanNeural` or `en-US-AndrewNeural`) at `+6%` rate + FFmpeg `silenceremove`
  - Strict YouTube Shorts UI safe zones (bottom 20% and right-side icon column kept clear)
  - Rotating story cards, team radio quote callouts, huge single-number stats, Driver vs. Driver cards, and center-screen word-by-word highlighted subtitles over animated dark-mode F1 backgrounds
  - Background music ducked to `-22 dB` + crisp whoosh/pop transition SFX
  - Strict `< 60s` export verification
- **Verified YouTube Shorts Uploader (`youtube_uploader.py`):**
  - OAuth 2.0 (`youtube.upload` + `youtube.readonly`) with upfront `channels().list(part="snippet", mine=True)` verification printing exact **Connected Channel Name & Channel ID**
  - Resumable chunked `videos().insert` (`categoryId="17"` Sports, no `thumbnails().set`, default `privacyStatus="private"` configurable in `.env`)
  - Immediate halt on HTTP `403 quotaExceeded`
  - Archives uploaded videos & metadata into `uploaded/`

## Quick Start

### 1. Configure `.env`
Review `.env` (copied from `.env.example`):
- Set `YOUTUBE_PRIVACY_STATUS=private` (change to `public` when ready to auto-publish).
- Choose `TTS_VOICE=en-GB-RyanNeural` or `en-US-AndrewNeural`.
- Set `GEMINI_API_KEY` if you are not using local Antigravity account authentication.

### 2. Authenticate YouTube & Verify Channel (`--auth-only`)
1. Download your Desktop App OAuth 2.0 JSON from Google Cloud Console (with **YouTube Data API v3** enabled) and save it as `client_secret.json` in this folder.
2. Run:
   ```powershell
   .\.venv\Scripts\python.exe main.py --auth-only
   ```
3. Complete the browser sign-in and verify that the printed **Connected Channel Name & Channel ID** match your intended F1 channel.

### 3. Test Without Uploading (`--dry-run`)
```powershell
.\.venv\Scripts\python.exe main.py --dry-run
```
Inspect the rendered `1080x1920` `.mp4` files and `.json` metadata inside `output/`.

### 4. Run Live Upload (2 Shorts)
```powershell
.\.venv\Scripts\python.exe main.py
```

### 5. Twice-Daily Schedule (11:00 AM & 7:00 PM)
The Windows Scheduled Task `F1ShortsEngine_TwiceDaily` runs automatically at **11:00 AM** and **7:00 PM** with **Run task as soon as possible after a scheduled start is missed** (`StartWhenAvailable`) enabled. To re-register or inspect it:
```powershell
powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
```
