"""CLI FastAPI Webhook server: receives Telegram updates."""
import asyncio
import logging
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager

_parent = Path(__file__).resolve().parent.parent
if sys.path[0] != str(_parent):
    sys.path.insert(0, str(_parent))

from fastapi import FastAPI, Depends
from pydantic import BaseModel, Field
from typing import Optional

import httpx

from cli.config import BOT_TOKEN, WEBHOOK_URL, ALLOWED_USERS, PORT, GATEWAY_WS_URL, LOG_LEVEL
from cli.ws_client import WSClient

logger = logging.getLogger("cli")

# ── State ─────────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.http_client: httpx.AsyncClient = httpx.AsyncClient(timeout=10.0)
        self.ws_client: Optional[WSClient] = None
        self.user_chat_ids: dict[int, int] = {}

    async def send_message(self, chat_id: int, text: str):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        try:
            await self.http_client.post(url, json=payload)
        except Exception as e:
            logger.error(f"[TG] send_message error: {e}")

_state: Optional[AppState] = None

def get_state() -> AppState:
    return _state

# ── Telegram data models ──────────────────────────────────────────────────────

class Message(BaseModel):
    message_id: int
    from_field: Optional[dict] = Field(default=None, alias="from")
    chat: dict
    text: Optional[str] = None

    class Config:
        populate_by_name = True

class Update(BaseModel):
    update_id: int
    message: Optional[Message] = None

# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    _state = AppState()

    async def on_dm(msg: dict):
        if not _state:
            return
        if _state.user_chat_ids:
            chat_id = next(iter(_state.user_chat_ids.values()))
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

    if BOT_TOKEN and WEBHOOK_URL:
        await register_webhook(_state.http_client, BOT_TOKEN, WEBHOOK_URL)

    yield

    if _state:
        if _state.http_client:
            await _state.http_client.aclose()
        if _state.ws_client:
            await _state.ws_client.disconnect()

app = FastAPI(title="tg-nostr-bot CLI", lifespan=lifespan)

async def register_webhook(client: httpx.AsyncClient, token: str, webhook_url: str):
    url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}/webhook"
    try:
        resp = await client.get(url)
        body = resp.json()
        if body.get("ok"):
            logger.info(f"[CLI] Webhook registered: {webhook_url}/webhook")
        else:
            logger.warning(f"[CLI] Webhook registration failed: {body}")
    except Exception as e:
        logger.warning(f"[CLI] Webhook registration error: {e}")

@app.post("/webhook")
async def webhook_handler(update: Update, state: AppState = Depends(get_state)):
    async def handle():
        msg = update.message
        if not msg or not msg.text:
            return
        from_field = msg.from_field
        if not from_field:
            return
        user_id = from_field.get("id")
        chat_id = msg.chat.get("id")
        text = msg.text

        if user_id not in ALLOWED_USERS:
            logger.warning(f"[TG] Blocked uid={user_id}")
            await state.send_message(chat_id, "No permission")
            return

        state.user_chat_ids[user_id] = chat_id

        to_npub = os.getenv("MSG_TO", "")
        if to_npub and state.ws_client and state.ws_client._running:
            state.ws_client.send_dm(to_npub, text)
            logger.info(f"[TG] Sent to Nostr: {text[:50]}")
        else:
            logger.warning(f"[TG] No MSG_TO or WS not connected")

    asyncio.create_task(handle())
    return {"ok": True}
