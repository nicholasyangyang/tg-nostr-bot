# Key Management --cwd-dir Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI and Gateway require `--cwd-dir` argument for key file location. No default relative paths.

**Architecture:** CLI reads/writes `{cwd}/key.json`, Gateway reads/writes `{cwd}/all_key.json`. Both derive the full path by joining `--cwd-dir` with the filename from config.

---

## File Overview

| File | Change |
|------|--------|
| `cli/main.py` | Add `--cwd-dir` argparse, pass full path to ws_client |
| `cli/config.py` | `KEY_PATH` default: `"key.json"` |
| `cli/ws_client.py` | `connect_and_register()` receives full key path |
| `cli/.env.example` | Add `CWD_DIR=` line |
| `gateway/main.py` | Add `--cwd-dir` argparse, pass full path to WebSocketServer |
| `gateway/config.py` | `ALL_KEY_PATH` default: `"all_key.json"`, remove `__file__` resolution |
| `gateway/websocket_server.py` | `WebSocketServer.__init__` receives full key path |
| `gateway/.env.example` | Add `CWD_DIR=` line |

---

## Task 1: CLI main.py --cwd-dir

**Files:** Modify: `cli/main.py`

- [ ] **Step 1: Read current cli/main.py**

Read the full file to understand current argparse setup.

- [ ] **Step 2: Add argparse for --cwd-dir**

Replace or extend the existing argument parsing. The file currently has no argparse.

```python
# cli/main.py
"""CLI entry point: python -m cli.main --cwd-dir /path/to/data"""
import argparse
# ... existing imports ...

def main():
    parser = argparse.ArgumentParser(description="Telegram-Nostr CLI Bot")
    parser.add_argument(
        "--cwd-dir",
        type=str,
        required=True,
        metavar="DIR",
        help="Data directory for key.json and other files (required)",
    )
    args = parser.parse_args()

    # Resolve to absolute path
    from pathlib import Path
    cwd_dir = Path(args.cwd_dir).resolve()
    cwd_dir.mkdir(parents=True, exist_ok=True)

    from cli.ws_client import WSClient
    # ... rest of main() uses cwd_dir ...
```

- [ ] **Step 3: Pass key_path to WSClient**

In `lifespan()`, compute `key_path = cwd_dir / "key.json"` and pass to `WSClient.__init__`.

- [ ] **Step 4: Commit**

```bash
git add cli/main.py
git commit -m "feat(cli): require --cwd-dir argument for data directory"
```

---

## Task 2: CLI config.py and ws_client.py

**Files:** Modify: `cli/config.py`, `cli/ws_client.py`

- [ ] **Step 1: Update cli/config.py**

```python
# cli/config.py — change KEY_PATH default
KEY_PATH: str = os.getenv("KEY_PATH", "key.json")  # relative, joined with --cwd-dir
```

- [ ] **Step 2: Update WSClient.__init__ signature**

```python
# cli/ws_client.py
class WSClient:
    def __init__(self, gateway_url: str, on_message: callable, key_path: str):
        # key_path is now the full absolute path, no Path(KEY_PATH) lookup needed
        self._key_path = key_path
```

- [ ] **Step 3: Update connect_and_register() to use self._key_path**

Change `Path(KEY_PATH)` to `Path(self._key_path)` in the two places it's used (exists check, open for read, open for write).

```python
# cli/ws_client.py connect_and_register()
if Path(self._key_path).exists():
    with open(self._key_path) as f:
        ...
    Path(self._key_path).parent.mkdir(parents=True, exist_ok=True)
    with open(self._key_path, "w") as f:
        ...
```

- [ ] **Step 4: Update lifespan() in cli/app.py to pass key_path**

```python
# cli/app.py lifespan()
from cli.config import KEY_PATH
# ...
key_path = str(Path(BOT_TOKEN).parent / KEY_PATH)  # WRONG - need to pass from main
```

Wait — `cli/app.py` doesn't have `--cwd-dir` visibility. The cleanest approach: compute `key_path` in `main()` and store it in a module-level variable or pass via `AppState`.

Best approach: store `cwd_dir` as a global in `cli/main.py`, then `cli/app.py` imports it.

```python
# cli/main.py — add module-level variable
CWD_DIR: Path = None

# after parsing:
CWD_DIR = cwd_dir
```

```python
# cli/app.py lifespan()
from cli.main import CWD_DIR
from cli.config import KEY_PATH
key_path = str(CWD_DIR / KEY_PATH)
ws = WSClient(GATEWAY_WS_URL, on_message=on_dm, key_path=key_path)
```

- [ ] **Step 5: Commit**

```bash
git add cli/config.py cli/ws_client.py cli/app.py
git commit -m "feat(cli): key_path derived from --cwd-dir + KEY_PATH filename"
```

---

## Task 3: CLI .env.example

**Files:** Modify: `cli/.env.example`

- [ ] **Step 1: Update .env.example**

```bash
# .env.example
# Required: data directory (absolute path)
CWD_DIR=/path/to/data

# Bot token from @BotFather
BOT_TOKEN=...
```

- [ ] **Step 2: Commit**

```bash
git add cli/.env.example
git commit -m "docs(cli): add CWD_DIR to .env.example"
```

---

## Task 4: Gateway main.py --cwd-dir

**Files:** Modify: `gateway/main.py`

- [ ] **Step 1: Read current gateway/main.py**

- [ ] **Step 2: Add argparse for --cwd-dir**

```python
# gateway/main.py
"""Gateway entry point: python -m gateway.main --cwd-dir /path/to/data"""
import argparse
# ... existing imports ...

def main():
    parser = argparse.ArgumentParser(description="Nostr Gateway Server")
    parser.add_argument(
        "--cwd-dir",
        type=str,
        required=True,
        metavar="DIR",
        help="Data directory for all_key.json and other files (required)",
    )
    args = parser.parse_args()

    from pathlib import Path
    cwd_dir = Path(args.cwd_dir).resolve()
    cwd_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(...)
    ws_server = WebSocketServer(cwd_dir=cwd_dir)
    asyncio.run(ws_server.start())
```

- [ ] **Step 3: Commit**

```bash
git add gateway/main.py
git commit -m "feat(gateway): require --cwd-dir argument for data directory"
```

---

## Task 5: Gateway config.py and websocket_server.py

**Files:** Modify: `gateway/config.py`, `gateway/websocket_server.py`

- [ ] **Step 1: Update gateway/config.py**

```python
# gateway/config.py — remove __file__ resolution, use simple filename
ALL_KEY_PATH: str = os.getenv("ALL_KEY_PATH", "all_key.json")
```

- [ ] **Step 2: Update WebSocketServer.__init__ signature**

```python
# gateway/websocket_server.py
class WebSocketServer:
    def __init__(
        self,
        cwd_dir: Path,  # required, from --cwd-dir
    ):
        self.cwd_dir = cwd_dir
        self.key_path = cwd_dir / ALL_KEY_PATH
        self.handler = GatewayMessageHandler(str(self.key_path))
```

- [ ] **Step 3: Commit**

```bash
git add gateway/config.py gateway/websocket_server.py
git commit -m "feat(gateway): key path derived from --cwd-dir + ALL_KEY_PATH filename"
```

---

## Task 6: Gateway .env.example

**Files:** Modify: `gateway/.env.example`

- [ ] **Step 1: Update .env.example**

```bash
# .env.example
# Required: data directory (absolute path)
CWD_DIR=/path/to/data

GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=7899
NOSTR_RELAYS=wss://relay.damus.io,...
ALL_KEY_PATH=all_key.json
LOG_LEVEL=INFO
```

- [ ] **Step 2: Commit**

```bash
git add gateway/.env.example
git commit -m "docs(gateway): add CWD_DIR to .env.example"
```

---

## Task 7: Integration Test

**Files:** None (manual test)

- [ ] **Step 1: Test CLI without --cwd-dir**

```bash
cd /home/deeptuuk/Code/cc_workdir/tg-nostr-bot
python -m cli.main 2>&1 | head -5
# Expected: argparse error, exit code 2
```

- [ ] **Step 2: Test Gateway without --cwd-dir**

```bash
python -m gateway.main 2>&1 | head -5
# Expected: argparse error, exit code 2
```

- [ ] **Step 3: Test CLI with --cwd-dir (temp dir)**

```bash
mkdir -p /tmp/cli-test-data
python -m cli.main --cwd-dir /tmp/cli-test-data &
sleep 3
ls /tmp/cli-test-data/
# Expected: key.json created
kill %1 2>/dev/null
```

- [ ] **Step 4: Test Gateway with --cwd-dir (temp dir)**

```bash
mkdir -p /tmp/gw-test-data
python -m gateway.main --cwd-dir /tmp/gw-test-data &
sleep 3
ls /tmp/gw-test-data/
# Expected: all_key.json created
kill %1 2>/dev/null
```

- [ ] **Step 5: Commit test results (if any code changes needed)**

---

## Task 8: Final Review

- [ ] Run all tests: `python -m pytest tests/ -q`
- [ ] Verify both services start correctly with --cwd-dir
- [ ] Push all commits
