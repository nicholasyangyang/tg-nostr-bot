"""WebSocket server for the Nostr gateway.

Handles:
- register_request: Generate and store new keypair
- register: Register CLI's existing npub
- dm: Route DMs via NIP-17 gift-wrap
- _on_relay_event: Unwrap incoming kind:1059 and route to CLI
- Ping/pong keepalive
"""
import asyncio
import json
import logging
import time
import sys
from pathlib import Path
from typing import Dict, Optional

import websockets

_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from gateway.config import GATEWAY_HOST, GATEWAY_PORT, NOSTR_RELAYS, ALL_KEY_PATH
from gateway.key_manager import (
    generate_keys, npub_to_hex, nsec_to_hex, hex_to_npub,
    nip17_wrap_message, nip17_unwrap, KIND_NIP17_GIFT_WRAP,
    sign_event,
)
from gateway.relay_client import RelayClient

logger = logging.getLogger("gateway")


class GatewayMessageHandler:
    """Handles gateway state: registered clients, all_key.json, DM routing."""

    def __init__(self, key_path: str):
        self._key_path = key_path
        # client_id -> websocket
        self._clients: Dict[str, websockets.WebSocketServerProtocol] = {}
        # npub_hex -> client_id
        self._npub_to_client: Dict[str, str] = {}
        # npub_hex -> seckey_hex (for receiving DMs / unwrapping)
        self._npub_to_seckey: Dict[str, str] = {}
        # npub_hex -> npub (hex -> bech32)
        self._hex_to_npub: Dict[str, str] = {}

        self._relay_client: Optional[RelayClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Load all_key.json
        self._all_keys: Dict[str, dict] = {}
        self._load_all_keys()

    def _load_all_keys(self):
        """Load all_key.json from disk."""
        path = Path(self._key_path)
        if path.exists():
            try:
                with open(path, "r") as f:
                    self._all_keys = json.load(f)
                logger.info(f"[Gateway] Loaded {len(self._all_keys)} keys from {self._key_path}")
                # Also populate _npub_to_seckey so gateway can unwrap DMs for any key
                for npub_hex, val in self._all_keys.items():
                    nsec_raw = val.get("nsec", "")
                    if nsec_raw:
                        seckey_hex = nsec_raw if len(nsec_raw) == 64 else nsec_to_hex(nsec_raw)
                        self._npub_to_seckey[npub_hex] = seckey_hex
                logger.info(f"[Gateway] Loaded {len(self._npub_to_seckey)} seckeys for DM unwrap")
            except Exception as e:
                logger.warning(f"[Gateway] Failed to load {self._key_path}: {e}")
                self._all_keys = {}

    def _save_all_keys(self):
        """Save all_key.json to disk atomically: write to tmp then rename."""
        import os, tempfile
        tmp_path = None
        try:
            path = Path(self._key_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(self._all_keys, f, indent=2)
            os.replace(tmp_path, str(path))
            logger.info(f"[Gateway] Saved {len(self._all_keys)} keys to {self._key_path}")
        except Exception as e:
            logger.error(f"[Gateway] Failed to save {self._key_path}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def set_relay_client(self, relay_client: RelayClient, loop: asyncio.AbstractEventLoop):
        self._relay_client = relay_client
        self._loop = loop

    # ---- Client registry ----

    def add_client(self, client_id: str, websocket: websockets.WebSocketServerProtocol):
        self._clients[client_id] = websocket
        logger.info(f"[Gateway] Client {client_id} added (total: {len(self._clients)})")

    def remove_client(self, client_id: str):
        # Clean up npub mappings for this client
        npubs_to_remove = [npub for npub, cid in self._npub_to_client.items() if cid == client_id]
        for npub in npubs_to_remove:
            self._npub_to_client.pop(npub, None)
            self._npub_to_seckey.pop(npub, None)
            self._hex_to_npub.pop(npub, None)
        self._clients.pop(client_id, None)
        logger.info(f"[Gateway] Client {client_id} removed (total: {len(self._clients)})")

    def register_npub(self, client_id: str, npub: str, seckey: str = ""):
        """Register a client's npub for relay subscription and DM routing."""
        npub_hex = npub_to_hex(npub)
        self._clients[client_id].npub = npub  # type: ignore[attr-defined]
        self._npub_to_client[npub_hex] = client_id
        self._hex_to_npub[npub_hex] = npub
        if seckey:
            seckey_hex = seckey if len(seckey) == 64 else nsec_to_hex(seckey)
            self._npub_to_seckey[npub_hex] = seckey_hex
        logger.info(f"[Gateway] Registered npub={npub[:20]}... for client {client_id}")

    def get_client_by_npub_hex(self, npub_hex: str) -> Optional[str]:
        return self._npub_to_client.get(npub_hex)

    def get_seckey_by_npub_hex(self, npub_hex: str) -> Optional[str]:
        return self._npub_to_seckey.get(npub_hex)

    def send_to_client(self, client_id: str, payload: dict):
        """Send a JSON payload to a specific WebSocket client."""
        if client_id not in self._clients:
            logger.warning(f"[Gateway] Client {client_id} not found")
            return
        try:
            asyncio.create_task(self._clients[client_id].send(json.dumps(payload)))
            logger.info(f"[Gateway] Routed message to client {client_id}")
        except Exception as e:
            logger.error(f"[Gateway] Failed to send to client {client_id}: {e}")

    # ---- Message handling ----

    def handle_message(self, msg: dict) -> dict:
        msg_type = msg.get("type")

        if msg_type == "register_request":
            return self._handle_register_request(msg)
        elif msg_type == "register":
            return self._handle_register(msg)
        elif msg_type == "dm":
            return self._handle_dm(msg)
        elif msg_type == "ping":
            # CLI keepalive ping — respond with pong to prevent tunnel idle timeouts
            return {"type": "pong"}
        return {"type": "error", "error": f"Unknown message type: {msg_type}"}

    def _handle_register_request(self, msg: dict) -> dict:
        """Generate a new keypair, save to all_key.json, return to CLI."""
        keys = generate_keys()
        npub = keys["npub"]
        nsec = keys["nsec"]

        npub_hex = npub_to_hex(npub)
        self._all_keys[npub_hex] = {"npub": npub, "nsec": nsec}
        self._save_all_keys()

        logger.info(f"[Gateway] register_request: generated npub={npub[:20]}...")
        return {"type": "register_done", "npub": npub, "nsec": nsec}

    def _handle_register(self, msg: dict) -> dict:
        """Register CLI's existing npub (with optional seckey for receiving DMs)."""
        npub = msg.get("npub", "")
        seckey = msg.get("seckey", "")

        if not npub:
            return {"type": "error", "error": "npub required"}

        # Store in all_key.json if seckey provided
        npub_hex = npub_to_hex(npub)
        if seckey:
            seckey_hex = seckey if len(seckey) == 64 else nsec_to_hex(seckey)
            self._all_keys[npub_hex] = {"npub": npub, "nsec": seckey}
            self._npub_to_seckey[npub_hex] = seckey_hex
            self._save_all_keys()
        else:
            # Still store the npub entry without seckey
            if npub_hex not in self._all_keys:
                self._all_keys[npub_hex] = {"npub": npub, "nsec": ""}

        logger.info(f"[Gateway] Registered npub={npub[:20]}..., seckey={'present' if seckey else 'missing'}")

        # Subscribe relay for this npub
        if self._relay_client and self._loop:
            self._loop.create_task(self._relay_client.subscribe([npub_hex]))

        return {"type": "registered", "npub": npub}

    def _handle_dm(self, msg: dict) -> dict:
        """CLI sends DM: Gateway wraps with NIP-17 and publishes to relay."""
        from_npub = msg.get("from_npub", "")
        to_npub = msg.get("to_npub", "")
        content = msg.get("content", "")

        if not from_npub or not to_npub or not content:
            return {"type": "error", "error": "from_npub, to_npub, and content are required"}

        # Resolve sender key from registered keys or all_key.json
        from_hex = npub_to_hex(from_npub)
        from_seckey = self._npub_to_seckey.get(from_hex, "")
        if not from_seckey and from_hex in self._all_keys:
            nsec_raw = self._all_keys[from_hex].get("nsec", "")
            if nsec_raw:
                from_seckey = nsec_to_hex(nsec_raw)

        if not from_seckey:
            return {"type": "error", "error": "Sender seckey not found. Use register_request or register with seckey."}

        from_pubkey = npub_to_hex(from_npub)
        to_hex = npub_to_hex(to_npub)

        logger.info(f"[Gateway] DM from={from_npub[:20]}... to={to_npub[:20]}...")

        # NIP-17 wrap and publish
        if self._relay_client and self._loop:
            gift_wrap = nip17_wrap_message(
                plaintext=content,
                sender_seckey=from_seckey,
                sender_pubkey=from_pubkey,
                recipient_pubkey=to_hex,
                recipient_npub=to_npub,
            )
            self._loop.create_task(self._relay_client.publish(gift_wrap))
            logger.info(f"[Gateway] Published gift-wrap DM to relays")
            return {"type": "sent"}
        else:
            return {"type": "error", "error": "Relay not connected"}


class WebSocketServer:
    """WebSocket server that bridges CLI to Nostr relays via NIP-17."""

    def __init__(
        self,
        host: str = GATEWAY_HOST,
        port: int = GATEWAY_PORT,
        key_path: str = ALL_KEY_PATH,
    ):
        self.host = host
        self.port = port
        self.key_path = key_path
        self.handler = GatewayMessageHandler(key_path)
        self._relay_client = RelayClient(NOSTR_RELAYS)
        # Wire up relay event callback
        self._relay_client._on_event = self._on_relay_event
        self._running = False

    def _on_relay_event(self, event: dict):
        """Handle incoming kind:1059 event from relay: unwrap NIP-17 and route to CLI."""
        import secp256k1  # local import
        # Local imports as required by architecture
        from gateway.key_manager import nsec_to_hex, hex_to_npub, nip17_unwrap

        kind = event.get("kind")
        if kind != KIND_NIP17_GIFT_WRAP:
            return

        logger.info(f"[Gateway] Received kind:{kind} event from relay")

        # Find the recipient pubkey from the #p tag
        tags = event.get("tags", [])
        recipient_hex = ""
        for tag in tags:
            if len(tag) >= 2 and tag[0] == "p":
                recipient_hex = tag[1]
                break

        if not recipient_hex:
            logger.warning("[Gateway] No recipient in #p tag")
            return

        # Look up the recipient's seckey
        recipient_seckey = self.handler.get_seckey_by_npub_hex(recipient_hex)
        if not recipient_seckey:
            logger.warning(f"[Gateway] No seckey found for recipient {recipient_hex[:20]}...")
            return

        # Extract recipient pubkey from seckey (first 32 bytes of x-only)
        pk = secp256k1.PrivateKey(bytes.fromhex(recipient_seckey))
        recipient_pubkey = pk.pubkey.serialize()[1:].hex()

        # Unwrap the gift wrap
        rumor = nip17_unwrap(event, recipient_seckey, recipient_pubkey)
        if not rumor:
            logger.warning("[Gateway] Failed to unwrap gift-wrapped DM")
            return

        # Route to the correct client
        sender_hex = rumor.get("pubkey", "")
        plaintext = rumor.get("plaintext", rumor.get("content", ""))
        sender_npub = hex_to_npub(sender_hex)
        recipient_npub = hex_to_npub(recipient_hex)

        client_id = self.handler.get_client_by_npub_hex(recipient_hex)
        if client_id:
            self.handler.send_to_client(client_id, {
                "type": "dm",
                "from_npub": sender_npub,
                "to_npub": recipient_npub,
                "content": plaintext,
            })
        else:
            logger.warning(f"[Gateway] No client connected for recipient {recipient_npub[:20]}...")

    async def start(self):
        """Start the WebSocket server and relay connections."""
        # Connect relay and set up handler
        loop = asyncio.get_running_loop()
        self.handler.set_relay_client(self._relay_client, loop)

        connected = await self._relay_client.connect()
        if connected:
            logger.info(f"[Gateway] Connected to {connected} relay(s)")
        else:
            logger.warning("[Gateway] No relays connected")

        self._running = True
        logger.info(f"[Gateway] WebSocket server listening on {self.host}:{self.port}")

        # Start relay listener in background
        relay_task = asyncio.create_task(self._relay_client.listen())

        try:
            async with websockets.serve(self._handle_client, self.host, self.port):
                await asyncio.Future()  # run forever
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            relay_task.cancel()
            await self._relay_client.disconnect()
            logger.info("[Gateway] Server stopped")

    async def _handle_client(self, websocket: websockets.WebSocketServerProtocol):
        """Handle a single WebSocket client connection with ping/pong keepalive."""
        client_id = str(id(websocket))
        self.handler.add_client(client_id, websocket)
        logger.info(f"[Gateway] Client {client_id} connected")

        last_ping = time.time()
        ping_task: Optional[asyncio.Task] = None

        async def ping_loop():
            nonlocal last_ping
            while True:
                await asyncio.sleep(30)
                try:
                    await websocket.send(json.dumps({"type": "ping"}))
                    last_ping = time.time()
                except Exception:
                    break

        try:
            ping_task = asyncio.create_task(ping_loop())
            async for raw in websocket:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                # Update last_ping FIRST when receiving pong, then check timeout.
                # This prevents a late pong (arrived after ping timeout window)
                # from incorrectly triggering disconnection.
                if msg_type == "pong":
                    last_ping = time.time()
                    continue

                # Check pong timeout AFTER pong processing.
                # CLI pings every 15s, so give 35s window before disconnecting.
                if time.time() - last_ping > 35:
                    logger.warning(f"[Gateway] Client {client_id} pong timeout")
                    break

                # Handle client messages
                response = self.handler.handle_message(msg)

                # Auto-register npub after register_request or register.
                # NOTE: do NOT subscribe here for register_request — _handle_register
                # subscribes when it receives the `register` follow-up, avoiding dup.
                if msg_type == "register_request":
                    npub = response.get("npub", "")
                    nsec = response.get("nsec", "")
                    if npub:
                        self.handler.register_npub(client_id, npub, nsec)
                elif msg_type == "register":
                    npub = msg.get("npub", "")
                    seckey = msg.get("seckey", "")
                    if npub:
                        self.handler.register_npub(client_id, npub, seckey)

                await websocket.send(json.dumps(response))

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"[Gateway] Client {client_id} error: {e}")
        finally:
            if ping_task:
                ping_task.cancel()
            self.handler.remove_client(client_id)
            logger.info(f"[Gateway] Client {client_id} disconnected")
