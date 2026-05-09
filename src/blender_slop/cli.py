"""Command line entry point for the Blender SLOP provider."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Sequence

from slop_ai.transports.stdio import listen as listen_stdio
from slop_ai.transports.websocket import serve as serve_websocket

from .connection import DEFAULT_HOST, DEFAULT_PORT
from .provider import BlenderSlopProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Blender as a SLOP provider.")
    parser.add_argument("--blender-host", default=os.getenv("BLENDER_HOST", DEFAULT_HOST))
    parser.add_argument("--blender-port", type=int, default=int(os.getenv("BLENDER_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--no-connect-on-start", action="store_true", help="Start without trying to connect to Blender.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")

    subparsers = parser.add_subparsers(dest="transport")
    subparsers.add_parser("stdio", help="Serve SLOP over NDJSON stdin/stdout.")

    websocket = subparsers.add_parser("websocket", help="Serve SLOP over a local WebSocket.")
    websocket.add_argument("--host", default="127.0.0.1")
    websocket.add_argument("--port", type=int, default=8765)
    websocket.add_argument("--path", default="/slop")
    websocket.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="Allowed browser Origin for WebSocket upgrades. Repeat for multiple origins.",
    )

    parser.set_defaults(transport="stdio")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    provider = BlenderSlopProvider(
        host=args.blender_host,
        port=args.blender_port,
        connect_on_start=not args.no_connect_on_start,
    )

    if args.transport == "stdio":
        asyncio.run(listen_stdio(provider.slop))
        return

    if args.transport == "websocket":
        asyncio.run(_run_websocket(provider, args.host, args.port, args.path, args.allow_origin))
        return

    raise SystemExit(f"Unknown transport: {args.transport}")


async def _run_websocket(
    provider: BlenderSlopProvider,
    host: str,
    port: int,
    path: str,
    allowed_origins: list[str],
) -> None:
    server = await serve_websocket(
        provider.slop,
        host=host,
        port=port,
        path=path,
        allowed_origins=allowed_origins or None,
    )
    logging.info("Blender SLOP provider listening on ws://%s:%s%s", host, port, path)
    await server.wait_closed()
