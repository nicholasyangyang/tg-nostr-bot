"""Gateway entry point: python -m gateway.main"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from gateway.config import LOG_LEVEL
from gateway.websocket_server import WebSocketServer

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd-dir", required=True, type=str, metavar="DIR")
    args = parser.parse_args()

    cwd_dir = Path(args.cwd_dir).resolve()
    cwd_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ws_server = WebSocketServer(cwd_dir=cwd_dir)
    try:
        asyncio.run(ws_server.start())
    except KeyboardInterrupt:
        logging.info("[Gateway] Shutdown")

if __name__ == "__main__":
    main()
