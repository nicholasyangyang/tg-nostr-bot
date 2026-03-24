"""WebSocket client: connects to Gateway, handles key reg and DM relay."""
import asyncio
import json
import logging
import websockets
from pathlib import Path

from typing import Optional

from cli.config import KEY_PATH, GATEWAY_WS_URL

logger = logging.getLogger("cli")


class WSClient:
    def __init__(self, gateway_url: str, on_message: callable):
        self.gateway_url = gateway_url
        self.on_message = on_message  # callback(msg: dict)
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._npub = ""
        self._msg_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def connect_and_register(self) -> bool:
        """Connect WS, handle key registration, return True on success."""
        for attempt in range(3):
            try:
                self._ws = await websockets.connect(self.gateway_url)
                break
            except Exception as e:
                logger.warning(f"[WS] Connection attempt {attempt+1}/3 failed: {e}")
                await asyncio.sleep(2)
        if not self._ws:
            logger.error("[WS] Could not connect to Gateway")
            return False

        npub = ""
        if Path(KEY_PATH).exists():
            try:
                with open(KEY_PATH) as f:
                    key_data = json.load(f)
                npub = key_data.get("npub", "")
            except Exception:
                pass

        if not npub:
            await self._ws.send(json.dumps({"type": "register_request"}))
            resp = json.loads(await self._ws.recv())
            if resp.get("type") == "register_done":
                npub = resp["npub"]
                nsec = resp["nsec"]
                Path(KEY_PATH).parent.mkdir(parents=True, exist_ok=True)
                with open(KEY_PATH, "w") as f:
                    json.dump({"npub": npub, "nsec": nsec}, f)
                logger.info(f"[WS] Key saved: {npub[:30]}...")
            else:
                logger.error(f"[WS] Unexpected: {resp}")
                return False

        await self._ws.send(json.dumps({"type": "register", "npub": npub}))
        resp = json.loads(await self._ws.recv())
        if resp.get("type") == "registered":
            self._npub = npub
            logger.info(f"[WS] Registered: {npub[:30]}...")
        else:
            logger.error(f"[WS] Unexpected: {resp}")
            return False

        return True

    async def _producer(self):
        """Receive from Gateway, put raw messages into queue. Never blocks caller."""
        while self._running and self._ws:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                await self._msg_queue.put(msg)
                logger.debug("[WS] Enqueued msg type=%s", msg.get("type"))
            except asyncio.TimeoutError:
                # No message in 5s — loop continues, check _running
                continue
            except websockets.exceptions.ConnectionClosed:
                logger.warning("[WS] Connection closed in producer")
                break
            except Exception as e:
                logger.warning("[WS] Producer error: %s", e)
                await asyncio.sleep(1)

    async def _consumer(self):
        """Consume from queue, dispatch messages. Never blocks producer."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self._msg_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            msg_type = msg.get("type", "")
            logger.debug("[WS] Consuming msg type=%s", msg_type)

            if msg_type == "pong":
                continue
            elif msg_type == "ping":
                if self._ws:
                    asyncio.create_task(self._ws.send(json.dumps({"type": "pong"})))
            elif msg_type == "dm":
                logger.info("[WS] DM from=%s content=%s",
                           msg.get("from_npub", "")[:20], msg.get("content", "")[:50])
                if self.on_message:
                    asyncio.create_task(self._safe_callback(msg))
            elif msg_type == "dm_received":
                logger.debug("[WS] dm_received ack")
            else:
                logger.warning("[WS] Unknown msg type: %s", msg_type)

    async def _ping_loop(self):
        """Send keepalive pings to prevent tunnel idle timeouts."""
        while self._running:
            await asyncio.sleep(20)
            if not self._running or not self._ws:
                break
            try:
                await self._ws.send(json.dumps({"type": "ping"}))
                logger.debug("[WS] Ping sent")
            except Exception as e:
                logger.warning("[WS] Ping failed: %s", e)
                break

    async def run(self):
        """Start background tasks. Producer auto-reconnects on disconnect."""
        self._running = True
        asyncio.create_task(self._producer_with_reconnect())
        asyncio.create_task(self._consumer())
        asyncio.create_task(self._ping_loop())
        logger.debug("[WS] run() spawned 3 background tasks")

    async def _producer_with_reconnect(self):
        """Producer with auto-reconnect: restart producer whenever it exits."""
        reconnect_delay = 2
        while self._running:
            # Run producer until it exits (connection close, error, etc.)
            await self._producer()
            if not self._running:
                break
            # Connection dropped — reconnect
            logger.warning("[WS] Producer exited, reconnecting in %ds...", reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)
            if not self._running:
                break
            try:
                self._ws = await websockets.connect(self.gateway_url)
                if self._npub:
                    await self._ws.send(json.dumps({"type": "register", "npub": self._npub}))
                    resp = json.loads(await self._ws.recv())
                    if resp.get("type") == "registered":
                        reconnect_delay = 2  # reset on success
                        logger.info("[WS] Reconnected and registered")
                    else:
                        logger.warning("[WS] Reconnect register unexpected response")
            except Exception as e:
                logger.warning("[WS] Reconnect failed: %s", e)

    async def disconnect(self):
        """Stop everything and close connection."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Drain queue
        while not self._msg_queue.empty():
            try:
                self._msg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _safe_callback(self, msg: dict):
        try:
            if self.on_message:
                await self.on_message(msg)
        except Exception as e:
            logger.error("[WS] on_message error: %s", e)

    def send_dm(self, to_npub: str, content: str):
        """Fire-and-forget send DM. Non-blocking."""
        if not self._ws or not self._running:
            logger.warning("[WS] send_dm: not connected or not running")
            return
        if not self._npub:
            logger.warning("[WS] send_dm: not registered yet")
            return
        asyncio.create_task(self._ws.send(json.dumps({
            "type": "dm",
            "from_npub": self._npub,
            "to_npub": to_npub,
            "content": content,
        })))
        logger.info("[WS] DM queued to=%s content=%s", to_npub[:20], content[:50])
