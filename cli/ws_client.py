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
        self._npub = ""  # stored after registration for reconnect and send_dm
        self._listener_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

    async def connect_and_register(self) -> bool:
        """Connect WS, handle key registration, return True on success."""
        # Connect with retry
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

        # Load local key if exists
        npub = ""
        if Path(KEY_PATH).exists():
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

        # Register npub
        await self._ws.send(json.dumps({"type": "register", "npub": npub}))
        resp = json.loads(await self._ws.recv())
        if resp.get("type") == "registered":
            self._npub = npub
            logger.info(f"[WS] Registered: {npub[:30]}...")
        else:
            logger.error(f"[WS] Unexpected: {resp}")
            return False

        return True

    async def _ping_loop(self):
        """Send keepalive pings to Gateway to prevent tunnel idle timeouts."""
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

    async def _listener_loop(self):
        """Listen for messages from Gateway. Non-blocking — processes each message and continues."""
        logger.debug("[WS] Listener loop started")
        while self._running:
            ws = self._ws
            if not ws:
                logger.debug("[WS] Listener: _ws is None, waiting 1s")
                await asyncio.sleep(1)
                continue

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                # No message in 5s, loop back to check _running
                continue
            except websockets.exceptions.ConnectionClosed:
                logger.warning("[WS] Connection closed, listener exiting")
                break
            except Exception as e:
                logger.warning("[WS] Listener error: %s", e)
                await asyncio.sleep(1)
                continue

            # Process message
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                logger.debug("[WS] msg type=%s", msg_type)

                if msg_type == "pong":
                    continue
                elif msg_type == "ping":
                    await self._ws.send(json.dumps({"type": "pong"}))
                elif msg_type == "dm":
                    logger.info("[WS] DM from=%s content=%s",
                               msg.get("from_npub", "")[:20], msg.get("content", "")[:50])
                    if self.on_message:
                        asyncio.create_task(self._safe_callback(msg))
                elif msg_type == "dm_received":
                    logger.debug("[WS] dm_received ack")
            except Exception as e:
                logger.error("[WS] Error processing msg: %s", e)

        logger.debug("[WS] Listener loop exited")

    async def run(self):
        """Start background listener and ping tasks. Non-blocking."""
        self._running = True
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._listener_task = asyncio.create_task(self._listener_loop())
        logger.debug("[WS] run() started, listener=%s ping=%s",
                     self._listener_task, self._ping_task)

    async def disconnect(self):
        """Stop all tasks and close connection."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            ws = self._ws
            self._ws = None
            await ws.close()
        logger.debug("[WS] disconnected")

    async def _safe_callback(self, msg: dict):
        try:
            if self.on_message:
                await self.on_message(msg)
        except Exception as e:
            logger.error("[WS] on_message error: %s", e)

    def send_dm(self, to_npub: str, content: str):
        """Fire-and-forget send DM. Must be called from async context."""
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
