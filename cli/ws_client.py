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
        self._ws = None
        self._running = False
        self._npub = ""  # stored after registration for reconnect and send_dm

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

    async def run(self):
        """Listen for messages from Gateway, auto-reconnect on disconnect."""
        self._running = True
        reconnect_attempts = 0
        ping_task: Optional[asyncio.Task] = None
        logger.debug("[WS] run() started, _running=%s", self._running)

        async def client_ping_loop():
            """Send keepalive pings to Gateway to prevent tunnel idle timeouts."""
            ping_count = 0
            while self._running:
                logger.debug("[WS] ping_loop iteration %d, sleeping 20s...", ping_count)
                await asyncio.sleep(20)
                if not self._running:
                    logger.debug("[WS] ping_loop: _running=False, breaking")
                    break
                if not self._ws:
                    logger.debug("[WS] ping_loop: _ws=None, breaking")
                    break
                try:
                    await self._ws.send(json.dumps({"type": "ping"}))
                    ping_count += 1
                    logger.debug("[WS] Ping #%d sent", ping_count)
                except websockets.exceptions.ConnectionClosed as e:
                    logger.warning("[WS] Ping: connection closed, exiting ping loop: %s", e)
                    break
                except Exception as e:
                    logger.warning("[WS] Ping failed: %s, exiting ping loop", e)
                    break
            logger.debug("[WS] ping_loop exited, ping_count=%d", ping_count)

        try:
            ping_task = asyncio.create_task(client_ping_loop())
            logger.debug("[WS] ping_task created: %s", ping_task)
            while self._running:
                try:
                    logger.debug("[WS] Waiting for message on _ws=%s...", id(self._ws))
                    async for raw in self._ws:
                        msg = json.loads(raw)
                        msg_type = msg.get("type", "")
                        logger.debug("[WS] Received msg type=%s, raw[:100]=%s", msg_type, raw[:100])

                        if msg_type == "pong":
                            logger.debug("[WS] Got pong")
                            continue
                        elif msg_type == "ping":
                            logger.debug("[WS] Got ping, replying")
                            await self._ws.send(json.dumps({"type": "pong"}))
                        elif msg_type == "dm":
                            logger.info("[WS] Got dm from=%s content=%s", msg.get("from_npub", "")[:20], msg.get("content", "")[:50])
                            if self.on_message:
                                asyncio.create_task(self._safe_callback(msg))
                        elif msg_type == "dm_received":
                            logger.debug("[WS] Got dm_received")
                            pass
                        else:
                            logger.warning("[WS] Unknown msg type: %s", msg_type)
                except websockets.exceptions.ConnectionClosed as e:
                    reconnect_attempts += 1
                    if reconnect_attempts >= 3:
                        logger.error(f"[WS] Connection closed after {reconnect_attempts} attempts, exiting")
                        self._running = False
                        break
                    delay = 5 * reconnect_attempts
                    logger.warning(f"[WS] Connection closed: {e}, reconnecting in {delay}s (attempt {reconnect_attempts}/3)...")
                    await asyncio.sleep(delay)
                    if not self._running:
                        break
                    # Try to reconnect
                    try:
                        self._ws = await websockets.connect(self.gateway_url)
                        # Re-register using stored npub
                        if self._npub:
                            await self._ws.send(json.dumps({"type": "register", "npub": self._npub}))
                            resp = json.loads(await self._ws.recv())
                            if resp.get("type") == "registered":
                                reconnect_attempts = 0
                                logger.info("[WS] Reconnected and registered")
                                # Restart ping loop for the new connection
                                if ping_task and not ping_task.done():
                                    ping_task.cancel()
                                    try:
                                        await ping_task
                                    except asyncio.CancelledError:
                                        pass
                                ping_task = asyncio.create_task(client_ping_loop())
                            else:
                                logger.warning("[WS] Reconnect register unexpected response")
                        else:
                            logger.warning("[WS] No npub for reconnection, requesting new key")
                            await self._ws.send(json.dumps({"type": "register_request"}))
                            resp = json.loads(await self._ws.recv())
                            if resp.get("type") == "register_done":
                                self._npub = resp["npub"]
                                nsec = resp["nsec"]
                                Path(KEY_PATH).parent.mkdir(parents=True, exist_ok=True)
                                with open(KEY_PATH, "w") as f:
                                    json.dump({"npub": self._npub, "nsec": nsec}, f)
                                await self._ws.send(json.dumps({"type": "register", "npub": self._npub}))
                                resp = json.loads(await self._ws.recv())
                                logger.info("[WS] Reconnected with new key")
                                # Restart ping loop for the new connection
                                if ping_task and not ping_task.done():
                                    ping_task.cancel()
                                    try:
                                        await ping_task
                                    except asyncio.CancelledError:
                                        pass
                                ping_task = asyncio.create_task(client_ping_loop())
                            else:
                                logger.warning("[WS] Reconnect register_request unexpected response")
                    except Exception as e2:
                        logger.error(f"[WS] Reconnect failed: {e2}, retrying in 10s...")
                        await asyncio.sleep(10)
                except Exception as e:
                    logger.error(f"[WS] Unexpected error: {e}")
                    await asyncio.sleep(5)
        finally:
            if ping_task:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    async def _safe_callback(self, msg: dict):
        try:
            if self.on_message:
                await self.on_message(msg)
        except Exception as e:
            logger.error(f"[WS] on_message error: {e}")

    def send_dm(self, to_npub: str, content: str):
        """Fire-and-forget send DM. Must be called from async context."""
        logger.debug("[WS] send_dm called: to=%s content=%s _ws=%s _running=%s _npub=%s",
                     to_npub[:20], content[:50], self._ws is not None, self._running, self._npub[:20])
        if not self._ws or not self._running:
            logger.warning("[WS] send_dm: _ws=%s or _running=%s — skipping", self._ws, self._running)
            return
        if not self._npub:
            logger.warning("[WS] send_dm: not registered yet")
            return
        try:
            asyncio.create_task(self._ws.send(json.dumps({
                "type": "dm",
                "from_npub": self._npub,
                "to_npub": to_npub,
                "content": content,
            })))
            logger.info("[WS] DM task created")
        except Exception as e:
            logger.error("[WS] send_dm failed: %s", e)

    async def disconnect(self):
        self._running = False
        if self._ws:
            ws = self._ws
            self._ws = None
            await ws.close()
