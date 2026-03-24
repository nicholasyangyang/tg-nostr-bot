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
            logger.info(f"[WS] Registered: {npub[:30]}...")
        else:
            logger.error(f"[WS] Unexpected: {resp}")
            return False

        return True

    async def run(self):
        """Listen for messages from Gateway."""
        self._running = True
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
                    # Gateway acknowledges sent DM
                    pass
        except websockets.exceptions.ConnectionClosed:
            logger.warning("[WS] Connection closed")
        finally:
            self._running = False

    async def _safe_callback(self, msg: dict):
        try:
            if self.on_message:
                await self.on_message(msg)
        except Exception as e:
            logger.error(f"[WS] on_message error: {e}")

    def send_dm(self, to_npub: str, content: str):
        """Fire-and-forget send DM. Must be called from async context."""
        if not self._ws:
            return
        asyncio.create_task(self._ws.send(json.dumps({"type": "dm", "to_npub": to_npub, "content": content})))
