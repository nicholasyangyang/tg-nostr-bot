"""Gateway configuration from .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Resolve ALL_KEY_PATH relative to this config file so it works
# regardless of the working directory from which gateway is started.
_default_key_path = Path(__file__).resolve().parent / "all_key.json"

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
ALL_KEY_PATH: str = os.getenv("ALL_KEY_PATH", str(_default_key_path))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
