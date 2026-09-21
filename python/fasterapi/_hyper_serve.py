"""fasterapi._hyper_serve — Python side of the Hyper ASGI host.

Rust (`serve_asgi`) owns sockets + HTTP parsing and calls in here:

* ``run_lifespan(app, kind)`` — drives Starlette lifespan around serving,
* ``run_request(...)`` — drives ``app(scope, receive, send)`` for one
  request on a thread-local asyncio loop, returns ``(status, headers, body)``,
* ``ws_open`` / ``ws_step`` / ``ws_close`` — websocket sessions: the ASGI
  app runs on a per-session loop, Rust does framing + the 101 handshake.

Same app object, same middleware, same state: bytes stay identical to uvicorn.
Spike limits (see ``src/lib.rs``): HTTP/1.1, buffered bodies, concatenated
streaming, no mid-response disconnect polling.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from typing import Any, Optional
from urllib.parse import unquote

_LIFESPAN_CM: Any = None
_LIFESPAN_LOOP: Any = None
_tls = threading.local()

_WS_SESSIONS: dict[str, dict[str, Any]] = {}
_WS_LOCK = threading.Lock()
_WS_TIMEOUT = 30.0


def _direct_ok(app: Any) -> bool:
    """Direct dispatch is valid only with zero user middleware.

    Skipping Starlette's ServerError/Exception wrappers + router loop is
    byte-identical only when no user middleware could observe or alter the
    exchange. The check is a tiny isinstance scan (no cache → always fresh
    if the app gains middleware later).
    """
    try:
        from .middleware.asyncexitstack import AsyncExitStackMiddleware

        middlewares = getattr(app, "user_middleware", None) or []
        return all(entry[0] is AsyncExitStackMiddleware for entry in middlewares)
    except Exception:
        return False


def _get_loop() -> asyncio.AbstractEventLoop:
    """One asyncio loop per worker thread, reused across requests.

    ``asyncio.run()`` per request costs a fresh loop each time (~tens of µs);
    Rust's blocking pool reuses threads, so a thread-local loop amortizes
    that to zero after the first request on each thread.
    """
    loop = getattr(_tls, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _tls.loop = loop
    return loop


def run_lifespan(app: Any, kind: str) -> None:
    """Run Starlette lifespan startup/shutdown around the Rust server.

    One persistent loop for the whole serve lifetime: ``asyncio.run()`` per
    call would close its loop and finalize the suspended lifespan generator,
    making shutdown unreachable. Both calls happen on the server thread.
    """
    global _LIFESPAN_CM, _LIFESPAN_LOOP
    if kind == "startup":
        loop = asyncio.new_event_loop()
        cm = app.router.lifespan_context(app)
        loop.run_until_complete(cm.__aenter__())
        _LIFESPAN_LOOP, _LIFESPAN_CM = loop, cm
    else:
        loop, cm = _LIFESPAN_LOOP, _LIFESPAN_CM
        _LIFESPAN_LOOP, _LIFESPAN_CM = None, None
        if loop is not None and cm is not None:
            try:
                loop.run_until_complete(cm.__aexit__(None, None, None))
            finally:
                loop.close()


def run_request(
    app: Any,
    method: str,
    path: str,
    query_string: str,
    headers: list[tuple[str, bytes]],
    body: bytes,
    sendfile_ok: bool = True,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Drive one ASGI request to completion; return ``(status, headers, body)``."""
    raw_headers = [(name.lower().encode("latin-1"), bytes(value)) for name, value in headers]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": unquote(path),
        "raw_path": path.encode("ascii", "replace"),
        "query_string": query_string.encode("ascii", "replace"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fasterapi-hyper", 0),
        "extensions": {},
    }
    pending: list[dict[str, Any]] = [{"type": "http.request", "body": bytes(body), "more_body": False}]
    sent: dict[str, Any] = {}

    async def receive() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        mtype = message.get("type")
        if mtype == "http.response.start":
            sent["status"] = int(message.get("status", 500))
            sent["headers"] = [
                (k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])
            ]
        elif mtype == "http.response.body":
            sent.setdefault("chunks", []).append(bytes(message.get("body", b"")))

    async def _go() -> None:
        if _direct_ok(app):
            # Fast lane: match via Starlette's own compiled matchers, then run
            # the matched route app directly — no ServerError/Exception
            # wrappers, no router loop. Anything unmatched (404/405/redirects)
            # falls through to the full stack for exact handling.
            from starlette.routing import Match

            router = app.router
            for route in router.routes:
                matches = getattr(route, "matches", None)
                if matches is None:
                    continue
                try:
                    match, child_scope = matches(scope)
                except Exception:
                    continue
                if match == Match.FULL:
                    scope.update(child_scope)
                    endpoint = getattr(route, "endpoint", None)
                    if endpoint is not None:
                        scope["endpoint"] = endpoint
                    await route.app(scope, receive, send)
                    return
        await app(scope, receive, send)

    try:
        _get_loop().run_until_complete(_go())
    except Exception:
        traceback.print_exc()
        raise
    return sent.get("status", 500), sent.get("headers", []), b"".join(sent.get("chunks", []))


def hyper_available() -> bool:
    try:
        from fasterapi._fasterapi_rs import serve_asgi  # type: ignore  # noqa: F401
    except ImportError:
        try:
            from _fasterapi_rs import serve_asgi  # type: ignore  # noqa: F401
        except ImportError:
            return False
    return True


# ------------------------------------------------------- websocket bridge ---

def _ws_accept_key(key: str) -> str:
    import base64 as _b64
    import hashlib as _hl

    return _b64.b64encode(_hl.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


def ws_open(app: Any, path: str, query_string: str, headers: list[tuple[str, bytes]],
            ws_key: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Start a websocket session; drive the ASGI app until accept/refusal.

    Returns ``(session_id, status, data)`` where status is ``"accepted"``
    (data = ``[(accept_key, "")]`` + response headers), ``"refused"``
    (data = ``[(status, detail)]``) or ``"error"``.
    """
    import secrets as _secrets
    from urllib.parse import unquote as _unquote

    raw_headers = [(name.lower().encode("latin-1"), bytes(value)) for name, value in headers]
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "ws",
        "path": _unquote(path),
        "raw_path": path.encode("ascii", "replace"),
        "query_string": query_string.encode("ascii", "replace"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fasterapi-hyper", 0),
        "subprotocols": [],
        "extensions": {"websocket.open": {}},
    }
    loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    connected = {"done": False}

    async def receive() -> dict[str, Any]:
        if not connected["done"]:
            connected["done"] = True
            return {"type": "websocket.connect"}
        return await in_q.get()

    async def send(message: dict[str, Any]) -> None:
        await out_q.put(message)

    async def _runner() -> None:
        try:
            await app(scope, receive, send)
        except Exception:
            traceback.print_exc()
        finally:
            await out_q.put({"type": "websocket.done"})

    def _thread_main() -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(_runner())
        with _WS_LOCK:
            entry = _WS_SESSIONS.get(sid)
            if entry is not None:
                entry["task"] = task
        try:
            loop.run_forever()
        finally:
            task.cancel()
            try:
                loop.run_until_complete(task)
            except Exception:
                pass
            loop.close()

    sid = _secrets.token_hex(16)
    with _WS_LOCK:
        # Registered BEFORE the thread starts: _thread_main reads it.
        _WS_SESSIONS[sid] = {"loop": loop, "thread": None, "task": None, "in": in_q, "out": out_q}
    thread = threading.Thread(target=_thread_main, name="fasterapi-ws", daemon=True)
    with _WS_LOCK:
        _WS_SESSIONS[sid]["thread"] = thread
    thread.start()
    # Wait for the app's first verdict (accept / close / crash). Anything
    # batched behind the accept (e.g. an immediate close) is re-queued so
    # the pump still delivers it.
    first = _ws_poll(sid, timeout=10.0)
    accept = None
    rest: list[dict[str, Any]] = []
    for message in first:
        if message.get("type") == "websocket.accept" and accept is None:
            accept = message
        else:
            rest.append(message)
    if accept is not None:
        if rest:
            with _WS_LOCK:
                session = _WS_SESSIONS.get(sid)
            if session is not None:
                loop = session["loop"]
                out_q = session["out"]

                def _requeue() -> None:
                    for m in rest:
                        out_q.put_nowait(m)

                try:
                    loop.call_soon_threadsafe(_requeue)
                except Exception:
                    pass
        accept_headers = [(k.decode("latin-1"), v.decode("latin-1"))
                          for k, v in accept.get("headers", [])]
        return sid, "accepted", [(_ws_accept_key(ws_key), "")] + accept_headers
    for message in first:
        if message.get("type") == "websocket.close":
            ws_close(sid)
            return "", "refused", [(str(message.get("code", 1000)), str(message.get("reason", "")))]
    ws_close(sid)
    return "", "error", [("500", "websocket app did not accept")]


def _ws_poll(sid: str, timeout: float) -> list[dict[str, Any]]:
    """Drain one batch of app outputs (blocking, called from Rust workers).

    Settle logic kills the drain race: after the first message, if the app
    task already finished, everything it sent is queued so drain until empty
    with no extra wait; otherwise allow one short grace window for a
    back-to-back send (e.g. echo immediately followed by close).
    """
    with _WS_LOCK:
        session = _WS_SESSIONS.get(sid)
    if session is None:
        return [{"type": "websocket.done"}]
    loop = session["loop"]
    out_q = session["out"]

    def _task_done() -> bool:
        task = session.get("task")
        try:
            return task is not None and bool(task.done())
        except Exception:
            return False

    async def _drain() -> list[dict[str, Any]]:
        messages = []
        try:
            first = await asyncio.wait_for(out_q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return messages
        messages.append(first)
        if _task_done():
            while True:
                try:
                    messages.append(out_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            return messages
        for _ in range(3):
            try:
                messages.append(await asyncio.wait_for(out_q.get(), timeout=0.05))
            except asyncio.TimeoutError:
                break
            if _task_done():
                while True:
                    try:
                        messages.append(out_q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                break
        return messages

    future = asyncio.run_coroutine_threadsafe(_drain(), loop)
    try:
        return future.result(timeout=timeout + 5.0)
    except Exception:
        return [{"type": "websocket.done"}]


def ws_step(sid: str, opcode: Optional[int], payload: bytes, wait_secs: float = 5.0) -> list[tuple[int, bytes]]:
    """Deliver one client frame (opcode None = poll) and return server frames.

    Returned tuples are ``(opcode, payload)`` with opcode 0x8 carrying a
    2-byte close code prefix for close frames. An empty list means "nothing
    yet" (Rust keeps polling); a ``websocket.done`` becomes ``[]`` and the
    Rust side closes the session afterwards.
    """
    with _WS_LOCK:
        session = _WS_SESSIONS.get(sid)
    if session is None:
        return [(0x8, b"\x03\xe8")]
    if opcode is not None:
        message: dict[str, Any]
        if opcode == 0x1:
            try:
                text = bytes(payload).decode("utf-8")
            except UnicodeDecodeError:
                return [(0x8, b"\x03\xef")]
            message = {"type": "websocket.receive", "text": text}
        elif opcode == 0x2:
            message = {"type": "websocket.receive", "bytes": bytes(payload)}
        elif opcode == 0x8:
            message = {"type": "websocket.disconnect", "code": 1000, "reason": ""}
        else:
            return []
        loop = session["loop"]
        in_q = session["in"]
        future = asyncio.run_coroutine_threadsafe(in_q.put(message), loop)
        try:
            future.result(timeout=10.0)
        except Exception:
            return [(0x8, b"\x03\xe8")]
    frames: list[tuple[int, bytes]] = []
    done = False
    for message in _ws_poll(sid, timeout=wait_secs):
        mtype = message.get("type")
        if mtype == "websocket.send":
            if "text" in message and message["text"] is not None:
                frames.append((0x1, str(message["text"]).encode("utf-8")))
            elif "bytes" in message and message["bytes"] is not None:
                frames.append((0x2, bytes(message["bytes"])))
        elif mtype == "websocket.close":
            code = int(message.get("code", 1000))
            frames.append((0x8, code.to_bytes(2, "big") + str(message.get("reason", "")).encode("utf-8", "replace")))
            done = True
        elif mtype == "websocket.done":
            done = True
    if done:
        ws_close(sid)
    return frames


def ws_close(sid: str) -> None:
    with _WS_LOCK:
        session = _WS_SESSIONS.pop(sid, None)
    if session is None:
        return
    loop = session["loop"]
    thread = session["thread"]
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    thread.join(timeout=5.0)
    try:
        loop.close()
    except Exception:
        pass
