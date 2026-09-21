"""Differential parity: docs-adjacent surface — SSE, middleware paths, static,
response shims, module imports.

Same app shape on FastAPI vs fasterapi; asserts identical wire behavior.
"""

from __future__ import annotations

import pytest

import fasterapi as F
from fasterapi import Depends as FDepends
from fasterapi.responses import EventSourceResponse as FEventSourceResponse
from fasterapi.sse import ServerSentEvent as FServerSentEvent

from fastapi import Depends as GDepends
from fastapi import FastAPI as GFastAPI
from fastapi.responses import EventSourceResponse as GEventSourceResponse
from fastapi.sse import ServerSentEvent as GServerSentEvent
from fastapi.testclient import TestClient as GTestClient


def build_fastapi_app() -> GFastAPI:
    app = GFastAPI(title="surface")

    @app.get("/events", response_class=GEventSourceResponse)
    async def events():
        yield GServerSentEvent(data={"n": 1}, event="tick", id="1")
        yield {"n": 2}
        yield "hello"

    @app.get("/raw", response_class=GEventSourceResponse)
    async def raw():
        yield GServerSentEvent(raw_data="plain line", comment="keep")

    return app


def build_fasterapi_app() -> F.FastAPI:
    app = F.FastAPI(title="surface")

    @app.get("/events", response_class=FEventSourceResponse)
    async def events():
        yield FServerSentEvent(data={"n": 1}, event="tick", id="1")
        yield {"n": 2}
        yield "hello"

    @app.get("/raw", response_class=FEventSourceResponse)
    async def raw():
        yield FServerSentEvent(raw_data="plain line", comment="keep")

    return app


@pytest.fixture()
def pair():
    with GTestClient(build_fastapi_app()) as gc, F.TestClient(build_fasterapi_app()) as fc:
        yield gc, fc


def test_sse_stream_bytes_identical(pair):
    gc, fc = pair
    for path in ["/events", "/raw"]:
        a = gc.get(path)
        b = fc.get(path)
        assert a.status_code == b.status_code == 200, path
        assert a.headers["content-type"] == b.headers["content-type"], path
        assert a.content == b.content, (path, a.content, b.content)


def test_sse_expected_wire(pair):
    _, fc = pair
    body = fc.get("/events").text
    assert 'event: tick\ndata: {"n": 1}\nid: 1\n\n' in body
    assert 'data: {"n": 2}\n\n' in body
    assert 'data: "hello"\n\n' in body
    raw = fc.get("/raw").text
    assert ": keep\n" in raw and "data: plain line\n\n" in raw


def test_middleware_import_paths():
    from fasterapi.middleware.cors import CORSMiddleware as FCORS
    from fasterapi.middleware.gzip import GZipMiddleware as FGZip
    from fasterapi.middleware.trustedhost import TrustedHostMiddleware as FTrusted
    from fastapi.middleware.cors import CORSMiddleware as GCORS
    from fastapi.middleware.gzip import GZipMiddleware as GZip
    from fastapi.middleware.trustedhost import TrustedHostMiddleware as GTrusted
    assert FCORS is GCORS.__mro__[0] or FCORS.__name__ == "CORSMiddleware"
    assert FGZip.__name__ == "GZipMiddleware" and FTrusted.__name__ == "TrustedHostMiddleware"


def test_cors_behavior_parity():
    from fasterapi.middleware.cors import CORSMiddleware as FCORS
    from fastapi.middleware.cors import CORSMiddleware as GCORS

    g = GFastAPI()
    g.add_middleware(GCORS, allow_origins=["https://x.example"], allow_methods=["GET"])
    f = F.FastAPI()
    f.add_middleware(FCORS, allow_origins=["https://x.example"], allow_methods=["GET"])

    @g.get("/p")
    async def _gp():
        return {"ok": True}

    @f.get("/p")
    async def _fp():
        return {"ok": True}

    with GTestClient(g) as gc, F.TestClient(f) as fc:
        a = gc.get("/p", headers={"Origin": "https://x.example"})
        b = fc.get("/p", headers={"Origin": "https://x.example"})
        assert a.headers.get("access-control-allow-origin") == \
            b.headers.get("access-control-allow-origin") == "https://x.example"
        assert a.json() == b.json()


def test_static_mount_parity(tmp_path):
    (tmp_path / "a.txt").write_text("static-hi")
    from fasterapi.staticfiles import StaticFiles as FStatic
    from fastapi.staticfiles import StaticFiles as GStatic

    g = GFastAPI()
    g.mount("/s", GStatic(directory=str(tmp_path)), name="s")
    f = F.FastAPI()
    f.mount("/s", FStatic(directory=str(tmp_path)), name="s")

    with GTestClient(g) as gc, F.TestClient(f) as fc:
        assert gc.get("/s/a.txt").text == fc.get("/s/a.txt").text == "static-hi"


def test_response_shim_imports():
    from fasterapi.responses import ORJSONResponse, UJSONResponse
    import pytest as _pt

    try:
        import orjson as _orjson  # noqa: F401
        has_orjson = True
    except ImportError:
        has_orjson = False

    if has_orjson:
        # orjson installed: shims must render successfully
        assert ORJSONResponse({"a": 1}).render({"a": 1}) == b'{"a":1}'
    else:
        with _pt.raises(AssertionError, match="orjson must be installed"):
            ORJSONResponse({"a": 1}).render({"a": 1})


def test_module_surface_imports():
    import fasterapi.background
    import fasterapi.concurrency
    import fasterapi.datastructures
    import fasterapi.dependencies
    import fasterapi.encoders
    import fasterapi.exception_handlers
    import fasterapi.logger
    import fasterapi.middleware
    import fasterapi.openapi
    import fasterapi.openapi.docs
    import fasterapi.requests
    import fasterapi.security
    import fasterapi.sse
    import fasterapi.staticfiles
    import fasterapi.status
    import fasterapi.templating
    import fasterapi.types
    import fasterapi.websockets
    from fasterapi import BackgroundTasks, Security, UploadFile
    from fasterapi.status import HTTP_200_OK, HTTP_404_NOT_FOUND
    assert HTTP_200_OK == 200 and HTTP_404_NOT_FOUND == 404
