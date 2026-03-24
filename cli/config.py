"""CLI configuration from .env."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
ALLOWED_USERS: set[int] = {
    int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
}
PORT: int = int(os.getenv("PORT", "8000"))
GATEWAY_WS_URL: str = os.getenv("GATEWAY_WS_URL", "ws://127.0.0.1:7899")
MSG_TO: str = os.getenv("MSG_TO", "")   # npub bech32, default destination
KEY_PATH: str = os.getenv("KEY_PATH", "./key.json")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
