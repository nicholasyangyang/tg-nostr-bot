"""Gateway configuration from .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GATEWAY_HOST: str = os.getenv("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT: int = int(os.getenv("GATEWAY_PORT", "7899"))
NOSTR_RELAYS: list[str] = [
    r.strip()
    for r in os.getenv("NOSTR_RELAYS", "").split(",")
    if r.strip()
] or [
    "wss://relay.damus.io",
    "wss://relay.0xchat.com",
    "wss://nostr.oxtr.dev",
    "wss://relay.primal.net",
]
# Filename only — will be resolved relative to cwd_dir at runtime
ALL_KEY_PATH: str = os.getenv("ALL_KEY_PATH", "all_key.json")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
