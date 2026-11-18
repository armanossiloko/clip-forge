"""Paths and user settings for ClipForge."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
FONTS_DIR = ROOT / "fonts"                      # bundled caption fonts (Poppins Bold)
IN_DOCKER = os.environ.get("CLIPFORGE_DOCKER") == "1"
# Folder for local video files. In Docker this is the ./videos folder mounted at /videos.
VIDEOS_DIR = Path(os.environ.get("CLIPFORGE_VIDEOS_DIR") or ROOT / "videos")

JOBS_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS: dict = {
    # --- transcription ---
    "whisper_model": "small",        # tiny | base | small | medium | large-v3 | turbo
    "whisper_device": "auto",        # auto | cpu | cuda
    "language": "",                  # "" = auto-detect, or e.g. "en", "bs", "de"
    # --- clip selection (Claude) ---
    "num_clips": 5,
    "min_seconds": 20,
    "max_seconds": 60,
    "claude_model": "",              # "" = your Claude Code default, or "sonnet" / "opus" / "haiku"
    "claude_timeout": 600,
    "extra_instructions": "",        # e.g. "My channel is about personal finance, prefer actionable tips"
    # --- rendering ---
    "crop_mode": "fit",              # fit (whole frame, never zoomed; the default) | smart | track | center
    "captions": True,
    "caption_words": 3,              # words per caption chunk
    "caption_uppercase": True,
    "caption_font": os.environ.get("CLIPFORGE_CAPTION_FONT", "Poppins"),
    "caption_size": 92,
    "caption_color": "#FFFFFF",
    "caption_highlight": "#FFD400",
    "caption_position": 0.70,        # 0 = top, 1 = bottom (vertical position of captions)
    "video_crf": 20,
    "max_download_height": 1080,
}

_lock = threading.Lock()


def load_settings() -> dict:
    with _lock:
        settings = dict(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        return settings


def save_settings(new: dict) -> dict:
    current = load_settings()
    for key, value in new.items():
        if key in DEFAULT_SETTINGS:
            default = DEFAULT_SETTINGS[key]
            try:
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, int):
                    value = int(value)
                elif isinstance(default, float):
                    value = float(value)
                else:
                    value = str(value)
            except (TypeError, ValueError):
                continue
            current[key] = value
    with _lock:
        SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current
