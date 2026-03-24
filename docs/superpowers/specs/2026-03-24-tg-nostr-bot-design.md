# tg-nostr-bot — Design Spec

**Date:** 2026-03-24
**Status:** Approved

## Overview

A Telegram-Nostr bridge. Each CLI instance bridges a Telegram Bot to the Nostr network. The Gateway acts as a WebSocket message router and relay pool manager. Multiple CLI instances can connect to a single Gateway, each with an independent npub.

## Architecture

```
[Telegram Bot] → POST /webhook → [CLI (FastAPI)]
                                          ↓ WebSocket
                                    [Gateway (WS Server)]
                                          ↓ Relay WebSocket
                                   [Nostr Relay Pool]
```

**Message Flow:**

- **TG → Nostr**: Telegram → CLI Webhook → NIP-17 encrypt → WebSocket → Gateway → Relay
- **Nostr → TG**: Relay → Gateway → decrypt NIP-17 → route by to_npub → WebSocket → CLI → Telegram Bot

## Components

### Gateway

**Responsibility:** WebSocket server + Nostr relay pool manager + key registry

- Manages `all_key.json` with all registered npub/nsec pairs
- Accepts WebSocket connections from multiple CLI instances
- Subscribes to Nostr relays (kind:1059) for each registered npub on demand
- Maintains a `npub → WebSocket client` mapping for routing
- Routes messages: decrypts incoming NIP-17 DM, extracts `to_npub`, looks up the corresponding WS client and forwards
- If `to_npub` has no registered WS client → log warning, drop message
- Forwards outgoing messages from CLI to relays

**CLI:**
- FastAPI webhook server (receives Telegram updates)
- WebSocket client (connects to Gateway)
- Each CLI has one independent npub
- Startup: reads `key.json` → if missing, requests from Gateway → saves locally
- Encrypts outgoing messages with NIP-17 Gift Wrap, sends to Gateway
- Decrypts incoming NIP-17 Gift Wrap messages, forwards to Telegram

## Project Structure

```
tg-nostr-bot/
├── gateway/
│   ├── __init__.py
│   ├── main.py              # Entry: python -m gateway.main
│   ├── config.py            # .env loader
│   ├── key_manager.py        # NIP-44 / NIP-17 (reused from py_gateway)
│   ├── relay_client.py       # Relay pool (reused from py_gateway)
│   ├── websocket_server.py   # WS server, CLI registration + routing
│   ├── all_key.json          # All npub/nsec managed by gateway
│   ├── .env.example
│   └── requirements.txt
└── cli/
    ├── __init__.py
    ├── main.py               # Entry: python -m cli.main
    ├── config.py             # .env loader
    ├── app.py                # FastAPI Webhook (Telegram)
    ├── ws_client.py          # WebSocket client to Gateway
    ├── nip17_client.py       # NIP-17 encrypt/decrypt wrappers
    ├── key.json              # Local key (persistent across restarts)
    ├── .env.example
    └── requirements.txt
```

## Key Flow

### CLI Startup

1. Load `.env` (BOT_TOKEN, GATEWAY_WS_URL, MSG_TO, PORT, ALLOWED_USERS)
2. Check local `key.json`:
   - Exists → load npub/nsec
   - Missing → open one WS connection to Gateway, send `{"type":"register_request"}`, receive `{"type":"register_done","npub":"...","nsec":"..."}`, **save to `key.json`**, **reuse same WS connection** for next step
3. On the same WS connection, send `{"type":"register","npub":"<npub>"}`
4. Gateway adds this npub to its routing map and subscribes relay for kind:1059 targeting this npub
5. Start FastAPI webhook server on `POST /webhook`

### Gateway Startup

1. Load `.env` (GATEWAY_HOST, GATEWAY_PORT, NOSTR_RELAYS)
2. Start WebSocket server on GATEWAY_PORT
3. Connect to Nostr relays
4. Load `all_key.json` (create if missing)
5. Wait for CLI registrations

### Outgoing Message (TG → Nostr)

```
Telegram webhook (POST /webhook) → CLI app.py
  → nip17_client.wrap(plaintext, my_nsec, MSG_TO_npub)
  → ws_client.send({"type": "dm", "to_npub": "...", "content": "..."})
  → Gateway → relay_client.publish(gift_wrap_event)
```

`app.py` calls `ws_client.send_dm(to_npub, content)` directly (same process, async). The WS connection is held open for the lifetime of the CLI.

### Incoming Message (Nostr → TG)

```
relay_client receives kind:1059 → key_manager.nip17_unwrap(seckey, event)
  → extract to_npub → websocket_server looks up npub→WS client map
  → send {"type": "dm", ...} to that WS client
  → CLI ws_client receives → nip17_client.unwrap()
  → app.py → Telegram Bot API sendMessage(chat_id, text)
```

## WebSocket Protocol

> **Security note:** `register_request` returns `nsec` over WS. This is acceptable because the Gateway and CLI communicate over a trusted network (localhost or private network). In production, ensure the GATEWAY_WS_URL uses `127.0.0.1` or a trusted LAN address.

### CLI → Gateway

```json
// Key request (only if key.json is missing locally)
{"type": "register_request"}

// Register this CLI's npub and subscribe relay for it
{"type": "register", "npub": "npub1..."}

// Send a DM to Nostr
{"type": "dm", "to_npub": "npub1...", "content": "hello"}
```

### Gateway → CLI

```json
// Key response: returns newly generated npub/nsec
{"type": "register_done", "npub": "npub1...", "nsec": "nsec1..."}

// Incoming DM: decrypted NIP-17 message from Nostr relay
{"type": "dm", "from_npub": "npub1...", "to_npub": "npub1...", "content": "hello"}
```

### Gateway-side Routing

Gateway maintains: `Dict[npub_hex, WebSocketClient]`

When `register` arrives: add mapping `npub → client`, subscribe `kind:1059` events where `#p = npub_hex`.
When receiving a kind:1059 event from relay:
1. Look up `to_npub` (from `#p` tag)
2. If `npub` in map → forward to that client
3. Else → log warning "no client for {npub}, dropping"

## Configuration

### gateway/.env.example

```
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=7899
NOSTR_RELAYS=wss://relay.damus.io,wss://relay.0xchat.com,wss://nostr.oxtr.dev,wss://relay.primal.net
LOG_LEVEL=INFO
```

> If NOSTR_RELAYS is empty/not set, fall back to default relays above.

### cli/.env.example

```
BOT_TOKEN=your_telegram_bot_token
WEBHOOK_URL=https://your-domain.com/bot
ALLOWED_USERS=123456789,987654321
PORT=8000
GATEWAY_WS_URL=ws://127.0.0.1:7899
MSG_TO=npub1...    # default destination npub for incoming TG messages
LOG_LEVEL=INFO
```

## Shared Modules (from py_gateway / tg_bot)

These modules are copied/adapted from existing projects into `tg-nostr-bot`:

| Module | Source | Notes |
|--------|--------|-------|
| NIP-44 encrypt/decrypt | py_gateway/key_manager.py | XChaCha20-Poly1305 (NIP-44 spec) |
| NIP-17 Gift Wrap | py_gateway/key_manager.py | nip17_wrap_message, nip17_unwrap |
| Relay pool | py_gateway/relay_client.py | Connect, subscribe, publish |
| WS Server | py_gateway/websocket_server.py | Adapt for CLI registration + routing |
| FastAPI Webhook pattern | tg_bot/main.py | Adapt for CLI app |
| .env loader | Both | python-dotenv |

## Dependencies

```
# gateway
aiohttp
websockets
python-dotenv
secp256k1
bech32
cryptography

# cli
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

## Error Handling

- Gateway: relay connection failure → warning log, continue without relay
- CLI: WS connection failure → retry 3 times, then exit(1)
- Telegram API failure → error log, continue
- NIP-17 unwrap failure → skip message, log warning
- Missing `key.json` on startup → request from Gateway, retry once on failure
- `all_key.json` corruption/malformed JSON → treat as empty, regenerate
- Duplicate `register` for same npub → replace old WS client mapping (graceful reconnect)
- WS ping/pong: Gateway sends ping every 30s; if pong not received in 10s → close client connection
