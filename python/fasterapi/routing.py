"""fasterapi.routing — FastAPI-compatible router.

Implements ``APIRouter`` with ``@get/post/put/delete/patch/options/head``,
``@websocket``, ``include_router`` and ``add_api_route``. Request handling
covers ``Path/Query/Header/Cookie/Body/Form`` + Pydantic v2 validation +
``Depends`` (sub-deps, ``use_cache``, ``yield`` teardown) with error payloads
shaped like FastAPI (``{"detail": [...]}``, 422).
"""

from __future__ import annotations

import inspect
import json as _stdlib_json
import sys
import weakref
from contextlib import AsyncExitStack
from typing import Any, Callable, Coroutine, Optional, Union, get_origin

from pydantic import BaseModel, TypeAdapter
from starlette.background import BackgroundTasks
from starlette.concurrency import iterate_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route as StarletteRoute
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from . import _rust_bridge as _bridge
from .encoders import jsonable_encoder
from .exceptions import HTTPException, RequestValidationError
from .params import Depends, Param
from .security import SecurityScopes

#: Cache key sharing one BackgroundTasks instance across an endpoint and its
#: whole dependency subtree (mirrors FastAPI's per-request instance).
_BG_KEY = "fasterapi:background-tasks"


# ------------------------------------------------------------------ utils ---

def _is_pydantic_model(tp: Any) -> bool:
    try:
        return inspect.isclass(tp) and issubclass(tp, BaseModel)
    except Exception:
        return False


_HINTS_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SIG_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _signature(fn: Callable[..., Any]) -> inspect.Signature | None:
    """Per-process cached signature (hot request path must not re-inspect).

    Weak keys: ``id()``-keyed caches would collide after garbage collection
    and serve one endpoint's signature to another.
    """
    try:
        sig = _SIG_CACHE.get(fn)
    except TypeError:
        sig = None
    if sig is None:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return None
        try:
            _SIG_CACHE[fn] = sig
        except TypeError:
            pass
    return sig


def _resolved_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    """Resolved type hints (handles `from __future__ import annotations`).

    Falls back to raw signature annotations when hints cannot be evaluated
    (e.g. locally-defined models inside test functions).
    """
    try:
        hints = _HINTS_CACHE.get(fn)  # type: ignore[arg-type]
    except TypeError:
        hints = None
    if hints is not None:
        return hints
    try:
        import typing

        hints = typing.get_type_hints(fn, include_extras=True)
        try:
            _HINTS_CACHE[fn] = hints  # type: ignore[index]
        except TypeError:
            pass
        return hints
    except Exception:
        return {}


def _ann_of(fn: Callable[..., Any], pname: str, raw_ann: Any) -> Any:
    if raw_ann is not inspect.Parameter.empty and not isinstance(raw_ann, str):
        return raw_ann
    hints = _resolved_hints(fn)
    if pname in hints:
        return hints[pname]
    if isinstance(raw_ann, str):
        # Last resort: evaluate in the endpoint's module globals (covers
        # `Optional[X]`, `X | None`, `list[X]` written as strings). Callable
        # *instances* (security schemes) have no __globals__, so fall back to
        # their class's module namespace.
        ns = getattr(fn, "__globals__", None)
        if ns is None:
            mod = sys.modules.get(getattr(type(fn), "__module__", ""), None)
            ns = getattr(mod, "__dict__", {})
        try:
            return eval(raw_ann, ns, {})  # noqa: S307
        except Exception:
            pass
    return raw_ann


def _is_request_annotation(ann: Any) -> bool:
    name = getattr(ann, "__name__", str(ann))
    mod = getattr(ann, "__module__", "")
    return name in ("Request", "HTTPConnection") and "starlette" in str(mod)


def _parse_bool(v: str) -> Union[bool, str]:
    low = v.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return v  # let pydantic raise


def _missing(loc: list[str]) -> dict[str, Any]:
    """Exact FastAPI wire shape for a missing field (input is null)."""
    return {"loc": loc, "msg": "Field required", "type": "missing", "input": None}


def _check_constraints(name: str, loc: list[str], value: Any, meta: Optional[Param]) -> Optional[dict[str, Any]]:
    if meta is None or value is None:
        return None
    if isinstance(value, str):
        if meta.min_length is not None and len(value) < meta.min_length:
            return {"loc": loc, "msg": f"String should have at least {meta.min_length} characters",
                    "type": "string_too_short", "input": value}
        if meta.max_length is not None and len(value) > meta.max_length:
            return {"loc": loc, "msg": f"String should have at most {meta.max_length} characters",
                    "type": "string_too_long", "input": value}
        if meta.pattern is not None:
            import re

            if not re.search(meta.pattern, value):
                return {"loc": loc, "msg": f"String should match pattern '{meta.pattern}'",
                        "type": "string_pattern_mismatch", "input": value,
                        "ctx": {"pattern": meta.pattern}}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if meta.ge is not None and value < meta.ge:
            return {"loc": loc, "msg": f"Input should be greater than or equal to {meta.ge}",
                    "type": "greater_than_equal", "input": value}
        if meta.le is not None and value > meta.le:
            return {"loc": loc, "msg": f"Input should be less than or equal to {meta.le}",
                    "type": "less_than_equal", "input": value}
        if meta.gt is not None and value <= meta.gt:
            return {"loc": loc, "msg": f"Input should be greater than {meta.gt}",
                    "type": "greater_than", "input": value}
        if meta.lt is not None and value >= meta.lt:
            return {"loc": loc, "msg": f"Input should be less than {meta.lt}",
                    "type": "less_than", "input": value}
    return None


_ADAPTER_CACHE: dict[Any, TypeAdapter] = {}


def _adapter(tp: Any) -> TypeAdapter:
    if tp not in _ADAPTER_CACHE:
        _ADAPTER_CACHE[tp] = TypeAdapter(tp)
    return _ADAPTER_CACHE[tp]


def _rust_error_type(msg: str, ann: Any) -> str:
    m = msg.lower()
    if "valid integer" in m:
        return "int_parsing"
    if "valid number" in m or "finite number" in m:
        return "float_parsing"
    if "valid boolean" in m:
        return "bool_parsing"
    if "at least" in m:
        return "string_too_short" if ann is str else "greater_than_equal"
    if "at most" in m:
        return "string_too_long" if ann is str else "less_than_equal"
    if "greater than or equal" in m:
        return "greater_than_equal"
    if "less than or equal" in m:
        return "less_than_equal"
    if "greater than" in m:
        return "greater_than"
    if "less than" in m:
        return "less_than"
    if "match pattern" in m:
        return "string_pattern_mismatch"
    return "value_error"


def _validate_value(loc: list[str], raw: Any, annotation: Any, meta: Optional[Param]) -> tuple[Any, Optional[dict[str, Any]]]:
    """Validate one scalar. Returns (value, error|None).

    Rust-first: `str/int/float/bool` scalars from query/path/header take the
    zero-GIL Rust validator (strict + type-safe); everything else falls back
    to Pydantic so behavior stays 1:1 with FastAPI.
    """
    ann = annotation if annotation is not inspect.Parameter.empty else Any
    if ann is Any:
        err = _check_constraints("", loc, raw, meta)
        return raw, err
    # --- Rust fast-path for scalars arriving as strings (query/path/header) ---
    if isinstance(raw, str) and ann in (int, float, bool, str) and _bridge.HAS_RUST:
        try:
            if ann is int:
                v = _bridge.validate_int_rs(
                    raw,
                    ge=int(meta.ge) if meta and meta.ge is not None else None,
                    le=int(meta.le) if meta and meta.le is not None else None,
                    gt=int(meta.gt) if meta and meta.gt is not None else None,
                    lt=int(meta.lt) if meta and meta.lt is not None else None,
                )
                return v, None
            if ann is float:
                v = _bridge.validate_float_rs(
                    raw,
                    ge=float(meta.ge) if meta and meta.ge is not None else None,
                    le=float(meta.le) if meta and meta.le is not None else None,
                    gt=float(meta.gt) if meta and meta.gt is not None else None,
                    lt=float(meta.lt) if meta and meta.lt is not None else None,
                )
                return v, None
            if ann is bool:
                return _bridge.validate_bool_rs(raw), None
            if ann is str:
                v = _bridge.validate_str_rs(
                    raw,
                    min_length=meta.min_length if meta else None,
                    max_length=meta.max_length if meta else None,
                    pattern=meta.pattern if meta else None,
                )
                return v, None
        except Exception as exc:
            msg = str(exc)
            # Strip Rust `ValueError: ` prefix if present.
            if ": " in msg and msg.split(": ", 1)[0].endswith("Error"):
                msg = msg.split(": ", 1)[1]
            err_type = _rust_error_type(msg, ann)
            err: dict[str, Any] = {"loc": loc, "msg": msg, "type": err_type, "input": raw}
            if err_type == "string_pattern_mismatch" and meta and meta.pattern:
                err["ctx"] = {"pattern": meta.pattern}
            return None, err
    # bool query strings need normalization before pydantic
    if ann is bool and isinstance(raw, str):
        parsed = _parse_bool(raw)
        if isinstance(parsed, str):
            return None, {"loc": loc, "msg": "Input should be a valid boolean",
                          "type": "bool_parsing", "input": raw}
        raw = parsed
    try:
        value = _adapter(ann).validate_python(raw)
    except Exception as exc:
        errs = getattr(exc, "errors", None)
        if callable(errs):
            first = errs()[0] if errs() else {}
            return None, {"loc": loc + list(first.get("loc", ())),
                          "msg": str(first.get("msg", "validation error")),
                          "type": str(first.get("type", "value_error")),
                          "input": raw}
        return None, {"loc": loc, "msg": str(exc), "type": "value_error", "input": raw}
    err = _check_constraints("", loc, value, meta)
    if err is not None:
        return None, err
    return value, None


# ------------------------------------------------------- dependency core ---

async def _call_with_teardown(fn: Callable[..., Any], kwargs: dict[str, Any], stack: AsyncExitStack) -> Any:
    """Call sync/async/generator dependency; `yield` values enter the stack."""
    if inspect.isasyncgenfunction(fn):
        gen = fn(**kwargs)
        try:
            value = await gen.__anext__()
        except StopAsyncIteration as exc:
            raise RuntimeError(f"async generator dependency {fn} did not yield") from exc

        async def _aclose() -> None:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass
            finally:
                await gen.aclose()

        stack.push_async_callback(_aclose)
        return value
    if inspect.isgeneratorfunction(fn):
        gen = fn(**kwargs)
        try:
            value = next(gen)
        except StopIteration as exc:
            raise RuntimeError(f"generator dependency {fn} did not yield") from exc

        def _close() -> None:
            try:
                next(gen)
            except StopIteration:
                pass

        stack.push(_close)  # type: ignore[arg-type]
        return value
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    # Instances with async __call__ (security schemes), partials, sync fns.
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _solve_fn_kwargs(
    fn: Callable[..., Any],
    *,
    request: Request,
    path_params: dict[str, Any],
    query: dict[str, str],
    headers: dict[str, str],
    cookies: dict[str, str],
    body_json: Any,
    form: Optional[dict[str, Any]],
    cache: dict[int, Any],
    stack: AsyncExitStack,
    body_raw: Optional[bytes] = None,
) -> dict[str, Any]:
    """Resolve kwargs for an endpoint *or* a dependency (recursive)."""
    sig = _signature(fn)
    if sig is None:
        return {}
    kwargs: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for pname, param in sig.parameters.items():
        # Unbound dunder initializers (`SomeForm.__init__`) carry `self`;
        # it is never a request parameter.
        if pname in ("self", "cls") and getattr(fn, "__name__", "") in ("__init__", "__new__"):
            continue
        ann = _ann_of(fn, pname, param.annotation)
        default = param.default

        # --- Request injection ------------------------------------------------
        if _is_request_annotation(ann) or pname in ("request", "req") and ann is inspect.Parameter.empty and isinstance(default, inspect.Parameter.empty.__class__):
            # Only inject when annotation matches or bare `request` with no default.
            if _is_request_annotation(ann) or (pname == "request" and ann is inspect.Parameter.empty and default is inspect.Parameter.empty):
                kwargs[pname] = request
                continue

        # --- BackgroundTasks / SecurityScopes (framework-owned) ---------------
        if ann is BackgroundTasks or (isinstance(ann, str) and ann == "BackgroundTasks"):
            tasks = cache.get(_BG_KEY)
            if tasks is None:
                tasks = BackgroundTasks()
                cache[_BG_KEY] = tasks
            kwargs[pname] = tasks
            continue
        if ann is SecurityScopes or (isinstance(ann, str) and ann == "SecurityScopes"):
            kwargs[pname] = SecurityScopes()
            continue

        # --- Depends ----------------------------------------------------------
        if isinstance(default, Depends):
            dep_fn = default.dependency
            if dep_fn is None:
                # `Depends()` with annotation as dependency (FastAPI class-dependency style)
                if inspect.isclass(ann) or inspect.isfunction(ann):
                    dep_fn = ann
                else:
                    errors.append(_missing(["query", pname]))
                    continue
            cache_key = id(dep_fn)
            if default.use_cache and cache_key in cache:
                kwargs[pname] = cache[cache_key]
                continue
            # Support class dependencies: instantiate with solved kwargs
            if inspect.isclass(dep_fn) and not _is_pydantic_model(dep_fn):
                sub_kwargs = await _solve_fn_kwargs(
                    dep_fn.__init__, request=request, path_params=path_params, query=query,
                    headers=headers, cookies=cookies, body_json=body_json, form=form,
                    cache=cache, stack=stack, body_raw=body_raw,
                )
                sub_kwargs.pop("self", None)
                try:
                    instance = dep_fn(**sub_kwargs)
                except TypeError:
                    instance = dep_fn()
                if default.use_cache:
                    cache[cache_key] = instance
                kwargs[pname] = instance
                continue
            sub_kwargs = await _solve_fn_kwargs(
                dep_fn, request=request, path_params=path_params, query=query,
                headers=headers, cookies=cookies, body_json=body_json, form=form,
                cache=cache, stack=stack, body_raw=body_raw,
            )
            value = await _call_with_teardown(dep_fn, sub_kwargs, stack)
            if default.use_cache:
                cache[cache_key] = value
            kwargs[pname] = value
            continue

        # --- Explicit Param(...) ---------------------------------------------
        if isinstance(default, Param):
            meta = default
            alias = meta.alias or pname
            loc = [meta.in_, alias]
            if meta.in_ == "path":
                raw = path_params.get(pname, path_params.get(alias))
                if raw is None:
                    if meta.has_default() and meta.default is not Ellipsis:
                        kwargs[pname] = meta.default
                        continue
                    errors.append(_missing(["path", pname]))
                    continue
                val, err = _validate_value(["path", pname], raw, ann, meta)
                if err:
                    errors.append(err)
                else:
                    kwargs[pname] = val
            elif meta.in_ == "query":
                # support list query params: List[int] etc.
                origin = get_origin(ann)
                is_list = origin in (list,) or str(origin) == "list"
                if is_list and alias in request.query_params:
                    raws = request.query_params.getlist(alias)
                    vals: list[Any] = []
                    inner = getattr(ann, "__args__", (str,))[0] if hasattr(ann, "__args__") else str
                    failed = False
                    for r in raws:
                        v, e = _validate_value(["query", alias], r, inner, meta)
                        if e:
                            errors.append(e)
                            failed = True
                            break
                        vals.append(v)
                    if not failed:
                        kwargs[pname] = vals
                elif alias in query:
                    val, err = _validate_value(["query", alias], query[alias], ann, meta)
                    if err:
                        errors.append(err)
                    else:
                        kwargs[pname] = val
                else:
                    if meta.has_default() and meta.default is not Ellipsis:
                        kwargs[pname] = meta.default
                    elif param.default is inspect.Parameter.empty and meta.required:
                        errors.append(_missing(["query", alias]))
                    elif meta.has_default():
                        kwargs[pname] = meta.default
                    else:
                        kwargs[pname] = None
            elif meta.in_ == "header":
                key = alias.replace("_", "-").lower()
                raw = headers.get(key, headers.get(alias.lower()))
                if raw is None:
                    if meta.has_default() and meta.default is not Ellipsis:
                        kwargs[pname] = meta.default
                    elif meta.required and param.default is inspect.Parameter.empty:
                        errors.append(_missing(["header", alias]))
                    else:
                        kwargs[pname] = meta.default if meta.has_default() else None
                else:
                    val, err = _validate_value(["header", alias], raw, ann, meta)
                    if err:
                        errors.append(err)
                    else:
                        kwargs[pname] = val
            elif meta.in_ == "cookie":
                raw = cookies.get(alias)
                if raw is None:
                    if meta.has_default() and meta.default is not Ellipsis:
                        kwargs[pname] = meta.default
                    elif meta.required and param.default is inspect.Parameter.empty:
                        errors.append(_missing(["cookie", alias]))
                    else:
                        kwargs[pname] = meta.default if meta.has_default() else None
                else:
                    val, err = _validate_value(["cookie", alias], raw, ann, meta)
                    if err:
                        errors.append(err)
                    else:
                        kwargs[pname] = val
            elif meta.in_ in ("body", "form", "file"):
                # handled below via body/form
                val, errs = _resolve_body_param(
                    pname, ann, meta, body_json, form, request, body_raw
                )
                if errs:
                    errors.extend(errs)
                else:
                    kwargs[pname] = val
            continue

        # --- Pydantic model without marker -> body ----------------------------
        if _is_pydantic_model(ann):
            val, errs = _resolve_body_param(pname, ann, None, body_json, form, request, body_raw)
            if errs:
                errors.extend(errs)
            else:
                kwargs[pname] = val
            continue

        # --- Path fallback (name appears in path) ------------------------------
        if pname in path_params:
            val, err = _validate_value(["path", pname], path_params[pname], ann, None)
            if err:
                errors.append(err)
            else:
                kwargs[pname] = val
            continue

        # --- Plain default -> query -------------------------------------------
        if alias_q := query.get(pname):
            val, err = _validate_value(["query", pname], alias_q, ann, None)
            if err:
                errors.append(err)
            else:
                kwargs[pname] = val
            continue
        if default is not inspect.Parameter.empty:
            kwargs[pname] = default
            continue
        # Required: decide loc — query by default (FastAPI behavior)
        errors.append(_missing(["query", pname]))

    if errors:
        raise RequestValidationError(errors)
    return kwargs


def _pydantic_errors(exc: Exception, loc_prefix: list[str], payload: Any) -> list[dict[str, Any]]:
    """Map ALL pydantic errors (FastAPI returns every failure, not the first)."""
    errs = exc.errors() if hasattr(exc, "errors") else [{"msg": str(exc), "type": "value_error"}]
    out = []
    for e in errs:
        out.append({
            "loc": loc_prefix + [str(x) for x in e.get("loc", ())],
            "msg": str(e.get("msg", "validation error")),
            "type": str(e.get("type", "value_error")),
            "input": e.get("input", payload),
        })
        if "url" in e:
            out[-1]["url"] = e["url"]
    return out


def _json_invalid_error(body_raw: bytes) -> list[dict[str, Any]]:
    """Mirror FastAPI's malformed-JSON 422 (offset comes from the error path only)."""
    pos: Any = 0
    msg = "Expecting value"
    try:
        _stdlib_json.loads(body_raw.decode("utf-8", "replace"))
    except _stdlib_json.JSONDecodeError as exc:
        pos, msg = exc.pos, exc.msg
    except Exception:
        pass
    return [{"loc": ["body", pos], "msg": "JSON decode error", "type": "json_invalid",
             "input": {}, "ctx": {"error": msg}}]


def _resolve_body_param(
    pname: str,
    ann: Any,
    meta: Optional[Param],
    body_json: Any,
    form: Optional[dict[str, Any]],
    request: Request,
    body_raw: Optional[bytes] = None,
) -> tuple[Any, list[dict[str, Any]]]:
    no: list[dict[str, Any]] = []
    # File / UploadFile
    if ann is not inspect.Parameter.empty and getattr(ann, "__name__", "") in ("UploadFile", "FileStorage"):
        if form is not None and pname in form:
            return form[pname], no
        # also check alias
        alias = (meta.alias if meta else None) or pname
        if form is not None and alias in form:
            return form[alias], no
        return None, [_missing(["body", pname])]
    if meta is not None and meta.in_ == "file":
        alias = meta.alias or pname
        if form is not None and (pname in form or alias in form):
            return form.get(pname, form.get(alias)), no
        return None, [_missing(["body", alias])]
    if meta is not None and meta.in_ == "form":
        alias = meta.alias or pname
        if form is not None and (pname in form or alias in form):
            raw = form.get(pname, form.get(alias))
            if ann is inspect.Parameter.empty or ann is Any:
                return raw, no
            val, err = _validate_value(["body", alias], raw, ann, meta)
            return (val, no) if err is None else (None, [err])
        if meta.has_default() and meta.default is not Ellipsis:
            return meta.default, no
        return None, [_missing(["body", alias])]

    # JSON body
    embed = bool(meta.embed) if meta else False
    alias = (meta.alias if meta else None) or pname
    # Fast path: single Pydantic model takes the whole raw payload through
    # `validate_json` — one Rust pass (parse + validate), no intermediate dict.
    if _is_pydantic_model(ann) and not embed and body_raw:
        try:
            return _adapter(ann).validate_json(body_raw), no
        except Exception as exc:
            get_errs = getattr(exc, "errors", None)
            first_type = get_errs()[0].get("type") if callable(get_errs) and get_errs() else None
            if first_type == "json_invalid":
                # Normalize pydantic's raw-bytes error to FastAPI's shape.
                return None, _json_invalid_error(body_raw)
            return None, _pydantic_errors(exc, ["body"], body_raw.decode("utf-8", "replace"))
    if body_json is None:
        if body_raw:
            # Bytes arrived but no parsed view yet: parse on first real use
            # (cached on request.state), or report malformed JSON like FastAPI.
            try:
                body_json = _parsed_json_cached(request, body_raw)
            except _InvalidJson:
                return None, _json_invalid_error(body_raw)
        else:
            # No JSON payload at all.
            has_default = False
            dflt = None
            if meta is not None and meta.has_default() and meta.default is not Ellipsis:
                has_default, dflt = True, meta.default
            if has_default:
                return dflt, no
            # Optional[X] (default None in signature handled by caller); here required.
            return None, [_missing(["body", alias if embed else pname])]
    payload = body_json.get(alias, body_json.get(pname, _MISSING)) if isinstance(body_json, dict) and embed else (
        body_json.get(pname, body_json) if isinstance(body_json, dict) and meta is None and not _is_pydantic_model(ann) and pname in body_json
        else body_json if _is_pydantic_model(ann) or meta is None or not embed else body_json.get(alias, _MISSING)
    )
    # Simplify: single-model body takes the whole payload.
    if _is_pydantic_model(ann) and not embed:
        payload = body_json
    if payload is _MISSING:
        if meta is not None and meta.has_default() and meta.default is not Ellipsis:
            return meta.default, no
        return None, [_missing(["body", alias])]
    if ann is inspect.Parameter.empty or ann is Any:
        return payload, no
    try:
        value = _adapter(ann).validate_python(payload)
        return value, no
    except Exception as exc:
        return None, _pydantic_errors(exc, ["body"], payload)


_MISSING: Any = object()


class _InvalidJson(Exception):
    """Internal: cached body bytes are not valid JSON."""


def _parsed_json_cached(request: Request, body_raw: bytes) -> Any:
    """Parse body bytes once per request (request.state cache).

    Raises `_InvalidJson` when malformed so callers mirror FastAPI's
    `json_invalid` 422. The single-model fast path never calls this.
    """
    state = getattr(request, "state", None)
    if state is not None and hasattr(state, "_fa_body"):
        cached = getattr(state, "_fa_body")
        if isinstance(cached, _InvalidJson):
            raise cached
        return cached
    try:
        parsed = _bridge.fast_json_loads(body_raw.decode("utf-8"))
    except Exception as exc:
        err = _InvalidJson(str(exc))
        if state is not None:
            try:
                setattr(state, "_fa_body", err)
            except Exception:
                pass
        raise err
    if state is not None:
        try:
            setattr(state, "_fa_body", parsed)
        except Exception:
            pass
    return parsed


# ----------------------------------------------------------------- routes ---

class _RouteDef:
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        methods: list[str],
        *,
        response_model: Any = None,
        status_code: int = 200,
        response_class: Any = None,
        deprecated: bool = False,
        tags: Optional[list[str]] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        dependencies: Optional[list[Depends]] = None,
        name: Optional[str] = None,
    ) -> None:
        self.path = path
        self.endpoint = endpoint
        self.methods = [m.upper() for m in methods]
        self.response_model = response_model
        self.status_code = status_code
        self.response_class = response_class
        self.deprecated = deprecated
        self.tags = tags or []
        self.summary = summary
        self.description = description
        self.dependencies = dependencies or []
        self.name = name or getattr(endpoint, "__name__", path)


# Models whose instances serialize identically with and without a
# revalidation round-trip (no validators/serializers that could transform).
_MODEL_PASSTHROUGH: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _model_dump_direct(model_cls: Any) -> bool:
    """True when `dump_json(instance)` == `dump_json(validate(instance))`."""
    try:
        direct = _MODEL_PASSTHROUGH.get(model_cls)
    except TypeError:
        direct = None
    if direct is not None:
        return direct
    try:
        dec = getattr(model_cls, "__pydantic_decorators__", None)
        decs = getattr(dec, "decorators", {}) if dec is not None else {}
        if decs:
            ok = False
        else:
            # Field-level validators/serializers live on the fields.
            ok = True
            fields = getattr(model_cls, "model_fields", {}) or {}
            for f in fields.values():
                if getattr(f, "validators", None) or getattr(f, "serializer", None):
                    ok = False
                    break
    except Exception:
        ok = False
    try:
        _MODEL_PASSTHROUGH[model_cls] = ok
    except TypeError:
        pass
    return ok


def _serialize_response(result: Any, response_model: Any, status_code: int = 200) -> Any:
    if isinstance(result, Response):
        return result
    if response_model is not None:
        try:
            if type(result) is response_model and _model_dump_direct(response_model):
                # Already a valid instance with no transforming hooks:
                # skip the revalidation round-trip, serialize directly.
                return Response(
                    content=_adapter(response_model).dump_json(result),
                    status_code=status_code,
                    media_type="application/json",
                )
            validated = _adapter(response_model).validate_python(result)
            # One Rust pass straight to JSON bytes — no intermediate dict.
            return Response(
                content=_adapter(response_model).dump_json(validated),
                status_code=status_code,
                media_type="application/json",
            )
        except Exception as exc:
            errs = exc.errors() if hasattr(exc, "errors") else [{"msg": str(exc), "type": "value_error"}]
            from .exceptions import ResponseValidationError

            raise ResponseValidationError(
                [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg")), "type": str(e.get("type"))} for e in errs]
            )
    if result is None:
        return None
    if isinstance(result, BaseModel):
        # Rust serializer straight to bytes.
        return Response(content=result.model_dump_json().encode("utf-8"),
                        status_code=status_code, media_type="application/json")
    return jsonable_encoder(result)


def _make_http_handler(route: _RouteDef, router_deps: list[Depends]) -> Callable[[Request], Coroutine[Any, Any, Response]]:
    async def handler(request: Request) -> Response:
        path_params = dict(request.path_params)
        query = dict(request.query_params)
        headers = {k.lower(): v for k, v in request.headers.items()}
        cookies = dict(request.cookies)
        # Body: keep raw bytes only. The single-model fast path validates
        # straight from bytes; anything else parses lazily (cached) on use.
        body_raw: Optional[bytes] = None
        body_json: Any = None
        form: Optional[dict[str, Any]] = None
        ctype = request.headers.get("content-type", "")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if "application/json" in ctype or (ctype == "" and (request.headers.get("content-length", "0") != "0")):
                try:
                    raw = await request.body()
                    if raw:
                        body_raw = bytes(raw)
                except Exception:
                    body_raw = None
            elif "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
                try:
                    f = await request.form()
                    form = {}
                    for k, v in f.items():
                        form[k] = v
                except Exception:
                    form = {}
        # Top-level + router-level dependencies (executed for side effects / guards).
        async with AsyncExitStack() as stack:
            cache: dict[int, Any] = {}
            try:
                for dep in list(router_deps) + list(route.dependencies):
                    if dep.dependency is None:
                        continue
                    key = id(dep.dependency)
                    if dep.use_cache and key in cache:
                        continue
                    sub = await _solve_fn_kwargs(
                        dep.dependency, request=request, path_params=path_params,
                        query=query, headers=headers, cookies=cookies,
                        body_json=body_json, form=form, cache=cache, stack=stack,
                        body_raw=body_raw,
                    )
                    val = await _call_with_teardown(dep.dependency, sub, stack)
                    if dep.use_cache:
                        cache[key] = val
                kwargs = await _solve_fn_kwargs(
                    route.endpoint, request=request, path_params=path_params,
                    query=query, headers=headers, cookies=cookies,
                    body_json=body_json, form=form, cache=cache, stack=stack,
                    body_raw=body_raw,
                )
            except RequestValidationError as exc:
                return JSONResponse({"detail": exc.errors()}, status_code=422)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers=exc.headers or {})
            try:
                if inspect.iscoroutinefunction(route.endpoint):
                    result = await route.endpoint(**kwargs)
                else:
                    result = route.endpoint(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers=exc.headers or {})
            except RequestValidationError as exc:
                return JSONResponse({"detail": exc.errors()}, status_code=422)
            # SSE: generator endpoint + EventSourceResponse streams frames.
            if _is_sse_route(route) and (inspect.isasyncgen(result) or inspect.isgenerator(result)):
                return _with_background(_sse_response(result, route.status_code), cache)
            try:
                payload = _serialize_response(result, route.response_model, route.status_code)
            except Exception as exc:
                from .exceptions import ResponseValidationError

                if isinstance(exc, ResponseValidationError):
                    return JSONResponse({"detail": exc.errors()}, status_code=500)
                raise
            if payload is None:
                return _with_background(Response(status_code=route.status_code), cache)
            if isinstance(payload, Response):
                return _with_background(payload, cache)
            return _with_background(JSONResponse(payload, status_code=route.status_code), cache)

    # Name the handler for Starlette route introspection.
    handler.__name__ = getattr(route.endpoint, "__name__", "handler")  # type: ignore[attr-defined]
    return handler


def _with_background(response: Response, cache: dict[int, Any]) -> Response:
    """Attach shared BackgroundTasks when the endpoint (or deps) queued work."""
    try:
        tasks = cache.get(_BG_KEY)
        if tasks is not None and getattr(tasks, "tasks", None) and response.background is None:
            response.background = tasks
    except Exception:
        pass
    return response


def _is_sse_route(route: _RouteDef) -> bool:
    try:
        from .sse import EventSourceResponse

        return (
            route.response_class is not None
            and inspect.isclass(route.response_class)
            and issubclass(route.response_class, EventSourceResponse)
        )
    except Exception:
        return False


def _serialize_sse_item(item: Any) -> bytes:
    """Mirror FastAPI's per-item SSE encoding exactly."""
    from .sse import ServerSentEvent, format_sse_event

    if isinstance(item, ServerSentEvent):
        if item.raw_data is not None:
            data_str = item.raw_data
        elif item.data is not None:
            if hasattr(item.data, "model_dump_json"):
                data_str = item.data.model_dump_json()
            else:
                data_str = _stdlib_json.dumps(jsonable_encoder(item.data))
        else:
            data_str = None
        return format_sse_event(
            data_str=data_str, event=item.event, id=item.id,
            retry=item.retry, comment=item.comment,
        )
    return format_sse_event(
        data_str=_stdlib_json.dumps(jsonable_encoder(item)).encode("utf-8").decode("utf-8")
    )


def _sse_response(gen: Any, status_code: int) -> StreamingResponse:
    async def _aiter():  # type: ignore[no-untyped-def]
        if inspect.isasyncgen(gen):
            async for item in gen:
                yield _serialize_sse_item(item)
        else:
            async for item in iterate_in_threadpool(gen):
                yield _serialize_sse_item(item)

    return StreamingResponse(_aiter(), status_code=status_code, media_type="text/event-stream")


# ---------------------------------------------------------------- router ---

class APIRouter:
    """Drop-in ``fastapi.APIRouter``."""

    def __init__(
        self,
        *,
        prefix: str = "",
        tags: Optional[list[str]] = None,
        dependencies: Optional[list[Depends]] = None,
        default_response_class: Any = JSONResponse,
        responses: Optional[dict[int, dict[str, Any]]] = None,
        redirect_slashes: bool = True,
        route_class: Any = None,
        **_extra: Any,
    ) -> None:
        self.prefix = prefix.rstrip("/")
        self.tags = tags or []
        self.dependencies = dependencies or []
        self.default_response_class = default_response_class
        self.responses = responses
        self.redirect_slashes = redirect_slashes
        self.route_class = route_class
        self.routes: list[_RouteDef] = []
        self.websocket_routes: list[tuple[str, Callable[..., Any], Optional[str]]] = []
        self._sub_routers: list[tuple["APIRouter", str]] = []

    # -- registration ------------------------------------------------------
    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        methods: Optional[list[str]] = None,
        response_model: Any = None,
        status_code: int = 200,
        response_class: Any = None,
        tags: Optional[list[str]] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        deprecated: bool = False,
        dependencies: Optional[list[Depends]] = None,
        name: Optional[str] = None,
        **_extra: Any,
    ) -> None:
        full = self.prefix + (path if path.startswith("/") else "/" + path)
        rc = response_class if response_class is not None else self.default_response_class
        if rc is JSONResponse:
            rc = None  # default JSON rendering needs no marker
        self.routes.append(
            _RouteDef(
                full, endpoint, methods or ["GET"],
                response_model=response_model, status_code=status_code,
                response_class=rc, deprecated=deprecated,
                tags=[*(self.tags or []), *(tags or [])],
                summary=summary, description=description,
                dependencies=[*self.dependencies, *(dependencies or [])],
                name=name,
            )
        )

    def api_route(self, path: str, *, methods: list[str], **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add_api_route(path, fn, methods=methods, **kw)
            return fn

        return deco

    def get(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw.setdefault("methods", ["GET"])
        return self.api_route(path, **kw)

    def post(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["POST"]
        if "status_code" not in kw:
            kw["status_code"] = 200
        return self.api_route(path, **kw)

    def put(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["PUT"]
        return self.api_route(path, **kw)

    def delete(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["DELETE"]
        return self.api_route(path, **kw)

    def patch(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["PATCH"]
        return self.api_route(path, **kw)

    def options(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["OPTIONS"]
        return self.api_route(path, **kw)

    def head(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["HEAD"]
        return self.api_route(path, **kw)

    def trace(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw["methods"] = ["TRACE"]
        return self.api_route(path, **kw)

    def route(self, path: str, *, methods: list[str], **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=methods, **kw)

    def websocket(self, path: str, *, name: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            full = self.prefix + (path if path.startswith("/") else "/" + path)
            self.websocket_routes.append((full, fn, name or getattr(fn, "__name__", None)))
            return fn

        return deco

    def include_router(
        self,
        router: "APIRouter",
        *,
        prefix: str = "",
        tags: Optional[list[str]] = None,
        dependencies: Optional[list[Depends]] = None,
        **_extra: Any,
    ) -> None:
        extra_tags = tags or []
        extra_deps = dependencies or []
        for r in router.routes:
            merged = _RouteDef(
                prefix.rstrip("/") + r.path, r.endpoint, r.methods,
                response_model=r.response_model, status_code=r.status_code,
                response_class=r.response_class, deprecated=r.deprecated,
                tags=[*r.tags, *extra_tags], summary=r.summary,
                description=r.description,
                dependencies=[*r.dependencies, *extra_deps], name=r.name,
            )
            # re-root with self prefix
            merged.path = self.prefix + merged.path
            self.routes.append(merged)
        for path, fn, name in router.websocket_routes:
            self.websocket_routes.append((self.prefix + prefix.rstrip("/") + path, fn, name))

    # -- starlette conversion ------------------------------------------------
    def _starlette_routes(self) -> list[Any]:
        out: list[Any] = []
        for r in self.routes:
            out.append(
                StarletteRoute(
                    r.path, _make_http_handler(r, []),
                    methods=r.methods, name=r.name,
                )
            )
        for path, fn, name in self.websocket_routes:
            out.append(WebSocketRoute(path, _make_ws_handler(fn), name=name))
        return out


def _make_ws_handler(fn: Callable[..., Any]) -> Callable[[WebSocket], Coroutine[Any, Any, None]]:
    async def handler(ws: WebSocket) -> None:
        # Minimal Depends support for websockets: inject by name where possible.
        try:
            sig = inspect.signature(fn)
            kwargs: dict[str, Any] = {}
            async with AsyncExitStack() as stack:
                cache: dict[int, Any] = {}
                for pname, param in sig.parameters.items():
                    ann = _ann_of(fn, pname, param.annotation)
                    if isinstance(param.default, Depends) and param.default.dependency is not None:
                        dep = param.default.dependency
                        sub = await _solve_fn_kwargs(
                            dep, request=ws, path_params=dict(ws.path_params),  # type: ignore[arg-type]
                            query=dict(ws.query_params), headers={k.lower(): v for k, v in ws.headers.items()},
                            cookies=dict(ws.cookies), body_json=None, form=None,
                            cache=cache, stack=stack,
                        )
                        kwargs[pname] = await _call_with_teardown(dep, sub, stack)
                    elif ann is WebSocket or pname in ("websocket", "ws", "connection"):
                        kwargs[pname] = ws
                    elif pname == "self":
                        continue
                    elif param.default is inspect.Parameter.empty:
                        # Unannotated single-arg websocket endpoint: assume the socket.
                        if len(sig.parameters) == 1:
                            kwargs[pname] = ws
                if inspect.iscoroutinefunction(fn):
                    await fn(**kwargs)
                else:
                    res = fn(**kwargs)
                    if inspect.isawaitable(res):
                        await res
        except Exception:
            try:
                await ws.close(code=1011)
            except Exception:
                pass

    return handler
