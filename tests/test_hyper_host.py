"""Hyper host differential tests: real servers, hyper vs uvicorn byte parity.

Spins both hosts as subprocesses on ephemeral ports and compares wire bytes.
Slow by design (~20s) — runs in the full suite / pre-push, not pre-commit.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

APP_CODE = """
import sys
sys.path.insert(0, {root!r})
import os
os.environ.setdefault("FASTERAPI_PORT_FILE", {datafile!r})
from fasterapi import FastAPI
from fasterapi.responses import FileResponse

app = FastAPI(title="hyper-test")

@app.get("/ping")
async def ping():
    return {{"pong": True}}

@app.get("/file")
async def get_file():
    return FileResponse(
        os.environ["FASTERAPI_PORT_FILE"], media_type="application/octet-stream"
    )

@app.post("/echo")
async def echo(payload: dict):
    return payload
"""

APP_CODE_CORS = APP_CODE + """
from fasterapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["https://x.example"], allow_methods=["GET"])
"""

APP_CODE_WS = """
import sys
sys.path.insert(0, {root!r})
from fasterapi import FastAPI, WebSocket

app = FastAPI(title="hyper-ws-test")

@app.get("/ping")
async def ping():
    return dict(pong=True)

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_text()
    await websocket.send_text(f"echo:{{data}}")
    await websocket.close()

@app.websocket("/auth")
async def wsa(websocket: WebSocket):
    await websocket.accept()
    token = websocket.query_params.get("token", "")
    if token != "s3cr3t":
        await websocket.close(code=4401)
        return
    await websocket.send_text("authed")
    await websocket.close()
"""


def _write_app(tmp: Path, name: str, code: str, datafile: str) -> Path:
    root = str(Path(__file__).resolve().parents[1] / "python")
    path = tmp / f"{name}.py"
    path.write_text(code.format(root=root, datafile=datafile))
    return path


def _free_port(retries: int = 20) -> int:
    import socket

    last: OSError | None = None
    for _ in range(retries):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError as exc:  # transient ephemeral exhaustion under load
            last = exc
            time.sleep(0.5)
    raise RuntimeError("no ephemeral ports available") from last


def _start(app_file: Path, port: int, *, hyper: bool) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("FASTERAPI_HYPER", None)
    cmd = [sys.executable, "-m", "fasterapi", "run", f"{app_file.stem}:app",
           "--host", "127.0.0.1", "--port", str(port)]
    if hyper:
        cmd.append("--hyper")
    return subprocess.Popen(cmd, cwd=str(app_file.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def _wait(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"server on {port} never came up")


def _get(port: int, path: str, **kw):  # type: ignore[no-untyped-def]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", **kw)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            # Hyper lowercases wire headers, uvicorn title-cases: compare folded.
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


@pytest.fixture()
def file_app(tmp_path):
    data = tmp_path / "payload.bin"
    data.write_bytes(bytes(range(256)) * 1024)  # 256 KiB
    app = _write_app(tmp_path, "hyperapp", APP_CODE, str(data))
    ports = {"hyper": _free_port(), "uvicorn": _free_port()}
    procs = [_start(app, ports["hyper"], hyper=True), _start(app, ports["uvicorn"], hyper=False)]
    try:
        _wait(ports["hyper"])
        _wait(ports["uvicorn"])
        yield ports
    finally:
        for p in procs:
            _stop(p)


def test_hyper_file_bytes_identical(file_app):
    s1, h1, b1 = _get(file_app["hyper"], "/file")
    s2, h2, b2 = _get(file_app["uvicorn"], "/file")
    assert (s1, b1) == (s2, b2) == (200, b1) and len(b1) == 256 * 1024
    assert h1["content-type"] == h2["content-type"] == "application/octet-stream"
    assert h1["content-length"] == h2["content-length"] == str(256 * 1024)
    assert h1.get("server") == "fasterapi-hyper"
    assert not [k for k in h1 if k.lower().startswith("x-fasterapi-")]


def test_hyper_json_bytes_identical(file_app):
    import json

    body = json.dumps({"a": [1, 2, {"b": "x"}]}).encode()
    kw = {"data": body, "headers": {"Content-Type": "application/json"}, "method": "POST"}
    s1, h1, b1 = _get(file_app["hyper"], "/echo", **kw)
    s2, h2, b2 = _get(file_app["uvicorn"], "/echo", **kw)
    assert (s1, b1) == (s2, b2)
    assert s1 == 200
    assert json.loads(b1) == {"a": [1, 2, {"b": "x"}]}


def test_hyper_413(tmp_path):
    app = _write_app(tmp_path, "hyper413", APP_CODE, str(tmp_path / "x"))
    port = _free_port()
    p = _start(app, port, hyper=True)
    try:
        _wait(port)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/echo", data=b"x" * (17 * 1024 * 1024),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=30)
            raise AssertionError("expected 413")
        except urllib.error.HTTPError as e:
            assert e.code == 413
    finally:
        _stop(p)


def test_hyper_middleware_fallback(tmp_path):
    data = tmp_path / "p.bin"
    data.write_bytes(b"0123456789")
    app = _write_app(tmp_path, "hypercors", APP_CODE_CORS, str(data))
    port = _free_port()
    p = _start(app, port, hyper=True)
    try:
        _wait(port)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/ping", headers={"Origin": "https://x.example"})
        with urllib.request.urlopen(req, timeout=10) as r:
            # Full-stack fallback ran: CORS headers present through Hyper.
            # (email.message headers are case-insensitive; _get folds anyway.)
            assert r.headers.get("Access-Control-Allow-Origin") == "https://x.example"
            assert r.status == 200
    finally:
        _stop(p)


def test_hyper_graceful_shutdown_runs_lifespan(tmp_path):
    code = APP_CODE.replace(
        "app = FastAPI(title=\"hyper-test\")",
        "from contextlib import asynccontextmanager\n"
        "import pathlib as _pl\n"
        "@asynccontextmanager\n"
        "async def _life(app):\n"
        "    _pl.Path(r\"MARKER\").write_text(\"up\")\n"
        "    yield\n"
        "    _pl.Path(r\"MARKER\").write_text(\"up-down\")\n"
        "app = FastAPI(title=\"hyper-test\", lifespan=_life)",
    ).replace("MARKER", str(tmp_path / "life.txt"))
    app = _write_app(tmp_path, "hyperlife", code, str(tmp_path / "x"))
    port = _free_port()
    p = _start(app, port, hyper=True)
    try:
        _wait(port)
        assert (tmp_path / "life.txt").read_text() == "up"
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        assert (tmp_path / "life.txt").read_text() == "up-down"
    finally:
        if p.poll() is None:
            p.kill()


def _ws_raw(host, port, path):
    """Stdlib raw websocket client (masked frames, no deps)."""
    import base64
    import hashlib
    import os as _os
    import socket as _socket

    sock = _socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(_os.urandom(16)).decode()
    sock.sendall(
        (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
         "Upgrade: websocket\r\nConnection: Upgrade\r\n"
         f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError(f"handshake failed: {head!r}")
        head += chunk
    assert b" 101 " in head.split(b"\r\n", 1)[0], head[:60]

    def send(op, payload: bytes):
        mask = _os.urandom(4)
        hdr = bytes([0x80 | op])
        n = len(payload)
        hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
        sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(timeout=10.0):
        sock.settimeout(timeout)
        hdr = _recvall(2)
        op, ln = hdr[0] & 0x0F, hdr[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(_recvall(2), "big")
        data = _recvall(ln) if ln else b""
        if op == 0x1:
            return ("text", data.decode())
        if op == 0x8:
            return ("close", int.from_bytes(data[:2], "big") if len(data) >= 2 else 1000)
        return ("other", (op, data))

    def _recvall(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("eof")
            buf += chunk
        return buf

    return sock, send, recv


def test_hyper_websocket_bytes_identical(tmp_path):
    app = _write_app(tmp_path, "hyperws", APP_CODE_WS, str(tmp_path / "x"))
    hyper_port, uvicorn_port = _free_port(), _free_port()
    ph = _start(app, hyper_port, hyper=True)
    pu = _start(app, uvicorn_port, hyper=False)
    try:
        _wait(hyper_port)
        _wait(uvicorn_port)
        for port in (hyper_port, uvicorn_port):
            sock, send, recv = _ws_raw("127.0.0.1", port, "/ws")
            try:
                send(0x1, b"hi")
                assert recv() == ("text", "echo:hi"), port
                assert recv()[0] == "close", port
            finally:
                sock.close()
        # auth gate: bad token closes, good token greets — both hosts alike
        for port in (hyper_port, uvicorn_port):
            sock, send, recv = _ws_raw("127.0.0.1", port, "/auth?token=nope")
            try:
                kind, val = recv()
                assert kind == "close", (port, kind, val)
            finally:
                sock.close()
            sock, send, recv = _ws_raw("127.0.0.1", port, "/auth?token=s3cr3t")
            try:
                assert recv() == ("text", "authed"), port
            finally:
                sock.close()
    finally:
        _stop(ph)
        _stop(pu)
