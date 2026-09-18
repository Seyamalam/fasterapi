"""Export-parity guard: everything importable from FastAPI must be importable
from fasterapi the same way. Fails the suite the moment the surface drifts."""

from __future__ import annotations

import importlib


def _resolve(package: str, name: str):  # type: ignore[no-untyped-def]
    pkg = importlib.import_module(package)
    obj = getattr(pkg, name, None)
    if obj is None:
        obj = importlib.import_module(f"{package}.{name}")
    return obj


def test_top_level_export_parity():
    import fastapi

    missing = []
    for name in dir(fastapi):
        if name.startswith("_"):
            continue
        try:
            _resolve("fasterapi", name)
        except Exception:
            missing.append(name)
    assert missing == []


def test_key_from_imports():
    checks = [
        ("fasterapi", "FastAPI"), ("fasterapi", "APIRouter"),
        ("fasterapi", "Depends"), ("fasterapi", "Security"),
        ("fasterapi", "BackgroundTasks"), ("fasterapi", "UploadFile"),
        ("fasterapi", "HTTPException"), ("fasterapi", "status"),
        ("fasterapi.security", "OAuth2PasswordBearer"),
        ("fasterapi.security", "OAuth2PasswordRequestForm"),
        ("fasterapi.security", "OAuth2AuthorizationCodeBearer"),
        ("fasterapi.security", "OpenIdConnect"),
        ("fasterapi.security", "HTTPBearer"), ("fasterapi.security", "HTTPBasic"),
        ("fasterapi.security", "HTTPDigest"),
        ("fasterapi.security", "APIKeyHeader"), ("fasterapi.security", "APIKeyQuery"),
        ("fasterapi.security", "APIKeyCookie"), ("fasterapi.security", "SecurityScopes"),
        ("fasterapi.security", "HTTPBasicCredentials"),
        ("fasterapi.security", "HTTPAuthorizationCredentials"),
        ("fasterapi.sse", "EventSourceResponse"), ("fasterapi.sse", "ServerSentEvent"),
        ("fasterapi.sse", "format_sse_event"),
        ("fasterapi.responses", "EventSourceResponse"),
        ("fasterapi.responses", "ORJSONResponse"), ("fasterapi.responses", "UJSONResponse"),
        ("fasterapi.openapi.docs", "get_swagger_ui_html"),
        ("fasterapi.openapi.docs", "get_redoc_html"),
        ("fasterapi.openapi.docs", "get_swagger_ui_oauth2_redirect_html"),
        ("fasterapi.encoders", "jsonable_encoder"),
        ("fasterapi.middleware.cors", "CORSMiddleware"),
        ("fasterapi.utils", "generate_unique_id"),
        ("fasterapi concurrency".replace(" ", "."), "run_in_threadpool"),
    ]
    for mod, name in checks:
        assert getattr(importlib.import_module(mod), name, None) is not None, f"{mod}.{name}"
