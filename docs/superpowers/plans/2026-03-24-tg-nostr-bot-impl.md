# tg-nostr-bot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Telegram-Nostr bridge. Each CLI bridges a Telegram Bot to Nostr via a shared Gateway. Multiple CLI instances each have an independent npub, routed by to_npub.

**Architecture:** Gateway = WebSocket server (accepts CLI connections) + Nostr relay pool (connects to public relays, subscribes kind:1059) + **ALL crypto** (owns all keys, does NIP-17 wrap/unwrap). CLI = FastAPI webhook (receives TG updates) + WebSocket client (raw text passthrough, no crypto). Both share key_manager (NIP-44/NIP-17). Gateway manages all_key.json; CLI saves local key.json.

**Tech Stack:** Python 3.12, asyncio, websockets, aiohttp, fastapi, uvicorn, httpx, secp256k1, bech32, cryptography

---

## File Map

| File | Responsibility | Reuse |
|------|----------------|-------|
| `gateway/requirements.txt` | Gateway pip deps | — |
| `cli/requirements.txt` | CLI pip deps | — |
| `shared/key_manager.py` | NIP-44/NIP-17/keys | Copy from py_gateway/key_manager.py |
| `shared/relay_client.py` | Nostr relay pool | Copy from py_gateway/relay_client.py |
| `gateway/config.py` | .env loader for gateway | — |
| `gateway/websocket_server.py` | WS server, CLI reg, routing | Adapt py_gateway |
| `gateway/key_manager.py` | Symlink to shared/ | — |
| `gateway/relay_client.py` | Symlink to shared/ | — |
| `gateway/main.py` | Entry point | — |
| `gateway/.env.example` | Config template | — |
| `cli/config.py` | .env loader for cli | — |
| `cli/ws_client.py` | WS client, key reg, raw text passthrough | — |
| `cli/app.py` | FastAPI Webhook | Adapt from tg_bot |
| `cli/main.py` | Entry point | — |
| `cli/.env.example` | Config template | — |

---

## Task 1: Project Scaffolding & Shared Modules

**Files:**
- Create: `tg-nostr-bot/gateway/requirements.txt`
- Create: `tg-nostr-bot/cli/requirements.txt`
- Create: `tg-nostr-bot/shared/`
- Create: `tg-nostr-bot/shared/__init__.py`
- Create: `tg-nostr-bot/shared/key_manager.py`
- Create: `tg-nostr-bot/shared/relay_client.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p tg-nostr-bot/gateway tg-nostr-bot/cli tg-nostr-bot/shared
touch tg-nostr-bot/gateway/__init__.py tg-nostr-bot/cli/__init__.py
```

- [ ] **Step 2: Create gateway/requirements.txt**

```
aiohttp
websockets
python-dotenv
secp256k1
bech32
cryptography
```

- [ ] **Step 3: Create cli/requirements.txt**

```
aiohttp
websockets
fastapi
uvicorn
httpx
python-dotenv
pydantic
secp256k1
bech32
cryptography
```

- [ ] **Step 4: Copy shared/key_manager.py**

Copy the entire contents of `/home/deeptuuk/Code/Peer_new/py_gateway/gateway/key_manager.py` into `tg-nostr-bot/shared/key_manager.py`. This file contains: NIP-44 (XChaCha20-Poly1305), NIP-17 Gift Wrap (nip17_wrap_message, nip17_unwrap), key generation, bech32 helpers, signing.

- [ ] **Step 5: Copy shared/relay_client.py**

Copy the entire contents of `/home/deeptuuk/Code/Peer_new/py_gateway/gateway/relay_client.py` into `tg-nostr-bot/shared/relay_client.py`. This file contains: RelayConnection, RelayClient (connect, subscribe, publish, listen, parse_dm, _publish_dm_async). Update import from `gateway.key_manager` to `shared.key_manager`.

**Important:** In relay_client.py, update the import:
```python
# Change from:
from gateway.key_manager import npub_to_hex, nsec_to_hex, sign_event, ...
# Change to:
from shared.key_manager import npub_to_hex, nsec_to_hex, sign_event, ...
```

Also update the `parse_dm` method's import and `_publish_dm_async` imports similarly.

- [ ] **Step 6: Create shared/__init__.py**

```python
"""Shared modules for tg-nostr-bot."""
```

- [ ] **Step 7: Install dependencies for both**

```bash
# Gateway env
python -m venv gateway_env
source gateway_env/bin/activate
pip install -r gateway/requirements.txt

# CLI env
python -m venv cli_env
source cli_env/bin/activate
pip install -r cli/requirements.txt
```

- [ ] **Step 8: Commit**

```bash
git add gateway/ cli/ shared/ -A
git commit -m "$(cat <<'EOF'
feat: scaffold project + shared modules

- gateway/requirements.txt: aiohttp, websockets, python-dotenv, secp256k1, bech32, cryptography
- cli/requirements.txt: above + fastapi, uvicorn, httpx, pydantic
- shared/key_manager.py: copied from py_gateway (NIP-44, NIP-17, keys)
- shared/relay_client.py: copied from py_gateway (relay pool)
EOF
)"
```

---

## Task 2: Gateway Core

**Files:**
- Create: `tg-nostr-bot/gateway/config.py`
- Create: `tg-nostr-bot/gateway/key_manager.py` (symlink → shared/)
- Create: `tg-nostr-bot/gateway/relay_client.py` (symlink → shared/)
- Create: `tg-nostr-bot/gateway/websocket_server.py`
- Create: `tg-nostr-bot/gateway/main.py`
- Create: `tg-nostr-bot/gateway/.env.example`
- Modify: `tg-nostr-bot/gateway/__init__.py`

- [ ] **Step 1: Create gateway/config.py**

```python
"""Gateway configuration from .env."""
import os
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
ALL_KEY_PATH: str = os.getenv("ALL_KEY_PATH", "./all_key.json")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
```

- [ ] **Step 2: Create gateway/key_manager.py and gateway/relay_client.py symlinks**

Since `shared/` is at the project root, create import helpers:

```python
# gateway/key_manager.py
"""Re-export shared key_manager."""
import sys
from pathlib import Path
_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))
from shared.key_manager import (
    generate_keys, get_keys, get_public_key, get_private_key,
    npub_to_hex, nsec_to_hex, hex_to_npub,
    nip44_encrypt, nip44_decrypt,
    nip17_wrap_message, nip17_unwrap,
    KIND_NIP17_GIFT_WRAP, KIND_NIP17_SEAL, KIND_NIP17_TEXT_MSG,
    sign_event,
)
```

```python
# gateway/relay_client.py
"""Re-export shared relay_client."""
import sys
from pathlib import Path
_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))
from shared.relay_client import RelayClient, NostrEvent
```

- [ ] **Step 3: Create gateway/websocket_server.py**

This is the core component. Adapt from `py_gateway/gateway/websocket_server.py` but:
- Manage `all_key.json` (load on init, save on key registration)
- Only support `register` (npub) and `register_request` (key gen + return nsec) messages
- No `send_dm` from CLI — gateway publishes to relay itself
- Route incoming DMs from relay to the right CLI by to_npub

```python
"""Gateway WebSocket server: CLI registration + DM routing."""
import asyncio
import json
import logging
import time
import websockets
from dataclasses import dataclass
from pathlib import Path

from gateway.config import GATEWAY_HOST, GATEWAY_PORT, NOSTR_RELAYS, ALL_KEY_PATH
from gateway.key_manager import (
    generate_keys, npub_to_hex, hex_to_npub,
    nip17_wrap_message,
    KIND_NIP17_GIFT_WRAP,
)
from shared.relay_client import RelayClient

logger = logging.getLogger("gateway")

# ── all_key.json management ────────────────────────────────────────────────────

def load_all_keys() -> dict:
    path = Path(ALL_KEY_PATH)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_all_keys(keys: dict):
    with open(ALL_KEY_PATH, "w") as f:
        json.dump(keys, f, indent=2)

def get_or_create_key(npub: str = None) -> dict:
    """Return existing key by npub, or generate new if npub is None."""
    all_keys = load_all_keys()
    if npub:
        for k in all_keys.values():
            if k.get("npub") == npub:
                return k
        return None
    # Generate new
    new = generate_keys()
    all_keys[new["npub"]] = new
    save_all_keys(all_keys)
    return new

# ── WebSocket client state ────────────────────────────────────────────────────

@dataclass
class WSClient:
    id: str
    websocket: object
    npub_hex: str = ""   # x-only hex
    npub_bech32: str = "" # bech32 npub1...

class GatewayHandler:
    def __init__(self):
        self._clients: dict[str, WSClient] = {}   # id → WSClient
        self._npub_to_client: dict[str, str] = {}  # npub_hex → client_id
        self._relay: RelayClient = None
        self._loop: asyncio.AbstractEventLoop = None
        self._all_keys: dict = load_all_keys()

    def set_relay(self, relay: RelayClient, loop: asyncio.AbstractEventLoop):
        self._relay = relay
        self._loop = loop
        # Wire up relay → handler callback
        relay._on_event = self._on_relay_event

    def _on_relay_event(self, event: dict):
        """Handle incoming kind:1059 event from relay."""
        try:
            # Extract to_npub from #p tags
            tags = event.get("tags", [])
            to_npub_hex = None
            for tag in tags:
                if len(tag) >= 2 and tag[0] == "p":
                    to_npub_hex = tag[1]
                    break
            if not to_npub_hex:
                logger.warning("[Gateway] Relay event has no #p tag, dropping")
                return

            # Look up seckey from all_key.json
            seckey = None
            sender_pubkey = None
            for k in self._all_keys.values():
                if npub_to_hex(k["npub"]) == to_npub_hex:
                    seckey = npub_to_hex(k["nsec"])
                    sender_pubkey = npub_to_hex(k["npub"])
                    break

            if not seckey:
                logger.warning(f"[Gateway] No seckey for npub_hex={to_npub_hex[:16]}..., dropping")
                return

            # Import at module level above (nip17_unwrap)
            rumor = nip17_unwrap(event, seckey, sender_pubkey)
            if not rumor:
                logger.warning("[Gateway] Failed to unwrap NIP-17 DM")
                return

            from_npub_hex = rumor.get("pubkey", "")
            from_npub = hex_to_npub(from_npub_hex) if from_npub_hex else ""
            content = rumor.get("content", "")
            created_at = rumor.get("created_at", 0)

            # Route to CLI by to_npub_hex
            client_id = self._npub_to_client.get(to_npub_hex)
            if not client_id or client_id not in self._clients:
                logger.warning(f"[Gateway] No client for {to_npub_hex[:16]}..., dropping")
                return

            client = self._clients[client_id]
            to_npub = hex_to_npub(to_npub_hex)
            payload = json.dumps({
                "type": "dm",
                "from_npub": from_npub,
                "to_npub": to_npub,
                "content": content,
            })
            if self._loop:
                self._loop.create_task(client.websocket.send(payload))
                logger.info(f"[Gateway] Routed DM to client {client_id}: {content[:50]}")

        except Exception as e:
            logger.error(f"[Gateway] _on_relay_event error: {e}", exc_info=True)

    def handle_register_request(self, client_id: str) -> dict:
        """Generate a new keypair, save to all_key.json, return to CLI."""
        key = get_or_create_key()
        self._all_keys = load_all_keys()
        npub_hex = npub_to_hex(key["npub"])
        self._clients[client_id].npub_hex = npub_hex
        self._clients[client_id].npub_bech32 = key["npub"]
        self._npub_to_client[npub_hex] = client_id
        logger.info(f"[Gateway] New key: {key['npub'][:30]}...")
        return {"type": "register_done", "npub": key["npub"], "nsec": key["nsec"]}

    def handle_register(self, client_id: str, npub: str) -> dict:
        """Register CLI's existing npub (key.json was already saved locally)."""
        npub_hex = npub_to_hex(npub)
        self._clients[client_id].npub_hex = npub_hex
        self._clients[client_id].npub_bech32 = npub

        # Update routing map (replace if reconnecting)
        old_client_id = self._npub_to_client.get(npub_hex)
        if old_client_id and old_client_id != client_id:
            # Remove old mapping
            self._npub_to_client[npub_hex] = client_id
            logger.info(f"[Gateway] Replaced old client for {npub[:30]}...")
        else:
            self._npub_to_client[npub_hex] = client_id

        # Subscribe relay for this npub
        if self._relay:
            self._relay.subscribe([npub_hex])
            logger.info(f"[Gateway] Subscribed relay for {npub[:30]}...")

        return {"type": "registered", "npub": npub}

    def handle_dm(self, client_id: str, to_npub: str, content: str) -> dict:
        """CLI sends a DM: wrap with NIP-17 and publish to relays."""
        client = self._clients.get(client_id)
        if not client or not client.npub_hex:
            return {"type": "error", "error": "not registered"}

        # nsec_to_hex already imported at module level
        seckey = None
        for k in self._all_keys.values():
            if npub_to_hex(k["npub"]) == client.npub_hex:
                seckey = nsec_to_hex(k["nsec"])
                break

        if not seckey:
            return {"type": "error", "error": "no seckey"}

        recipient_hex = npub_to_hex(to_npub)
        gift_wrap = nip17_wrap_message(
            plaintext=content,
            sender_seckey=seckey,
            sender_pubkey=client.npub_hex,
            recipient_pubkey=recipient_hex,
            recipient_npub=to_npub,
        )

        if self._relay:
            self._loop.create_task(self._relay.publish(gift_wrap))
            logger.info(f"[Gateway] Published DM to {to_npub[:30]}...")
        return {"type": "sent"}

    def add_client(self, client_id: str, websocket):
        self._clients[client_id] = WSClient(id=client_id, websocket=websocket)

    def remove_client(self, client_id: str):
        client = self._clients.pop(client_id, None)
        if client and client.npub_hex:
            self._npub_to_client.pop(client.npub_hex, None)
        logger.info(f"[Gateway] Client {client_id} disconnected")


class WebSocketServer:
    def __init__(self):
        self.handler = GatewayHandler()
        self._running = False

    async def start(self):
        self._running = True
        # Connect relay
        relay = RelayClient(NOSTR_RELAYS)
        connected = await relay.connect()
        if not connected:
            logger.warning("[Gateway] No relay connections established")
        else:
            logger.info(f"[Gateway] Connected to {len(relay._connections)} relays")

        loop = asyncio.get_event_loop()
        self.handler.set_relay(relay, loop)
        asyncio.create_task(relay.listen())

        # Start WS server
        async with websockets.serve(
            self._handle_client, GATEWAY_HOST, GATEWAY_PORT
        ):
            logger.info(f"[Gateway] WS server on {GATEWAY_HOST}:{GATEWAY_PORT}")
            await asyncio.Future()

    async def _handle_client(self, websocket):
        client_id = str(id(websocket))
        self.handler.add_client(client_id, websocket)
        logger.info(f"[Gateway] Client {client_id} connected")

        try:
            async for raw in websocket:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                if msg_type == "register_request":
                    resp = self.handler.handle_register_request(client_id)
                elif msg_type == "register":
                    resp = self.handler.handle_register(client_id, msg.get("npub", ""))
                elif msg_type == "dm":
                    resp = self.handler.handle_dm(
                        client_id,
                        msg.get("to_npub", ""),
                        msg.get("content", ""),
                    )
                else:
                    resp = {"type": "error", "error": f"unknown type: {msg_type}"}

                await websocket.send(json.dumps(resp))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.handler.remove_client(client_id)
```

- [ ] **Step 4: Create gateway/main.py**

```python
"""Gateway entry point: python -m gateway.main"""
import asyncio
import logging
import sys
from pathlib import Path

# Ensure shared/ is importable
_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from gateway.config import LOG_LEVEL
from gateway.websocket_server import WebSocketServer

def main():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ws_server = WebSocketServer()
    try:
        asyncio.run(ws_server.start())
    except KeyboardInterrupt:
        logging.info("[Gateway] Shutdown")

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Create gateway/.env.example**

```
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=7899
NOSTR_RELAYS=wss://relay.damus.io,wss://relay.0xchat.com,wss://nostr.oxtr.dev,wss://relay.primal.net
# Leave NOSTR_RELAYS empty to use defaults above
ALL_KEY_PATH=./all_key.json
LOG_LEVEL=INFO
```

- [ ] **Step 6: Update gateway/__init__.py**

```python
"""Gateway module."""
```

- [ ] **Step 7: Commit**

```bash
git add gateway/
git commit -m "$(cat <<'EOF'
feat: add gateway core

- gateway/config.py: .env loader (GATEWAY_HOST, GATEWAY_PORT, NOSTR_RELAYS, ALL_KEY_PATH)
- gateway/websocket_server.py: WS server with CLI registration, key generation,
  all_key.json management, relay→CLI DM routing, CLI→relay DM publishing
- gateway/main.py: entry point (python -m gateway.main)
- gateway/.env.example: config template
EOF
)"
```

---

## Task 3: CLI Core

**Files:**
- Create: `tg-nostr-bot/cli/config.py`
- Create: `tg-nostr-bot/cli/ws_client.py`
- Create: `tg-nostr-bot/cli/app.py`
- Create: `tg-nostr-bot/cli/main.py`
- Create: `tg-nostr-bot/cli/.env.example`
- Modify: `tg-nostr-bot/cli/__init__.py`

- [ ] **Step 1: Create cli/config.py**

```python
"""CLI configuration from .env."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path so shared/ is importable
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
```

- [ ] **Step 2: Create cli/ws_client.py**

Handles WS connection to Gateway, key registration, send/receive DMs.

```python
"""WebSocket client: connects to Gateway, handles key reg and DM relay."""
import asyncio
import json
import logging
import websockets
from pathlib import Path

logger = logging.getLogger("cli")

# KEY_PATH imported from config to respect .env setting
from cli.config import KEY_PATH


class WSClient:
    def __init__(self, gateway_url: str, on_message: callable):
        self.gateway_url = gateway_url
        self.on_message = on_message  # callback(msg: dict)
        self._ws = None
        self._running = False
        self._tasks = []

    async def connect_and_register(self) -> bool:
        """Connect WS, handle key registration, register npub."""
        retry = 0
        while retry < 3:
            try:
                self._ws = await websockets.connect(self.gateway_url)
                break
            except Exception as e:
                retry += 1
                logger.warning(f"[WS] Connection failed ({retry}/3): {e}")
                await asyncio.sleep(2)

        if not self._ws:
            logger.error("[WS] Could not connect to Gateway after 3 attempts")
            return False

        # Check local key.json
        npub = ""
        if KEY_PATH.exists():
            try:
                with open(KEY_PATH) as f:
                    key_data = json.load(f)
                npub = key_data.get("npub", "")
            except Exception:
                pass

        # Register
        if not npub:
            # Request new key from gateway
            await self._ws.send(json.dumps({"type": "register_request"}))
            resp_raw = await self._ws.recv()
            resp = json.loads(resp_raw)
            if resp.get("type") == "register_done":
                npub = resp["npub"]
                nsec = resp["nsec"]
                KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(KEY_PATH, "w") as f:
                    json.dump({"npub": npub, "nsec": nsec}, f)
                logger.info(f"[WS] Key saved: {npub[:30]}...")
            else:
                logger.error(f"[WS] Unexpected response to register_request: {resp}")
                return False

        # Register npub
        await self._ws.send(json.dumps({"type": "register", "npub": npub}))
        resp_raw = await self._ws.recv()
        resp = json.loads(resp_raw)
        if resp.get("type") == "registered":
            logger.info(f"[WS] Registered: {npub[:30]}...")
        else:
            logger.error(f"[WS] Unexpected response to register: {resp}")
            return False

        return True

    async def run(self):
        """Listen for incoming messages from Gateway."""
        self._running = True
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                if msg_type == "dm":
                    # Forward to app.py via callback
                    if self.on_message:
                        asyncio.create_task(self._safe_callback(msg))
                elif msg_type == "pong":
                    pass
        except websockets.exceptions.ConnectionClosed:
            logger.warning("[WS] Connection closed")
        finally:
            self._running = False

    async def _safe_callback(self, msg: dict):
        """Call on_message safely."""
        try:
            if self.on_message:
                await self.on_message(msg)
        except Exception as e:
            logger.error(f"[WS] on_message callback error: {e}")

    def send_dm(self, to_npub: str, content: str):
        """Fire-and-forget send DM. Must be called from async context."""
        if not self._ws:
            return
        msg = {"type": "dm", "to_npub": to_npub, "content": content}
        asyncio.create_task(self._ws.send(json.dumps(msg)))
```

- [ ] **Step 3: Create cli/app.py**

CLI is pure passthrough — no crypto, no nip17_client. Raw text only.

Adapt from tg_bot/main.py. Key changes:
- Import `send_message` and `handle_update` from this module
- On message received: call `ws_client.send_dm(MSG_TO, text)` instead of echo
- On incoming DM from WS: call `send_message(chat_id, content)`
- State holds `ws_client` instead of NostrRelayPool
- Track `chat_id` per user: when first message arrives from a user, store their chat_id so we can reply

```python
"""CLI FastAPI Webhook server: receives Telegram updates."""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add shared/ to path
_parent = Path(__file__).resolve().parent.parent
if sys.path[0] != str(_parent):
    sys.path.insert(0, str(_parent))

from fastapi import FastAPI, Depends, status
from pydantic import BaseModel
from typing import Optional

import httpx

from cli.config import BOT_TOKEN, WEBHOOK_URL, ALLOWED_USERS, PORT, GATEWAY_WS_URL, KEY_PATH, LOG_LEVEL
from cli.ws_client import WSClient

logger = logging.getLogger("cli")

# ── State ─────────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.http_client = httpx.AsyncClient(timeout=10.0)
        self.ws_client: Optional[WSClient] = None
        # Map from Telegram user_id to chat_id for replies
        self.user_chat_ids: dict[int, int] = {}

_state: Optional[AppState] = None

def get_state() -> AppState:
    return _state

# ── Telegram data models ──────────────────────────────────────────────────────

class User(BaseModel):
    id: int
    username: Optional[str] = None
    first_name: str

class Chat(BaseModel):
    id: int

class Message(BaseModel):
    message_id: int
    from_: Optional[User] = Field(default=None, alias="from")
    chat: Chat
    text: Optional[str] = None

class Update(BaseModel):
    update_id: int
    message: Optional[Message] = None

class SendMessage(BaseModel):
    chat_id: int
    text: str
    parse_mode: Optional[str] = None

# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="tg-nostr-bot CLI")

@app.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook_handler(update: Update, state: AppState = Depends(get_state)):
    async def handle():
        msg = update.message
        if not msg or not msg.text:
            return
        user = msg.from_
        if not user:
            return
        chat_id = msg.chat.id
        user_id = user.id
        text = msg.text

        if user_id not in ALLOWED_USERS:
            logger.warning(f"[TG] Blocked uid={user_id}")
            await state.send_message(chat_id, "No permission")
            return

        # Store chat_id for replies
        state.user_chat_ids[user_id] = chat_id

        # Send to Nostr via Gateway
        to_npub = os.getenv("MSG_TO", "")
        if to_npub and state.ws_client and state.ws_client._running:
            state.ws_client.send_dm(to_npub, text)
            logger.info(f"[TG] Sent to Nostr: {text[:50]}")
        else:
            logger.warning(f"[TG] No MSG_TO or WS not connected")

    asyncio.create_task(handle())
    return {"ok": True}

@app.post("/dm")   # Incoming DM from Nostr
async def nostr_dm_handler(data: dict, state: AppState = Depends(get_state)):
    """Receive DM from Gateway WS, forward to Telegram.
    Called by ws_client's on_message callback."""
    content = data.get("content", "")
    from_npub = data.get("from_npub", "")

    # Reply to the last known chat_id (simplest approach: single-user bot)
    if state.user_chat_ids:
        chat_id = next(iter(state.user_chat_ids.values()))
    else:
        chat_id = None

    if chat_id:
        await state.send_message(chat_id, f"[{from_npub[:16]}...]: {content}")
    return {"ok": True}

# ── Startup ───────────────────────────────────────────────────────────────────

async def startup():
    global _state
    _state = AppState()

    # Connect WS to Gateway
    async def on_dm(msg: dict):
        if _state:
            chat_id = next(iter(_state.user_chat_ids.values()), None)
            if chat_id:
                from_npub = msg.get("from_npub", "")
                content = msg.get("content", "")
                await _state.send_message(chat_id, f"[{from_npub[:16]}...]: {content}")

    ws = WSClient(GATEWAY_WS_URL, on_message=on_dm)
    if await ws.connect_and_register():
        _state.ws_client = ws
        asyncio.create_task(ws.run())
        logger.info("[CLI] WS connected and registered")
    else:
        logger.error("[CLI] Failed to connect to Gateway")

    # Register Telegram webhook
    await register_webhook(_state.http_client, BOT_TOKEN, WEBHOOK_URL)

async def register_webhook(client: httpx.AsyncClient, token: str, webhook_url: str):
    url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}/webhook"
    resp = await client.get(url)
    body = resp.json()
    if body.get("ok"):
        logger.info(f"[CLI] Webhook registered: {webhook_url}/webhook")
    else:
        logger.warning(f"[CLI] Webhook registration failed: {body}")

# Wire up lifespan events
@app.on_event("startup") -> startup  # Note: FastAPI lifespan in modern style needs lifespan=...
# Use the older on_event style for compatibility
# Actually FastAPI 0.100+ uses lifespan. Let's use on_event for compatibility.

@app.on_event("startup")
async def startup_event():
    await startup()

@app.on_event("shutdown")
async def shutdown_event():
    if _state and _state.http_client:
        await _state.http_client.aclose()
    if _state and _state.ws_client and _state.ws_client._ws:
        await _state.ws_client._ws.close()
```

**Note:** The `app.py` above uses the older `@app.on_event` style for FastAPI compatibility. FastAPI 0.100+ prefers the lifespan context manager. If using FastAPI 0.100+, replace with:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield
    if _state and _state.http_client:
        await _state.http_client.aclose()
    if _state and _state.ws_client and _state.ws_client._ws:
        await _state.ws_client._ws.close()

app = FastAPI(lifespan=lifespan)
# Remove @app.on_event decorators
```

Choose the style based on the installed FastAPI version.

- [ ] **Step 4: Create cli/main.py**

```python
"""CLI entry point: python -m cli.main"""
import asyncio
import logging
import sys
import uvicorn
from pathlib import Path

_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from cli.config import LOG_LEVEL, PORT

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

def main():
    logging.info(f"[CLI] Starting on port {PORT}")
    uvicorn.run("cli.app:app", host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Create cli/.env.example**

```
BOT_TOKEN=your_telegram_bot_token
WEBHOOK_URL=https://your-domain.com/bot
ALLOWED_USERS=123456789,987654321
PORT=8000
GATEWAY_WS_URL=ws://127.0.0.1:7899
MSG_TO=npub1...    # default destination npub for incoming TG messages
KEY_PATH=./key.json
LOG_LEVEL=INFO
```

- [ ] **Step 6: Commit**

```bash
git add cli/
git commit -m "$(cat <<'EOF'
feat: add CLI core

- cli/config.py: .env loader (BOT_TOKEN, WEBHOOK_URL, GATEWAY_WS_URL, MSG_TO)
- cli/ws_client.py: WS client, key registration, raw text passthrough
- cli/app.py: FastAPI webhook, pure passthrough (no crypto)
- cli/main.py: entry point (python -m cli.main)
- cli/.env.example: config template
EOF
)"
```

---

## Task 4: Integration & Final

- [ ] **Step 1: Smoke test — Gateway imports**

```bash
source ../code_env/bin/activate
cd gateway
python -c "from websocket_server import WebSocketServer; print('OK')"
```

- [ ] **Step 2: Smoke test — CLI imports**

```bash
source ../code_env/bin/activate
cd cli
python -c "from ws_client import WSClient; print('OK')"
python -c "import cli.config; print('BOT_TOKEN:', cli.config.BOT_TOKEN)"
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: complete tg-nostr-bot — gateway + CLI integration"
```
