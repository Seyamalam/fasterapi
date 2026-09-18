"""fasterapi.openapi.utils — spec generation (1:1 ``fastapi.openapi.utils`` subset).

``get_openapi()`` builds the OpenAPI document from route definitions with the
same structure FastAPI emits: operationIds, typed parameters (constraints,
defaults, titles), ``$ref`` request/response bodies, component schemas
(including nested ``$defs``), and the ``HTTPValidationError``/``ValidationError``
422 contract. Accepts our ``_RouteDef`` objects or plain Starlette routes.
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from pydantic import BaseModel, TypeAdapter


def _is_model(tp: Any) -> bool:
    try:
        return inspect.isclass(tp) and issubclass(tp, BaseModel)
    except Exception:
        return False


_VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "properties": {
        "loc": {
            "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "type": "array",
            "title": "Location",
        },
        "msg": {"type": "string", "title": "Message"},
        "type": {"type": "string", "title": "Error Type"},
        "input": {"title": "Input"},
        "ctx": {"type": "object", "title": "Context"},
    },
    "type": "object",
    "required": ["loc", "msg", "type"],
    "title": "ValidationError",
}

_HTTP_VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "properties": {
        "detail": {
            "items": {"$ref": "#/components/schemas/ValidationError"},
            "type": "array",
            "title": "Detail",
        }
    },
    "type": "object",
    "title": "HTTPValidationError",
}


def _title(name: str) -> str:
    return name.replace("_", " ").title()


def _param_schema(ann: Any, meta: Any = None) -> dict[str, Any]:
    """JSON schema for one scalar param, constraints included."""
    if ann is inspect.Parameter.empty or ann is Any:
        schema: dict[str, Any] = {"type": "string"}
    else:
        try:
            schema = TypeAdapter(ann).json_schema()
        except Exception:
            schema = {"type": "string"}
    if meta is not None:
        for attr, key in (("ge", "minimum"), ("le", "maximum"), ("gt", "exclusiveMinimum"),
                          ("lt", "exclusiveMaximum"), ("min_length", "minLength"),
                          ("max_length", "maxLength"), ("pattern", "pattern")):
            val = getattr(meta, attr, None)
            if val is not None:
                schema[key] = val
    return schema


def _register_model(model: Any, components: dict[str, Any]) -> str:
    """Add a model (+ nested $defs) to components; return its $ref."""
    name = model.__name__
    try:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    except Exception:
        schema = {"type": "object", "title": name}
    defs = schema.pop("$defs", {})
    # FastAPI strips null defaults from model schemas; match it.
    _strip_null_defaults(schema)
    components.setdefault(name, schema)
    for dname, dsch in defs.items():
        if isinstance(dsch, dict):
            _strip_null_defaults(dsch)
        components.setdefault(dname, dsch)
    return f"#/components/schemas/{name}"


def _strip_null_defaults(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("default") is None and "default" in node:
            del node["default"]
        for value in node.values():
            _strip_null_defaults(value)
    elif isinstance(node, list):
        for value in node:
            _strip_null_defaults(value)


def _scheme_name(scheme: Any) -> str:
    return getattr(scheme, "scheme_name", None) or type(scheme).__name__


def _scheme_json(scheme: Any) -> dict[str, Any]:
    """OpenAPI security-scheme object mirroring FastAPI's emission."""
    from ..security import APIKeyBase, HTTPBase, OAuth2, OpenIdConnect

    out: dict[str, Any] = {}
    if isinstance(scheme, OAuth2):
        flows = {}
        for flow_name, flow in (getattr(scheme, "flows", None) or {}).items():
            flows[flow_name] = {k: v for k, v in flow.items() if v is not None}
        out = {"type": "oauth2", "flows": flows}
    elif isinstance(scheme, APIKeyBase):
        out = {"type": "apiKey", "in": scheme.openapi_in, "name": scheme.name}
    elif isinstance(scheme, HTTPBase):
        out = {"type": "http", "scheme": (scheme.scheme or "").lower()}
        if getattr(scheme, "bearerFormat", None):
            out["bearerFormat"] = scheme.bearerFormat
    elif isinstance(scheme, OpenIdConnect):
        out = {"type": "openIdConnect", "openIdConnectUrl": scheme.openIdConnectUrl}
    else:  # pragma: no cover - unknown custom scheme
        out = {"type": "http", "scheme": "bearer"}
    if getattr(scheme, "description", None):
        out["description"] = scheme.description
    return out


def _route_security(route: Any, endpoint: Any, security_schemes: dict[str, Any]) -> list[dict[str, list[str]]]:
    """Collect (scheme, scopes) pairs from endpoint + route dependencies."""
    from ..params import Depends, Security
    from ..routing import _signature
    from ..security import SecurityBase

    found: list[tuple[Any, list[str]]] = []

    def _scan(marker: Any) -> None:
        dep = getattr(marker, "dependency", None)
        if isinstance(dep, SecurityBase):
            scopes = list(getattr(marker, "scopes", None) or [])
            found.append((dep, scopes))

    for dep in list(getattr(route, "dependencies", None) or []):
        _scan(dep)
    try:
        sig = _signature(endpoint) or inspect.signature(endpoint)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        for param in sig.parameters.values():
            if isinstance(param.default, (Depends, Security)):
                _scan(param.default)
    requirements: list[dict[str, list[str]]] = []
    for scheme, scopes in found:
        name = _scheme_name(scheme)
        security_schemes.setdefault(name, _scheme_json(scheme))
        requirements.append({name: scopes})
    return requirements


def get_openapi(
    *,
    title: str,
    version: str,
    openapi_version: str = "3.1.0",
    description: str = "",
    routes: list[Any],
    openapi_prefix: str = "",
    servers: Optional[list[dict[str, Any]]] = None,
    **_extra: Any,
) -> dict[str, Any]:
    from ..params import Depends, Param
    from ..routing import _ann_of, _is_pydantic_model, _signature

    paths: dict[str, Any] = {}
    components: dict[str, Any] = {}
    security_schemes: dict[str, Any] = {}
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        methods = [m.lower() for m in (getattr(route, "methods", None) or ["get"])]
        rpath = openapi_prefix + getattr(route, "path", "/")
        item = paths.setdefault(rpath, {})
        try:
            sig = inspect.signature(endpoint)
        except (TypeError, ValueError):
            continue
        parameters: list[dict[str, Any]] = []
        body_model: Any = None
        body_required = False
        has_validation = False
        for pname, param in sig.parameters.items():
            ann = _ann_of(endpoint, pname, param.annotation)
            default = param.default
            if isinstance(default, Depends):
                continue
            dname = getattr(ann, "__name__", "")
            if dname in ("Request", "HTTPConnection", "BackgroundTasks", "SecurityScopes") \
                    or pname in ("request", "background_tasks"):
                continue
            if isinstance(default, Param):
                if default.in_ in ("query", "path", "header", "cookie"):
                    has_validation = True
                    alias = default.alias or pname
                    schema = _param_schema(ann, default)
                    schema.setdefault("title", _title(alias))
                    entry: dict[str, Any] = {
                        "name": alias, "in": default.in_,
                        "required": default.default is Ellipsis,
                        "schema": schema,
                    }
                    if default.description:
                        entry["description"] = default.description
                    # FastAPI omits null defaults from the schema.
                    if default.has_default() and default.default is not Ellipsis \
                            and default.default is not None:
                        entry["schema"]["default"] = default.default
                    parameters.append(entry)
                elif default.in_ == "body":
                    has_validation = True
                    if _is_pydantic_model(ann) and not default.embed:
                        body_model, body_required = ann, True
                continue
            if _is_pydantic_model(ann):
                has_validation = True
                body_model, body_required = ann, True
                continue
            if pname in rpath:
                has_validation = True
                schema = _param_schema(ann)
                schema.setdefault("title", _title(pname))
                parameters.append({"name": pname, "in": "path", "required": True, "schema": schema})
                continue
            if default is not inspect.Parameter.empty:
                has_validation = True
                schema = _param_schema(ann)
                schema.setdefault("title", _title(pname))
                entry = {"name": pname, "in": "query", "required": False, "schema": schema}
                if default is not None:
                    entry["schema"]["default"] = default
                parameters.append(entry)
                continue
            # Required bare param -> required query (FastAPI behavior).
            has_validation = True
            schema = _param_schema(ann)
            schema.setdefault("title", _title(pname))
            parameters.append({"name": pname, "in": "query", "required": True, "schema": schema})

        op: dict[str, Any] = {
            "summary": getattr(route, "summary", None) or getattr(endpoint, "__name__", rpath).replace("_", " ").title(),
            "operationId": _operation_id(route, endpoint, methods[0]),
        }
        op_description = getattr(route, "description", None) or inspect.getdoc(endpoint)
        if op_description:
            op["description"] = op_description
        if getattr(route, "deprecated", False):
            op["deprecated"] = True
        if getattr(route, "tags", None):
            op["tags"] = list(route.tags)
        if parameters:
            op["parameters"] = parameters
        if body_model is not None:
            ref = _register_model(body_model, components)
            op["requestBody"] = {
                "content": {"application/json": {"schema": {"$ref": ref}}},
                "required": body_required,
            }
        resp_model = getattr(route, "response_model", None)
        status = str(getattr(route, "status_code", 200))
        ok: dict[str, Any] = {"description": "Successful Response"}
        if _is_model(resp_model):
            ok["content"] = {"application/json": {"schema": {"$ref": _register_model(resp_model, components)}}}
        else:
            ok["content"] = {"application/json": {"schema": {}}}
        responses: dict[str, Any] = {status: ok}
        if has_validation:
            components.setdefault("ValidationError", dict(_VALIDATION_ERROR_SCHEMA))
            components.setdefault("HTTPValidationError", dict(_HTTP_VALIDATION_ERROR_SCHEMA))
            responses["422"] = {
                "description": "Validation Error",
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}},
            }
        op["responses"] = responses
        # Security: schemes referenced by Depends()/Security() on the endpoint
        # or the route produce components + per-operation requirements.
        op_security = _route_security(route, endpoint, security_schemes)
        if op_security:
            op["security"] = op_security
        for m in methods:
            item[m] = op
    doc: dict[str, Any] = {
        "openapi": openapi_version,
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    if description:
        doc["info"]["description"] = description
    if servers:
        doc["servers"] = servers
    if components or security_schemes:
        doc["components"] = {}
        if components:
            doc["components"]["schemas"] = components
        if security_schemes:
            doc["components"]["securitySchemes"] = security_schemes
    return doc


def _operation_id(route: Any, endpoint: Any, method: str) -> str:
    import re as _re

    name = getattr(route, "name", None) or getattr(endpoint, "__name__", "route")
    path = getattr(route, "path_format", None) or getattr(route, "path", "")
    op_id = f"{name}{path}"
    op_id = _re.sub(r"\W", "_", op_id)
    return f"{op_id}_{method.lower()}"


def get_openapi_path(  # pragma: no cover - convenience alias
    **kwargs: Any,
) -> dict[str, Any]:
    return get_openapi(**kwargs)
