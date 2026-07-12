"""Command-line entry point for stdio and Streamable HTTP transports."""

from __future__ import annotations

import argparse

import uvicorn

from casda_mcp import __version__
from casda_mcp.observability import configure_logging
from casda_mcp.server import create_http_app, create_mcp_server, mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CASDA Model Context Protocol server")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument(
        "--host", default="127.0.0.1", help="HTTP bind host (default: loopback only)"
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--version", action="version", version=f"casda-mcp {__version__}")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    server = (
        mcp
        if args.host == "127.0.0.1" and args.port == 8000
        else create_mcp_server(host=args.host, port=args.port)
    )
    if args.transport == "streamable-http":
        uvicorn.run(
            create_http_app(server),
            host=args.host,
            port=args.port,
            log_level=args.log_level.lower(),
        )
    else:
        server.run(transport="stdio")
