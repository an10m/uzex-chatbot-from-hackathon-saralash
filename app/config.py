"""Environment configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
try:
    load_dotenv(BASE_DIR / ".env")
except OSError:
    # A missing or unreadable .env is reported later by the entry point with a
    # clear message. Offline tools must still work without secrets.
    pass

DB_PATH = Path(os.getenv("UZEX_DB_PATH", str(BASE_DIR / "data" / "uzex.sqlite")))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = Path(os.getenv("LOG_FILE", str(BASE_DIR / "logs" / "bot.log")))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
