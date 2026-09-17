from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="screaming-camera")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml (created if missing)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    store = ConfigStore(Path(args.config))
    app = create_app(store)
    uvicorn.run(app, host=args.host or store.cfg.server.host, port=args.port or store.cfg.server.port,
                log_level="warning")


if __name__ == "__main__":
    main()
