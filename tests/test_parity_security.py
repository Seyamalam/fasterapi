"""Differential parity: auth schemes, background tasks, encoders, edge bodies.

Same endpoints on FastAPI vs fasterapi; asserts identical wire behavior
(status, headers, JSON bodies, 422 shapes) for the security + background +
encoding surface.
"""

from __future__ import annotations

import datetime
import enum
import uuid

import pytest
from pydantic import BaseModel

import fasterapi as F
from fasterapi import Depends as FDepends
from fasterapi import Security as FSecurity
from fasterapi.background import BackgroundTasks as FBackgroundTasks
from fasterapi.security import (
    APIKeyHeader as FAPIKeyHeader,
    HTTPBasic as FHTTPBasic,
    HTTPBearer as FHTTPBearer,
    OAuth2PasswordBearer as FOAuth2PasswordBearer,
    OAuth2PasswordRequestForm as FOAuth2PasswordRequestForm,
    OpenIdConnect as FOpenIdConnect,
)

from fastapi import Depends as GDepends
from fastapi import Security as GSecurity
from fastapi import FastAPI as GFastAPI
from fastapi.background import BackgroundTasks as GBackgroundTasks
from fastapi.security import (
    APIKeyHeader as GAPIKeyHeader,
    HTTPBasic as GHTTPBasic,
    HTTPBearer as GHTTPBearer,
    OAuth2PasswordBearer as GOAuth2PasswordBearer,
    OAuth2PasswordRequestForm as GOAuth2PasswordRequestForm,
    OpenIdConnect as GOpenIdConnect,
)
from fastapi.testclient import TestClient as GTestClient


class Color(enum.Enum):
    RED = "red"


class Stamp(BaseModel):
    at: datetime.datetime
    uid: uuid.UUID
    color: Color


class _Pair(BaseModel):
    a: int
    b: int


def build_fastapi_app(side_effects: list[str]) -> GFastAPI:
    app = GFastAPI()
    oauth = GOAuth2PasswordBearer(tokenUrl="token")
    bearer = GHTTPBearer()
    basic = GHTTPBasic()
    key = GAPIKeyHeader(name="x-key")
    oidc = GOpenIdConnect(openIdConnectUrl="https://ex.example/.well-known")

    @app.post("/token")
    async def token(form: GOAuth2PasswordRequestForm = GDepends()):
        return {"access_token": form.username, "token_type": "bearer", "scopes": form.scopes}

    @app.get("/me")
    async def me(t: str = GDepends(oauth)):
        return {"token": t}

    @app.get("/scoped")
    async def scoped(t: str = GSecurity(oauth, scopes=["read"])):
        return {"token": t}

    @app.get("/bearer")
    async def bearer_route(c= GDepends(bearer)):
        return {"scheme": c.scheme, "credentials": c.credentials}

    @app.get("/basic")
    async def basic_route(c= GDepends(basic)):
        return {"username": c.username}

    @app.get("/key")
    async def key_route(k: str = GDepends(key)):
        return {"key": k}

    @app.get("/oidc")
    async def oidc_route(t: str = GDepends(oidc)):
        return {"token": t}

    @app.post("/tasks")
    async def tasks(bg: GBackgroundTasks):
        bg.add_task(side_effects.append, "done")
        return {"queued": True}

    @app.get("/stamp")
    async def stamp():
        return Stamp(at=datetime.datetime(2026, 1, 2, 3, 4, 5), uid=uuid.UUID(int=1), color=Color.RED)

    return app


def build_fasterapi_app(side_effects: list[str]) -> F.FastAPI:
    app = F.FastAPI()
    oauth = FOAuth2PasswordBearer(tokenUrl="token")
    bearer = FHTTPBearer()
    basic = FHTTPBasic()
    key = FAPIKeyHeader(name="x-key")
    oidc = FOpenIdConnect(openIdConnectUrl="https://ex.example/.well-known")

    @app.post("/token")
    async def token(form: FOAuth2PasswordRequestForm = FDepends()):
        return {"access_token": form.username, "token_type": "bearer", "scopes": form.scopes}

    @app.get("/me")
    async def me(t: str = FDepends(oauth)):
        return {"token": t}

    @app.get("/scoped")
    async def scoped(t: str = FSecurity(oauth, scopes=["read"])):
        return {"token": t}

    @app.get("/bearer")
    async def bearer_route(c= FDepends(bearer)):
        return {"scheme": c.scheme, "credentials": c.credentials}

    @app.get("/basic")
    async def basic_route(c= FDepends(basic)):
        return {"username": c.username}

    @app.get("/key")
    async def key_route(k: str = FDepends(key)):
        return {"key": k}

    @app.get("/oidc")
    async def oidc_route(t: str = FDepends(oidc)):
        return {"token": t}

    @app.post("/tasks")
    async def tasks(bg: FBackgroundTasks):
        bg.add_task(side_effects.append, "done")
        return {"queued": True}

    @app.get("/stamp")
    async def stamp():
        return Stamp(at=datetime.datetime(2026, 1, 2, 3, 4, 5), uid=uuid.UUID(int=1), color=Color.RED)

    return app


@pytest.fixture()
def pair():
    g_fx: list[str] = []
    f_fx: list[str] = []
    with GTestClient(build_fastapi_app(g_fx)) as gc, F.TestClient(build_fasterapi_app(f_fx)) as fc:
        yield gc, fc, g_fx, f_fx


def _unauth_shape(resp):  # type: ignore[no-untyped-def]
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}
    assert "www-authenticate" in resp.headers


def test_oauth_missing_and_wrong_scheme(pair):
    gc, fc, _, _ = pair
    for path in ["/me", "/scoped", "/bearer", "/key", "/oidc"]:
        _unauth_shape(gc.get(path))
        _unauth_shape(fc.get(path))
    assert gc.get("/me").headers["www-authenticate"] == fc.get("/me").headers["www-authenticate"] == "Bearer"
    assert gc.get("/bearer").headers["www-authenticate"] == fc.get("/bearer").headers["www-authenticate"] == "Bearer"
    assert gc.get("/key").headers["www-authenticate"] == fc.get("/key").headers["www-authenticate"] == "APIKey"
    # Wrong scheme on OAuth2 route
    a = gc.get("/me", headers={"Authorization": "Basic abc"})
    b = fc.get("/me", headers={"Authorization": "Basic abc"})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, {"detail": "Not authenticated"})


def test_oauth_bearer_ok(pair):
    gc, fc, _, _ = pair
    assert gc.get("/me", headers={"Authorization": "Bearer abc"}).json() == \
        fc.get("/me", headers={"Authorization": "Bearer abc"}).json() == {"token": "abc"}
    assert gc.get("/scoped", headers={"Authorization": "Bearer abc"}).json() == \
        fc.get("/scoped", headers={"Authorization": "Bearer abc"}).json() == {"token": "abc"}
    a = gc.get("/bearer", headers={"Authorization": "Bearer abc"})
    b = fc.get("/bearer", headers={"Authorization": "Bearer abc"})
    assert a.json() == b.json() == {"scheme": "Bearer", "credentials": "abc"}
    assert gc.get("/oidc", headers={"Authorization": "Bearer z"}).json() == \
        fc.get("/oidc", headers={"Authorization": "Bearer z"}).json() == {"token": "Bearer z"}


def test_basic_and_apikey(pair):
    import base64

    gc, fc, _, _ = pair
    cred = base64.b64encode(b"ada:secret").decode()
    a = gc.get("/basic", headers={"Authorization": f"Basic {cred}"})
    b = fc.get("/basic", headers={"Authorization": f"Basic {cred}"})
    assert a.json() == b.json() == {"username": "ada"}
    # Bad base64 -> 401 both
    a = gc.get("/basic", headers={"Authorization": "Basic !!!"})
    b = fc.get("/basic", headers={"Authorization": "Basic !!!"})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, {"detail": "Not authenticated"})
    a = gc.get("/key", headers={"x-key": "k1"})
    b = fc.get("/key", headers={"x-key": "k1"})
    assert a.json() == b.json() == {"key": "k1"}


def test_token_form_variants(pair):
    gc, fc, _, _ = pair
    good = {"username": "ada", "password": "pw", "scope": "read write"}
    a = gc.post("/token", data=good)
    b = fc.post("/token", data=good)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"access_token": "ada", "token_type": "bearer", "scopes": ["read", "write"]}
    # Missing grant_type (permissive) + explicit grant_type both behave the same
    for data in [{"username": "ada", "password": "pw"},
                 {"grant_type": "password", "username": "ada", "password": "pw"},
                 {"grant_type": "other", "username": "ada", "password": "pw"}]:
        a = gc.post("/token", data=data)
        b = fc.post("/token", data=data)
        assert a.status_code == b.status_code, data
        assert a.json() == b.json(), data


def test_background_tasks(pair):
    gc, fc, g_fx, f_fx = pair
    assert gc.post("/tasks").json() == fc.post("/tasks").json() == {"queued": True}
    assert g_fx == f_fx == ["done"]


def test_encoder_types(pair):
    gc, fc, _, _ = pair
    a = gc.get("/stamp")
    b = fc.get("/stamp")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"at": "2026-01-02T03:04:05", "uid": "00000000-0000-0000-0000-000000000001",
                                    "color": "red"}


def test_multi_error_and_malformed_json():
    from fastapi import FastAPI as _G

    g = _G()

    @g.post("/m")
    async def _gm(payload: _Pair):
        return payload

    f = F.FastAPI()

    @f.post("/m")
    async def _fm(payload: _Pair):
        return payload

    with GTestClient(g) as gc, F.TestClient(f) as fc:
        a = gc.post("/m", json={})
        b = fc.post("/m", json={})
        assert a.status_code == b.status_code == 422
        assert [(e["loc"], e["type"]) for e in a.json()["detail"]] == \
            [(e["loc"], e["type"]) for e in b.json()["detail"]]
        assert len(b.json()["detail"]) == 2
        # Malformed JSON mirrors FastAPI's json_invalid shape
        a = gc.post("/m", content=b"not json", headers={"content-type": "application/json"})
        b = fc.post("/m", content=b"not json", headers={"content-type": "application/json"})
        assert a.status_code == b.status_code == 422
        assert a.json() == b.json()
