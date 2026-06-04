import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
DATABASE_URL = "sqlite:///tracker.db"

AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "changeme")

DAILY_DIGEST_HOUR = int(os.getenv("DAILY_DIGEST_HOUR", 20))
WEEKLY_DIGEST_DAY = os.getenv("WEEKLY_DIGEST_DAY", "mon")
WEEKLY_DIGEST_HOUR = int(os.getenv("WEEKLY_DIGEST_HOUR", 9))
MEETING_REMINDER_MINUTES = int(os.getenv("MEETING_REMINDER_MINUTES", 30))
