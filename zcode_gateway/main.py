"""Entrypoint: `python -m zcode_gateway.main` or `python main.py`."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from zcode_gateway.app import create_app
from zcode_gateway.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zcode-gateway",
                                     description="OpenAI/Anthropic-compatible proxy "
                                                 "over Zcode's LLM API")
    parser.add_argument("--config", "-c", default=None, help="path to config.yaml")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.config and not os.environ.get("ZCODE_GATEWAY_CONFIG"):
        # tkinter-free bootstrap: on a headless box this must never pull in
        # gui.py (which imports tkinter at module level)
        from zcode_gateway.paths import bootstrap_config
        args.config = str(bootstrap_config())
    cfg = load_config(args.config)
    # No --host flag on purpose: the gateway is local-only, and a CLI host
    # override bypassed the loopback check that gates admin-key injection in
    # the dashboard (config server.host is the single source of truth).
    host = cfg.server.host
    port = args.port or cfg.server.port
    cfg.server.port = port

    if args.reload:
        # the reloader forks a child that must re-import the app, so it needs
        # an import string, not an already-constructed app object; the config
        # path travels via the environment
        if args.config:
            os.environ.setdefault("ZCODE_GATEWAY_CONFIG", str(args.config))
        if args.port:
            os.environ["ZCODE_GATEWAY_SERVER__PORT"] = str(port)
        uvicorn.run("zcode_gateway.app:create_app", factory=True, host=host,
                    port=port, reload=True, log_level="info",
                    **cfg.server.ssl_kwargs())
        return 0

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info",
                **({"timeout_graceful_shutdown": 10} if sys.platform.startswith("linux") else {}),
                **cfg.server.ssl_kwargs())
    return 0


if __name__ == "__main__":
    sys.exit(main())
