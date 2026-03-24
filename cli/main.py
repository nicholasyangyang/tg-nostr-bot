"""CLI entry point: python -m cli.main"""
import logging
import sys
import uvicorn
from pathlib import Path

_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from cli.config import LOG_LEVEL, PORT

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

def main():
    logging.info(f"[CLI] Starting on port {PORT}")
    uvicorn.run("cli.app:app", host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower(), reload=False)

if __name__ == "__main__":
    main()
