"""fasterapi.cli — `fasterapi` command (dev/serve CLI, stdlib only).

Installed as the ``fasterapi`` console script (see ``pyproject.toml``) and
available as ``python -m fasterapi``::

    fasterapi run myapp:app [--host 127.0.0.1] [--port 8000] [--hyper] [--reload]
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
import time


def load_app(ref: str):  # type: ignore[no-untyped-def]
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"app ref must look like 'module:app', got {ref!r}")
    sys.path.insert(0, os.getcwd())
    module = importlib.import_module(module_name)
    app = getattr(module, attr, None)
    if app is None:
        raise SystemExit(f"{ref!r}: module {module_name!r} has no attribute {attr!r}")
    return app


def _serve_once(args: argparse.Namespace) -> int:
    app = load_app(args.app)
    if args.hyper:
        os.environ["FASTERAPI_HYPER"] = "1"
    workers = {"workers": args.workers} if args.workers else {}
    serve = getattr(app, "serve", None)
    if serve is None:
        raise SystemExit(f"{args.app!r}: object has no serve() — is it a fasterapi/FastAPI app?")
    serve(host=args.host, port=args.port, **workers)
    return 0


def _watch_and_restart(args: argparse.Namespace) -> int:
    """Restart the child server whenever a ``.py`` file under cwd changes."""
    def snapshot() -> dict[str, float]:
        out: dict[str, float] = {}
        for dirpath, _, filenames in os.walk(os.getcwd()):
            if "/." in dirpath or "__pycache__" in dirpath or "/.venv" in dirpath:
                continue
            for fn in filenames:
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    try:
                        out[p] = os.stat(p).st_mtime_ns
                    except OSError:
                        pass
        return out

    child_cmd = [sys.executable, "-m", "fasterapi", "run", args.app,
                 "--host", args.host, "--port", str(args.port)]
    if args.hyper:
        child_cmd.append("--hyper")
    if args.workers:
        child_cmd += ["--workers", str(args.workers)]
    print(f"fasterapi: watching .py files under {os.getcwd()} (Ctrl-C to stop)", flush=True)
    last = snapshot()
    proc = subprocess.Popen(child_cmd)
    try:
        while True:
            time.sleep(0.5)
            if proc.poll() is not None:
                return proc.returncode
            now = snapshot()
            if now != last:
                print("fasterapi: change detected — restarting", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                last = snapshot()
                proc = subprocess.Popen(child_cmd)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.reload:
        return _watch_and_restart(args)
    return _serve_once(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fasterapi", description="fasterapi dev CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="serve an app (uvicorn, or --hyper for the Rust host)")
    run.add_argument("app", help="app reference, e.g. myapp:app")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--hyper", action="store_true", help="serve via the Rust Hyper host")
    run.add_argument("--reload", action="store_true", help="restart on .py changes")
    run.add_argument("--workers", type=int, default=0, help="tokio workers for --hyper (0 = default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
