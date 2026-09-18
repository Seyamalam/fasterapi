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


# Curated user-meaningful API names (audited against installed FastAPI;
# excludes typing machinery, Pydantic-v1 compat, openapi Spec models,
# routing-engine internals and dependencies.* internals by decision).
CURATED_API = [
    # top-level app surface
    ("", "FastAPI"), ("", "APIRouter"), ("", "Depends"), ("", "Security"),
    ("", "BackgroundTasks"), ("", "UploadFile"), ("", "HTTPException"),
    ("", "status"), ("", "middleware"), ("", "openapi"),
    # params
    ("params", "Query"), ("params", "Path"), ("params", "Body"),
    ("params", "Header"), ("params", "Cookie"), ("params", "Form"),
    ("params", "File"), ("params", "Depends"), ("params", "Security"),
    ("params", "Param"), ("params", "ParamTypes"),
    ("param_functions", "Query"), ("param_functions", "Body"),
    ("param_functions", "Security"),
    # requests / responses / websockets
    ("requests", "Request"), ("requests", "HTTPConnection"),
    ("responses", "JSONResponse"), ("responses", "HTMLResponse"),
    ("responses", "PlainTextResponse"), ("responses", "RedirectResponse"),
    ("responses", "StreamingResponse"), ("responses", "FileResponse"),
    ("responses", "EventSourceResponse"), ("responses", "ORJSONResponse"),
    ("responses", "UJSONResponse"), ("responses", "Response"),
    ("websockets", "WebSocket"), ("websockets", "WebSocketDisconnect"),
    # exceptions + handlers
    ("exceptions", "HTTPException"), ("exceptions", "WebSocketException"),
    ("exceptions", "RequestValidationError"),
    ("exceptions", "ResponseValidationError"),
    ("exceptions", "WebSocketRequestValidationError"),
    ("exceptions", "FastAPIError"), ("exceptions", "FastAPIDeprecationWarning"),
    ("exception_handlers", "http_exception_handler"),
    ("exception_handlers", "request_validation_exception_handler"),
    ("exception_handlers", "websocket_request_validation_exception_handler"),
    # security
    ("security", "OAuth2PasswordBearer"),
    ("security", "OAuth2AuthorizationCodeBearer"),
    ("security", "OAuth2PasswordRequestForm"),
    ("security", "OAuth2PasswordRequestFormStrict"),
    ("security", "OpenIdConnect"), ("security", "HTTPBearer"),
    ("security", "HTTPBasic"), ("security", "HTTPDigest"),
    ("security", "HTTPBasicCredentials"),
    ("security", "HTTPAuthorizationCredentials"),
    ("security", "APIKeyHeader"), ("security", "APIKeyQuery"),
    ("security", "APIKeyCookie"), ("security", "SecurityScopes"),
    ("security", "SecurityBase"),
    ("security", "get_authorization_scheme_param"),
    ("security.http", "HTTPBearer"), ("security.oauth2", "OAuth2PasswordBearer"),
    ("security.api_key", "APIKeyHeader"),
    ("security.open_id_connect_url", "OpenIdConnect"),
    ("security.base", "SecurityBase"), ("security.utils", "get_authorization_scheme_param"),
    # sse / background / encoders / concurrency
    ("sse", "EventSourceResponse"), ("sse", "ServerSentEvent"),
    ("sse", "format_sse_event"),
    ("background", "BackgroundTasks"), ("background", "BackgroundTask"),
    ("encoders", "jsonable_encoder"),
    ("concurrency", "run_in_threadpool"), ("concurrency", "iterate_in_threadpool"),
    ("concurrency", "contextmanager_in_threadpool"),
    # datastructures / testclient / middleware paths
    ("datastructures", "UploadFile"), ("datastructures", "State"),
    ("datastructures", "URL"), ("datastructures", "QueryParams"),
    ("datastructures", "FormData"), ("datastructures", "Headers"),
    ("testclient", "TestClient"),
    ("middleware.cors", "CORSMiddleware"), ("middleware.gzip", "GZipMiddleware"),
    ("middleware.trustedhost", "TrustedHostMiddleware"),
    ("middleware.httpsredirect", "HTTPSRedirectMiddleware"),
    ("middleware.asyncexitstack", "AsyncExitStackMiddleware"),
    ("staticfiles", "StaticFiles"), ("templating", "Jinja2Templates"),
    ("logger", "logger"), ("types", "DecoratedCallable"),
    # openapi
    ("openapi.docs", "get_swagger_ui_html"),
    ("openapi.docs", "get_redoc_html"),
    ("openapi.docs", "get_swagger_ui_oauth2_redirect_html"),
    ("openapi.utils", "get_openapi"), ("openapi.utils", "get_openapi_path"),
    ("openapi.utils", "get_openapi_operation_metadata"),
    ("openapi.utils", "generate_operation_summary"),
    ("openapi.constants", "REF_TEMPLATE"),
    # utils / routing / applications user-facing
    ("utils", "generate_unique_id"), ("utils", "deep_dict_update"),
    ("utils", "get_path_param_names"),
    ("utils", "is_body_allowed_for_status_code"),
    ("routing", "APIRouter"), ("routing", "Mount"), ("routing", "URLPath"),
    ("routing", "generate_unique_id"), ("routing", "run_in_threadpool"),
    ("applications", "FastAPI"), ("applications", "get_openapi"),
    ("applications", "generate_unique_id"),
    ("dependencies", "Depends"), ("dependencies", "Security"),
    ("status", "HTTP_200_OK"), ("status", "HTTP_404_NOT_FOUND"),
    ("status", "HTTP_422_UNPROCESSABLE_CONTENT"),
]


def test_curated_api_parity():
    for mod, name in CURATED_API:
        base_g = "fastapi" + ("." + mod if mod else "")
        base_f = "fasterapi" + ("." + mod if mod else "")
        try:
            getattr(importlib.import_module(base_g), name)
        except Exception:
            continue  # not present upstream either — not our burden
        assert getattr(importlib.import_module(base_f), name, None) is not None, (
            f"{base_f}.{name}"
        )
