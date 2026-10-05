"""Simple SPARTON launcher.

Usage:
    venv python:   .venv/Scripts/python start_sparton.py   (Windows; .venv/bin/python elsewhere)
    any python:    python start_sparton.py   (deps must be installed)

Options:
    --port 8000     Port to listen on (default 8000)
    --host 0.0.0.0  Bind address (default 127.0.0.1)
    --reload        Enable auto-reload for development
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the SPARTON API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = parser.parse_args()

    # Sensible defaults for local runs (env vars still win if already set).
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(ROOT / 'sparton.db').as_posix()}")
    os.environ.setdefault("LLM_PROVIDER", "ollama")
    os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")

    # Make sure 'app' is importable regardless of where we're launched from.
    sys.path.insert(0, str(ROOT))

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run:")
        print(f"  {sys.executable} -m pip install -r requirements.txt")
        return 1

    # Far and away the most common launch failure, and uvicorn reports it as a
    # bare WinError 10048. Say what is actually wrong instead.
    if _port_in_use(args.host, args.port):
        print(f"Port {args.port} is already in use on {args.host}.")
        print("SPARTON is probably still running in another window.")
        print(f"  Open it:        http://{args.host}:{args.port}/dashboard/")
        print(f"  Or use another: {Path(__file__).name} --port {args.port + 1}")
        return 1

    print("=" * 60)
    print("SPARTON")
    print(f"  Dashboard: http://{args.host}:{args.port}/dashboard/")
    print(f"  Docs:      http://{args.host}:{args.port}/docs")
    print(f"  Database:  {os.environ['DATABASE_URL']}")
    print(f"  LLM:       {os.environ['LLM_PROVIDER']}")
    print("=" * 60)
    # stdout is block-buffered when redirected to a file or pipe; without this
    # the banner only appears once the server exits.
    sys.stdout.flush()

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(ROOT),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
