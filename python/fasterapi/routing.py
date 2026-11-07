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
import threading
import weakref
from contextlib import AsyncExitStack
from typing import Any, Callable, Coroutine, Optional, Union, get_origin

from pydantic import BaseModel, TypeAdapter
from starlette.background import BackgroundTasks
from starlette.concurrency import iterate_in_threadpool
from starlette.concurrency import run_in_threadpool as run_in_threadpool  # noqa: F401
from starlette.datastructures import FormData as FormData  # noqa: F401
from starlette.datastructures import URL as URL  # noqa: F401
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.responses import PlainTextResponse as PlainTextResponse  # noqa: F401
from starlette.responses import RedirectResponse as RedirectResponse  # noqa: F401
from starlette.routing import Match as Match  # noqa: F401
from starlette.routing import Mount as Mount  # noqa: F401
from starlette.routing import NoMatchFound as NoMatchFound  # noqa: F401
from starlette.routing import Route as StarletteRoute
from starlette.routing import URLPath as URLPath  # noqa: F401
from starlette.routing import BaseRoute as BaseRoute  # noqa: F401
from starlette.routing import WebSocketRoute
from starlette.routing import compile_path as compile_path  # noqa: F401
from starlette.routing import request_response as request_response  # noqa: F401
from starlette.routing import websocket_session as websocket_session  # noqa: F401
from starlette.routing import wrap_app_handling_exceptions as wrap_app_handling_exceptions  # noqa: F401
from starlette.staticfiles import StaticFiles as StaticFiles  # noqa: F401
from starlette.types import AppType as AppType  # noqa: F401
from starlette.websockets import WebSocket

from . import _rust_bridge as _bridge
from . import params as params  # noqa: F401 (surface parity)
from .encoders import jsonable_encoder
from .exceptions import EndpointContext as EndpointContext  # noqa: F401
from .exceptions import FastAPIError as FastAPIError  # noqa: F401
from .exceptions import HTTPException, RequestValidationError
from .exceptions import ResponseValidationError
from .exceptions import WebSocketRequestValidationError as WebSocketRequestValidationError  # noqa: F401
from .params import Depends, Param
from .params import _UNSET as _PARAM_UNSET
from .security import SecurityScopes
from .sse import EventSourceResponse as EventSourceResponse  # noqa: F401
from .sse import ServerSentEvent as ServerSentEvent  # noqa: F401
from .sse import format_sse_event as format_sse_event  # noqa: F401
from .types import DecoratedCallable as DecoratedCallable  # noqa: F401
from .utils import generate_unique_id as generate_unique_id  # noqa: F401
from .utils import is_body_allowed_for_status_code as is_body_allowed_for_status_code  # noqa: F401

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
_HINTS_CACHE_LOCK = threading.RLock()
_SIG_CACHE_LOCK = threading.RLock()


def _signature(fn: Callable[..., Any]) -> inspect.Signature | None:
    """Per-process cached signature (hot request path must not re-inspect).

    Weak keys: ``id()``-keyed caches would collide after garbage collection
    and serve one endpoint's signature to another.
    """
    try:
        with _SIG_CACHE_LOCK:
            sig = _SIG_CACHE.get(fn)
    except TypeError:
        sig = None
    if sig is None:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return None
        try:
            with _SIG_CACHE_LOCK:
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
        with _HINTS_CACHE_LOCK:
            hints = _HINTS_CACHE.get(fn)  # type: ignore[arg-type]
    except TypeError:
        hints = None
    if hints is not None:
        return hints
    try:
        import typing

        hints = typing.get_type_hints(fn, include_extras=True)
        try:
            with _HINTS_CACHE_LOCK:
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
                    "type": "string_too_short", "input": value, "ctx": {"min_length": meta.min_length}}
        if meta.max_length is not None and len(value) > meta.max_length:
            return {"loc": loc, "msg": f"String should have at most {meta.max_length} characters",
                    "type": "string_too_long", "input": value, "ctx": {"max_length": meta.max_length}}
        if meta.pattern is not None:
            import re

            if not re.search(meta.pattern, value):
                return {"loc": loc, "msg": f"String should match pattern '{meta.pattern}'",
                        "type": "string_pattern_mismatch", "input": value,
                        "ctx": {"pattern": meta.pattern}}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if meta.ge is not None and value < meta.ge:
            return {"loc": loc, "msg": f"Input should be greater than or equal to {meta.ge}",
                    "type": "greater_than_equal", "input": value, "ctx": {"ge": meta.ge}}
        if meta.le is not None and value > meta.le:
            return {"loc": loc, "msg": f"Input should be less than or equal to {meta.le}",
                    "type": "less_than_equal", "input": value, "ctx": {"le": meta.le}}
        if meta.gt is not None and value <= meta.gt:
            return {"loc": loc, "msg": f"Input should be greater than {meta.gt}",
                    "type": "greater_than", "input": value, "ctx": {"gt": meta.gt}}
        if meta.lt is not None and value >= meta.lt:
            return {"loc": loc, "msg": f"Input should be less than {meta.lt}",
                    "type": "less_than", "input": value, "ctx": {"lt": meta.lt}}
    return None


_ADAPTER_CACHE: dict[Any, TypeAdapter] = {}
_ADAPTER_CACHE_LOCK = threading.RLock()


def _adapter(tp: Any) -> TypeAdapter:
    with _ADAPTER_CACHE_LOCK:
        adapter = _ADAPTER_CACHE.get(tp)
        if adapter is None:
            adapter = TypeAdapter(tp)
            _ADAPTER_CACHE[tp] = adapter
        return adapter


def _int_constraints_fit_rust(meta: Optional[Param]) -> bool:
    """Rust int constraints are i64; avoid truncating fractional bounds."""
    if meta is None:
        return True
    for value in (meta.ge, meta.le, meta.gt, meta.lt):
        if value is None:
            continue
        try:
            if int(value) != value:
                return False
        except (OverflowError, TypeError, ValueError):
            return False
    return True


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
    if (
        isinstance(raw, str)
        and ann in (int, float, bool, str)
        and _bridge.HAS_RUST
        and (ann is not int or _int_constraints_fit_rust(meta))
    ):
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
            if ann is int:
                # Python integers are unbounded while the native fast path is
                # currently i64-based. Preserve Pydantic's behavior on values
                # outside that range (and on native parse/constraint errors).
                try:
                    value = _adapter(int).validate_python(raw)
                except Exception:
                    pass
                else:
                    err = _check_constraints("", loc, value, meta)
                    if err is None:
                        return value, None
                    # Preserve the original wire value in Pydantic-style errors.
                    err["input"] = raw
                    return None, err
            msg = str(exc)
            # Strip Rust `ValueError: ` prefix if present.
            if ": " in msg and msg.split(": ", 1)[0].endswith("Error"):
                msg = msg.split(": ", 1)[1]
            err_type = _rust_error_type(msg, ann)
            err: dict[str, Any] = {"loc": loc, "msg": msg, "type": err_type, "input": raw}
            if err_type == "string_pattern_mismatch" and meta and meta.pattern:
                err["ctx"] = {"pattern": meta.pattern}
            elif err_type in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
                import re as _re

                m = _re.search(r"(-?\d+(?:\.\d+)?)\s*$", msg)
                if m:
                    num = float(m.group(1))
                    key = {"greater_than_equal": "ge", "less_than_equal": "le",
                           "greater_than": "gt", "less_than": "lt"}[err_type]
                    err["ctx"] = {key: int(num) if (num.is_integer() and ann is int) else num}
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
            first = errs(include_url=False)[0] if errs() else {}
            err: dict[str, Any] = {"loc": loc + list(first.get("loc", ())),
                                   "msg": str(first.get("msg", "validation error")),
                                   "type": str(first.get("type", "value_error")),
                                   "input": raw}
            if "ctx" in first:
                err["ctx"] = first["ctx"]
            return None, err
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
        # --- WebSocket injection (websocket routes only) -----------------------
        if ann is WebSocket or (isinstance(ann, str) and ann == "WebSocket"):
            kwargs[pname] = request
            continue
        if pname in ("websocket", "ws", "connection") and default is inspect.Parameter.empty \
                and ann is inspect.Parameter.empty and type(request).__name__ == "WebSocket":
            kwargs[pname] = request
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

        # --- Bare dict/list without marker -> body (FastAPI rule) --------------
        # `payload: dict` (or any subscripted container like `list[Item]`)
        # takes the whole JSON body. A bare `list`/`tuple`/`set` without a
        # `Body()` marker is unsatisfiable via JSON (FastAPI 422s too).
        _origin = get_origin(ann)
        if (ann in (dict, list, tuple, set, frozenset) or _origin in (dict, list, tuple, set, frozenset)) \
                and default is inspect.Parameter.empty:
            if ann in (list, tuple, set, frozenset):
                errors.extend(_missing(["body", pname]))
                continue
            val, errs = _resolve_body_param(
                pname, ann, None, body_json, form, request, body_raw,
            )
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


def _json_body_type(ann: Any) -> bool:
    """Annotations safe for single-pass `validate_json` (models + containers)."""
    if ann is inspect.Parameter.empty or ann is Any:
        return False
    if _is_pydantic_model(ann):
        return True
    if ann in (dict, list):
        return True
    return get_origin(ann) in (dict, list, tuple, set, frozenset)


def _pydantic_errors(exc: Exception, loc_prefix: list[str], payload: Any) -> list[dict[str, Any]]:
    """Map ALL pydantic errors (FastAPI returns every failure, not the first).

    Wire shape mirrors FastAPI: pydantic errors with ``include_url=False``
    (no ``url`` key) + FastAPI's ``loc`` prefix. ``ctx`` is preserved.
    """
    errs = exc.errors(include_url=False) if hasattr(exc, "errors") else [{"msg": str(exc), "type": "value_error"}]
    out = []
    for e in errs:
        item = {
            "loc": loc_prefix + [str(x) for x in e.get("loc", ())],
            "msg": str(e.get("msg", "validation error")),
            "type": str(e.get("type", "value_error")),
            "input": e.get("input", payload),
        }
        if "ctx" in e:
            item["ctx"] = e["ctx"]
        out.append(item)
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
    # Fast path: single JSON-shaped body takes the whole raw payload through
    # `validate_json` — one Rust pass (parse + validate), no intermediate dict.
    if _json_body_type(ann) and not embed and body_raw:
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
    if embed:
        # Embedded field: only a JSON object carrying the name qualifies.
        if isinstance(body_json, dict) and (alias in body_json or pname in body_json):
            payload = body_json.get(alias, body_json.get(pname))
        else:
            payload = _MISSING
    elif _is_pydantic_model(ann) or meta is None:
        # Single model (or unmarked) body takes the whole payload.
        payload = body_json
    elif isinstance(body_json, dict):
        payload = body_json.get(alias, _MISSING)
    else:
        payload = _MISSING
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


def _effective_response_model(explicit: Any, endpoint: Callable[..., Any]) -> Any:
    """Mirror FastAPI: unset `response_model` falls back to the return annotation.

    Explicit `response_model=None` disables filtering; `-> None` / missing
    annotation means no model.
    """
    if explicit is not _PARAM_UNSET:
        return explicit
    try:
        sig = _signature(endpoint) or inspect.signature(endpoint)
        raw = sig.return_annotation
    except (TypeError, ValueError):
        return None
    if raw is inspect.Signature.empty:
        return None
    ann = _ann_of(endpoint, "return", raw)
    if ann is None or ann is type(None):
        return None
    if ann is inspect.Signature.empty:
        return None
    return ann


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
        response_model_include: Any = None,
        response_model_exclude: Any = None,
        response_model_by_alias: bool = True,
        response_model_exclude_unset: bool = False,
        response_model_exclude_defaults: bool = False,
        response_model_exclude_none: bool = False,
        operation_id: Optional[str] = None,
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
        self.response_model_include = response_model_include
        self.response_model_exclude = response_model_exclude
        self.response_model_by_alias = response_model_by_alias
        self.response_model_exclude_unset = response_model_exclude_unset
        self.response_model_exclude_defaults = response_model_exclude_defaults
        self.response_model_exclude_none = response_model_exclude_none
        self.operation_id = operation_id
        self.tags = tags or []
        self.summary = summary
        self.description = description
        self.dependencies = dependencies or []
        self.name = name or getattr(endpoint, "__name__", path)


# Models whose instances serialize identically with and without a
# revalidation round-trip (no validators/serializers that could transform).
_MODEL_PASSTHROUGH: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_MODEL_PASSTHROUGH_LOCK = threading.RLock()


def _model_dump_direct(model_cls: Any) -> bool:
    """True when `dump_json(instance)` == `dump_json(validate(instance))`."""
    try:
        with _MODEL_PASSTHROUGH_LOCK:
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
        with _MODEL_PASSTHROUGH_LOCK:
            _MODEL_PASSTHROUGH[model_cls] = ok
    except TypeError:
        pass
    return ok


def _serialize_response(result: Any, route: _RouteDef) -> Any:
    status_code = route.status_code
    response_model = route.response_model
    if isinstance(result, Response):
        return result
    if response_model is not None:
        dump_kw: dict[str, Any] = {
            "include": route.response_model_include,
            "exclude": route.response_model_exclude,
            "by_alias": route.response_model_by_alias,
            "exclude_unset": route.response_model_exclude_unset,
            "exclude_defaults": route.response_model_exclude_defaults,
            "exclude_none": route.response_model_exclude_none,
        }
        filtered = any(v is not None and v is not False for k, v in dump_kw.items() if k != "by_alias") \
            or dump_kw["by_alias"] is False
        try:
            if type(result) is response_model and _model_dump_direct(response_model) and not filtered:
                # Already a valid instance with no transforming hooks:
                # skip the revalidation round-trip, serialize directly.
                return Response(
                    content=_adapter(response_model).dump_json(result, **dump_kw),
                    status_code=status_code,
                    media_type="application/json",
                )
            validated = _adapter(response_model).validate_python(result)
            # One Rust pass straight to JSON bytes — no intermediate dict.
            return Response(
                content=_adapter(response_model).dump_json(validated, **dump_kw),
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


def _make_http_handler(route: _RouteDef, router_deps: list[Depends]) -> Callable[..., Any]:
    async def asgi_app(scope: Any, receive: Any, send: Any) -> None:
        # FastAPI's exact lifecycle: solve (yield enters) → endpoint →
        # build response → attach background → SEND (background runs) → and
        # only then close the stack (yield teardowns). Teardown therefore
        # runs after background work, never before the response is sent.
        request = Request(scope, receive, send)
        path_params = dict(request.path_params)
        query = dict(request.query_params)
        headers = {k.lower(): v for k, v in request.headers.items()}
        cookies = dict(request.cookies)
        # Body: keep raw bytes only. The single-model fast path validates
        # straight from bytes; anything else parses lazily (cached) on use.
        body_raw: Optional[bytes] = None
        form: Optional[dict[str, Any]] = None
        body_json: Any = None
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
        # FastAPI parity: detect malformed JSON *before* solving dependencies,
        # so a broken body 422s (json_invalid) even when auth would 401 first.
        if body_raw:
            try:
                body_json = _parsed_json_cached(request, body_raw)
            except _InvalidJson:
                raise RequestValidationError(
                    _json_invalid_error(body_raw),
                    body=body_raw.decode("utf-8", "replace"),
                )
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
            except (RequestValidationError, HTTPException):
                # Let Starlette's exception middleware dispatch registered app
                # handlers, including user overrides of these framework errors.
                raise
            try:
                if inspect.iscoroutinefunction(route.endpoint):
                    result = await route.endpoint(**kwargs)
                else:
                    result = route.endpoint(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result
            except (RequestValidationError, HTTPException):
                raise
            # SSE: generator endpoint + EventSourceResponse streams frames.
            if _is_sse_route(route) and (inspect.isasyncgen(result) or inspect.isgenerator(result)):
                await _with_background(_sse_response(result, route.status_code), cache)(scope, receive, send)
                return
            try:
                payload = _serialize_response(result, route)
            except Exception as exc:
                from .exceptions import ResponseValidationError

                if isinstance(exc, ResponseValidationError):
                    await JSONResponse({"detail": exc.errors()}, status_code=500)(scope, receive, send)
                    return
                raise
            if payload is None:
                await _with_background(Response(status_code=route.status_code), cache)(scope, receive, send)
                return
            if isinstance(payload, Response):
                await _with_background(payload, cache)(scope, receive, send)
                return
            await _with_background(JSONResponse(payload, status_code=route.status_code), cache)(scope, receive, send)

    # Name the handler for Starlette route introspection.
    asgi_app.__name__ = getattr(route.endpoint, "__name__", "handler")  # type: ignore[attr-defined]
    return asgi_app


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
        response_model: Any = _PARAM_UNSET,
        status_code: int = 200,
        response_class: Any = None,
        tags: Optional[list[str]] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        deprecated: bool = False,
        response_model_include: Any = None,
        response_model_exclude: Any = None,
        response_model_by_alias: bool = True,
        response_model_exclude_unset: bool = False,
        response_model_exclude_defaults: bool = False,
        response_model_exclude_none: bool = False,
        operation_id: Optional[str] = None,
        dependencies: Optional[list[Depends]] = None,
        name: Optional[str] = None,
        **_extra: Any,
    ) -> None:
        # Empty sub-path ("@router.post(\"\")") addresses the prefix itself —
        # no trailing slash gained (FastAPI idiom for collection routes).
        if not path:
            full = self.prefix or "/"
        else:
            full = self.prefix + (path if path.startswith("/") else "/" + path)
        rc = response_class if response_class is not None else self.default_response_class
        if rc is JSONResponse:
            rc = None  # default JSON rendering needs no marker
        self.routes.append(
            _RouteDef(
                full, endpoint, methods or ["GET"],
                response_model=_effective_response_model(response_model, endpoint),
                status_code=status_code,
                response_class=rc, deprecated=deprecated,
                response_model_include=response_model_include,
                response_model_exclude=response_model_exclude,
                response_model_by_alias=response_model_by_alias,
                response_model_exclude_unset=response_model_exclude_unset,
                response_model_exclude_defaults=response_model_exclude_defaults,
                response_model_exclude_none=response_model_exclude_none,
                operation_id=operation_id,
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
                response_model_include=r.response_model_include,
                response_model_exclude=r.response_model_exclude,
                response_model_by_alias=r.response_model_by_alias,
                response_model_exclude_unset=r.response_model_exclude_unset,
                response_model_exclude_defaults=r.response_model_exclude_defaults,
                response_model_exclude_none=r.response_model_exclude_none,
                operation_id=r.operation_id,
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
            # Our handler is already a full ASGI app (solve → send inside the
            # teardown stack, like FastAPI) — bypass Starlette's
            # request_response wrapper which expects (Request) -> Response.
            sr = StarletteRoute(r.path, lambda _req: Response(status_code=500),  # placeholder
                                methods=r.methods, name=r.name)
            # FastAPI parity: GET-only routes do not auto-serve HEAD.
            sr.methods = set(r.methods)
            sr.app = _make_http_handler(r, [])
            out.append(sr)
        for path, fn, name in self.websocket_routes:
            out.append(WebSocketRoute(path, _make_ws_handler(fn), name=name))
        return out


def _make_ws_handler(fn: Callable[..., Any]) -> Callable[[WebSocket], Coroutine[Any, Any, None]]:
    async def handler(ws: WebSocket) -> None:
        # The socket injects via _solve_fn_kwargs (WebSocket branch); every
        # other param (Query/Path/Depends/…) solves uniformly like HTTP.
        try:
            async with AsyncExitStack() as stack:
                cache: dict[int, Any] = {}
                kwargs = await _solve_fn_kwargs(
                    fn, request=ws, path_params=dict(ws.path_params),  # type: ignore[arg-type]
                    query=dict(ws.query_params),
                    headers={k.lower(): v for k, v in ws.headers.items()},
                    cookies=dict(ws.cookies), body_json=None, form=None,
                    cache=cache, stack=stack,
                )
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
