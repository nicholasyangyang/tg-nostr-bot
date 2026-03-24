"""CLI entry point: python -m cli.main"""
import argparse
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
    parser = argparse.ArgumentParser(prog="cli.main")
    parser.add_argument("--cwd-dir", required=True, type=str, metavar="DIR")
    args = parser.parse_args()

    cwd_dir = Path(args.cwd_dir).resolve()
    cwd_dir.mkdir(parents=True, exist_ok=True)

    from cli.app import AppState
    state = AppState(cwd_dir)
    import cli.app
    cli.app._state = state

    logging.info(f"[CLI] Starting on port {PORT}")
    uvicorn.run("cli.app:app", host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower(), reload=False)

if __name__ == "__main__":
    main()
