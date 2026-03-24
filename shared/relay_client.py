import json
import time
import asyncio
import logging
import websockets
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Set
from websockets.exceptions import ConnectionClosed

from shared.key_manager import npub_to_hex, nsec_to_hex, sign_event, get_private_key, get_public_key
from shared.key_manager import KIND_NIP17_GIFT_WRAP, KIND_NIP17_SEAL, KIND_NIP17_TEXT_MSG, KIND_NIP17_FILE_MSG
from shared.key_manager import nip17_wrap_message, nip17_unwrap

logger = logging.getLogger("relay")



@dataclass
class NostrEvent:
    kind: int
    content: str
    tags: List[List[str]] = field(default_factory=list)
    pubkey: str = ""
    created_at: int = 0
    id: str = ""
    sig: str = ""

    def to_dict_for_id(self) -> dict:
        """Serialize without id/sig for id computation.
        Field order must match Nostr spec: [0, pubkey, created_at, kind, tags, content].
        """
        # Return as array per NIP-01 spec
        return [0, self.pubkey, self.created_at, self.kind, self.tags, self.content]

    def compute_id(self):
        """Compute SHA256 id from serialized event."""
        import hashlib
        data = json.dumps(self.to_dict_for_id(), separators=(",", ":"))
        self.id = hashlib.sha256(data.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "content": self.content,
            "tags": self.tags,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "id": self.id,
            "sig": self.sig,
        }


class RelayConnection:
    """Manages a single WebSocket connection to a Nostr relay."""

    def __init__(self, relay_url: str, parent_client: "RelayClient"):
        self.relay_url = relay_url
        self._parent = parent_client  # keep reference to call parent's _on_event dynamically
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._t0 = int(time.time()) - 7 * 86400  # 7 days back, limit=0 (NIP-16 reactive)
        self._running = False
        self._sub_id = f"sub_{int(time.time())}"

    async def connect(self) -> bool:
        """Connect to the relay and subscribe to DMs."""
        try:
            self.ws = await websockets.connect(self.relay_url)
            self._running = True
            logger.info(f"[Relay] Connected to {self.relay_url}")
            return True
        except Exception as e:
            logger.error(f"[Relay] Failed to connect to {self.relay_url}: {e}")
            return False

    async def subscribe(self, pubkeys: List[str]):
        """Subscribe to DMs for given pubkeys with since=_t0, limit=0."""
        if not self.ws:
            return

        # Convert npub to hex for relay
        hex_pubkeys = [npub_to_hex(pk) for pk in pubkeys]

        # NIP-17 subscription: kind 1059, filter by #p tag (recipient)
        subscription = {
            "sub_id": self._sub_id,
            "subscription": {
                "kinds": [KIND_NIP17_GIFT_WRAP],
                "#p": hex_pubkeys,
                "since": self._t0,
                "limit": 0,
            }
        }
        try:
            await self.ws.send(json.dumps(["REQ", self._sub_id, subscription["subscription"]]))
            logger.info(f"[Relay] Subscribed to DMs for {len(pubkeys)} pubkeys (hex), since={self._t0}, limit=0")
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"[Relay] subscribe failed: connection closed for {self.relay_url}")

    async def listen(self):
        """Listen for events from the relay."""
        if not self.ws:
            return

        try:
            async for message in self.ws:
                await self._handle_message(message)
        except ConnectionClosed:
            logger.warning(f"[Relay] Connection closed: {self.relay_url}")
            self._running = False

    async def _handle_message(self, message: str):
        """Handle incoming relay messages."""
        try:
            data = json.loads(message)
            # Relay messages are arrays: ["EVENT", sub_id, event], ["EOSE", sub_id], ["OK", id, accepted, reason], ["Notice", message], etc.
            if isinstance(data, list) and len(data) >= 2:
                msg_type = data[0]
                if msg_type == "EVENT" and len(data) >= 3:
                    event = data[2]
                    kind = event.get("kind")
                    logger.debug(f"[Relay] _handle_message: EVENT kind={kind}")
                    if kind == KIND_NIP17_GIFT_WRAP:
                        logger.info(f"[Relay] Received DM event kind={kind} from {event.get('pubkey', '')[:20]}...")
                        logger.debug(f"[Relay] Calling _parent._on_event, _parent={type(self._parent).__name__}")
                        self._parent._on_event(event)
                elif msg_type == "OK" and len(data) >= 3:
                    event_id = str(data[1])
                    accepted = data[2]
                    reason = str(data[3]) if len(data) > 3 else ""
                    if accepted:
                        logger.debug(f"[Relay] Relay accepted event {event_id[:16]}...")
                    else:
                        logger.warning(f"[Relay] Relay rejected event {event_id[:16]}...: {reason}")
                elif msg_type == "EOSE":
                    logger.debug(f"[Relay] End of stored events: {data[1]}")
        except json.JSONDecodeError:
            logger.warning(f"[Relay] Invalid JSON from {self.relay_url}: {message[:100]}")
        except Exception as e:
            logger.error(f"[Relay] _handle_message error: {e}", exc_info=True)

    async def close(self):
        """Close the connection."""
        self._running = False
        if self.ws:
            try:
                # Send CLOSE message
                await self.ws.send(json.dumps(["CLOSE", self._sub_id]))
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


class RelayClient:
    """Manages connections to multiple Nostr relays."""

    def __init__(self, relays: List[str]):
        self.relays = relays
        self._connections: List[RelayConnection] = []
        self._running = False
        self._t0 = int(time.time()) - 7 * 86400  # 7 days back, limit=0 (NIP-16 reactive)  # Subscription start time
        self._all_subscribed_npub: Set[str] = set()

    async def connect(self) -> bool:
        """Connect to all relays."""
        self._running = True
        connected = 0
        for relay in self.relays:
            conn = RelayConnection(relay, self)
            if await conn.connect():
                self._connections.append(conn)
                connected += 1
        return connected > 0

    async def subscribe(self, pubkeys: List[str]):
        """Subscribe to DMs for given pubkeys on all relays.

        Deduplicates globally across all connections — calling subscribe([A])
        then subscribe([B]) merges to a single subscription with both npubs.
        """
        self._all_subscribed_npub.update(pubkeys)
        for conn in self._connections:
            await conn.subscribe(self._all_subscribed_npub)

    async def listen(self):
        """Listen for events from all relays, auto-reconnect on disconnect."""
        backoff = 10  # seconds, doubles up to 300s (5 min)
        while self._running:
            # Reconnect dead connections
            for conn in self._connections:
                if not conn._running and conn.ws is None:
                    logger.info(f"[Relay] Attempting to reconnect to {conn.relay_url}")
                    if await conn.connect():
                        await conn.subscribe(self._all_subscribed_npub)
                        backoff = 10  # reset on success
                    else:
                        logger.warning(f"[Relay] Reconnect failed to {conn.relay_url}, will retry")

            # Run listeners for active connections
            tasks = [conn.listen() for conn in self._connections if conn._running]
            if not tasks:
                logger.warning(f"[Relay] No active connections, waiting {backoff}s to reconnect...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            # Reset backoff on successful active connections
            backoff = 10

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"[Relay] Listener error on {self._connections[i].relay_url}: {result}")

    def _on_event(self, event: dict):
        """Handle received event (called from relay connection)."""
        logger.info(f"[Relay] Event received: kind={event.get('kind')}, from={event.get('pubkey', '')[:20]}...")

    async def disconnect(self):
        """Disconnect from all relays."""
        self._running = False
        for conn in self._connections:
            await conn.close()
        self._connections.clear()

    async def publish(self, event: dict) -> bool:
        """Publish an event to all relays."""
        if not self._connections:
            return False

        success = False
        for conn in self._connections:
            if conn.ws:
                try:
                    await conn.ws.send(json.dumps(["EVENT", event]))
                    success = True
                    logger.info(f"[Relay] Published event to {conn.relay_url}")
                except Exception as e:
                    logger.error(f"[Relay] Failed to publish to {conn.relay_url}: {e}")
        return success

    async def publish_metadata(self, pubkey_hex: str, seckey_hex: str, name: str = "", about: str = "", nip05: str = ""):
        """Publish kind:0 metadata event to make npub discoverable on relays."""
        from shared.key_manager import _event_id, sign_event

        content = {}
        if name:
            content["name"] = name
        if about:
            content["about"] = about
        if nip05:
            content["nip05"] = nip05

        event = {
            "pubkey": pubkey_hex,
            "created_at": int(time.time()),
            "kind": 0,
            "tags": [],
            "content": json.dumps(content, separators=(",", ":"), ensure_ascii=False),
        }
        event["id"] = _event_id(event)
        event["sig"] = sign_event(event, seckey_hex)

        await self.publish(event)
        logger.info(f"[Relay] Published kind:0 metadata for {pubkey_hex[:16]}... name='{name}'")

    def parse_dm(self, event: dict, my_seckey: str, my_pubkey: str) -> Optional[dict]:
        """Parse a NIP-17 gift-wrapped DM event (kind=1059).
        Unwraps using the recipient's key and returns the plaintext message.
        """
        kind = event.get("kind")
        if kind != KIND_NIP17_GIFT_WRAP:
            return None

        rumor = nip17_unwrap(event, my_seckey, my_pubkey)
        if not rumor:
            return None

        from shared.key_manager import hex_to_npub

        return {
            "id": event.get("id"),
            "from_npub": hex_to_npub(rumor.get("pubkey")),
            "to_npub": hex_to_npub(my_pubkey),
            "content": rumor.get("plaintext"),
            "created_at": rumor.get("created_at"),
        }

    def publish_dm(self, from_npub: str, to_npub: str, content: str):
        """Fire-and-forget publish DM to all relays (non-blocking)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        asyncio.create_task(self._publish_dm_async(from_npub, to_npub, content))

    async def _publish_dm_async(self, from_npub: str, to_npub: str, content: str, from_seckey: str = ""):
        """Publish NIP-17 gift-wrapped DM using sender's key."""
        from shared.key_manager import nsec_to_hex
        logger.info(f"[Relay] PUBLISH -> to: {to_npub}, msg: {content}")
        if not self._connections:
            logger.warning("[Relay] No connections to publish DM")
            return

        sender_pubkey = npub_to_hex(from_npub)
        recipient_npub = to_npub
        recipient_hex = npub_to_hex(recipient_npub)

        # Convert nsec bech32 to hex if needed
        sender_seckey_hex = from_seckey
        if sender_seckey_hex.startswith("nsec1"):
            sender_seckey_hex = nsec_to_hex(sender_seckey_hex)

        # If no sender key provided, use gateway's own key
        if not sender_seckey_hex:
            raise ValueError("[Relay] No sender key provided — cannot publish DM")

        # Create gift-wrapped event
        gift_wrap = nip17_wrap_message(
            plaintext=content,
            sender_seckey=sender_seckey_hex,
            sender_pubkey=sender_pubkey,
            recipient_pubkey=recipient_hex,
            recipient_npub=recipient_npub,
        )

        for conn in self._connections:
            if conn.ws:
                try:
                    event_json = json.dumps(["EVENT", gift_wrap])
                    await conn.ws.send(event_json)
                    logger.info(f"[Relay] Published gift-wrap to {conn.relay_url}")
                except Exception as e:
                    logger.error(f"[Relay] Failed to publish to {conn.relay_url}: {e}")
