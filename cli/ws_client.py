"""WebSocket client: connects to Gateway, handles key reg and DM relay."""
import asyncio
import json
import logging
import websockets
from pathlib import Path

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
        while self._running:
            try:
                async for raw in self._ws:
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")

                    if msg_type == "pong":
                        continue
                    elif msg_type == "ping":
                        await self._ws.send(json.dumps({"type": "pong"}))
                    elif msg_type == "dm":
                        if self.on_message:
                            asyncio.create_task(self._safe_callback(msg))
                    elif msg_type == "dm_received":
                        pass
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
                        else:
                            logger.warning("[WS] Reconnect register_request unexpected response")
                except Exception as e2:
                    logger.error(f"[WS] Reconnect failed: {e2}, retrying in 10s...")
                    await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"[WS] Unexpected error: {e}")
                await asyncio.sleep(5)

    async def _safe_callback(self, msg: dict):
        try:
            if self.on_message:
                await self.on_message(msg)
        except Exception as e:
            logger.error(f"[WS] on_message error: {e}")

    def send_dm(self, to_npub: str, content: str):
        """Fire-and-forget send DM. Must be called from async context."""
        if not self._ws or not self._running:
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
        except Exception as e:
            logger.error(f"[WS] send_dm failed: {e}")

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()
