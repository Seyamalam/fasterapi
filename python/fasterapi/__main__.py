"""`python -m fasterapi` — minimal dev/serve CLI (no third-party deps).

Usage:
    python -m fasterapi run myapp:app [--host 127.0.0.1] [--port 8000] [--hyper] [--workers 4]
    python -m fasterapi --help
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys


def _load_app(ref: str):  # type: ignore[no-untyped-def]
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"app ref must look like 'module:app', got {ref!r}")
    sys.path.insert(0, os.getcwd())
    module = importlib.import_module(module_name)
    app = getattr(module, attr, None)
    if app is None:
        raise SystemExit(f"{ref!r}: module {module_name!r} has no attribute {attr!r}")
    return app


def _cmd_run(args: argparse.Namespace) -> int:
    app = _load_app(args.app)
    if args.hyper:
        os.environ["FASTERAPI_HYPER"] = "1"
    workers = {"workers": args.workers} if args.workers else {}
    serve = getattr(app, "serve", None)
    if serve is None:
        raise SystemExit(f"{args.app!r}: object has no serve() — is it a fasterapi/FastAPI app?")
    serve(host=args.host, port=args.port, **workers)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fasterapi", description="fasterapi dev CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="serve an app (uvicorn, or --hyper for the Rust host)")
    run.add_argument("app", help="app reference, e.g. myapp:app")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--hyper", action="store_true", help="serve via the Rust Hyper host")
    run.add_argument("--workers", type=int, default=0, help="tokio workers for --hyper (0 = default)")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
