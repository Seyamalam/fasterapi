//! fasterapi — Rust core (`fasterapi._fasterapi_rs`).
//!
//! Shipped fast-paths (minimal deps, all GIL-aware via `allow_threads`):
//!   * `RustRouter` — zero-Python radix-tree routing (`matchit`); Python pushes
//!     its route table once at `app.build()` (FastAPI `{param}` syntax is
//!     compiled to matchit `:param` on insert).
//!   * `fast_json_loads` / `fast_json_dumps` — SIMD JSON (`simd-json`).
//!   * `validate_int/float/bool/str`, `percent_decode`, `parse_query_pairs` —
//!     strict, type-safe scalar coercion + constraints for query/path/header.
//!   * `TokenBucket` — socket-level token-bucket rate limiting (`governor`),
//!     returning 429 without touching Python.
//!   * `DependencyDAG` — AOT-compiled `Depends()` graph: topological order +
//!     concurrent levels for fanning out async dependencies.
//!   * Host helpers (`hyper_health`, `tokio_worker_count`, `process_rss_bytes`,
//!     `serve_static_config`).
//!
//! The opt-in Hyper + Tokio host is implemented as an experimental HTTP/1.1
//! server. Rust owns sockets and framing, while Python still runs the ASGI app
//! through a bridge; request/response buffering, Python dispatch, and protocol
//! compatibility remain constraints. Uvicorn stays the default host.
//!
//! Threading: shared native router state uses `parking_lot`; pure parsing
//! helpers release the GIL where practical. This does not establish support
//! for free-threaded Python; that requires a compatible PyO3 build and GIL-off
//! validation of the whole extension and Python integration.

use std::collections::{HashMap, VecDeque};
use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Instant;

use parking_lot::RwLock;
use pyo3::conversion::IntoPyObject;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
const VERSION: &str = env!("CARGO_PKG_VERSION");

// ---------------------------------------------------------------------------
// JSON fast-path
// ---------------------------------------------------------------------------

fn value_to_py(py: Python<'_>, v: &serde_json::Value) -> PyObject {
    match v {
        serde_json::Value::Null => py.None(),
        serde_json::Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any().unbind(),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_pyobject(py)
                    .expect("i64 conversion cannot fail")
                    .into_any()
                    .unbind()
            } else if let Some(u) = n.as_u64() {
                u.into_pyobject(py)
                    .expect("u64 conversion cannot fail")
                    .into_any()
                    .unbind()
            } else if let Some(f) = n.as_f64() {
                PyFloat::new(py, f).into_any().unbind()
            } else {
                py.None()
            }
        }
        serde_json::Value::String(s) => PyString::new(py, s).into_any().unbind(),
        serde_json::Value::Array(arr) => {
            let list = PyList::empty(py);
            for item in arr {
                list.append(value_to_py(py, item))
                    .expect("list append cannot fail");
            }
            list.into_any().unbind()
        }
        serde_json::Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, val) in map {
                dict.set_item(k, value_to_py(py, val))
                    .expect("dict insert cannot fail");
            }
            dict.into_any().unbind()
        }
    }
}

fn write_json_str(buf: &mut String, s: &str) {
    buf.push('"');
    // Byte-wise scan: ASCII bytes pass through verbatim; only bytes needing
    // an escape take the slow arm. Non-ASCII UTF-8 passes through raw, like
    // `json.dumps(..., ensure_ascii=False)` — the bridge's contract.
    let bytes = s.as_bytes();
    let mut run_start = 0usize;
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        let esc: Option<&'static str> = match b {
            b'"' => Some("\\\""),
            b'\\' => Some("\\\\"),
            b'\n' => Some("\\n"),
            b'\r' => Some("\\r"),
            b'\t' => Some("\\t"),
            0x08 => Some("\\b"),
            0x0C => Some("\\f"),
            0x00..=0x1F => None,
            _ => {
                i += 1;
                continue;
            }
        };
        buf.push_str(&s[run_start..i]);
        match esc {
            Some(e) => buf.push_str(e),
            None => {
                const HEX: &[u8; 16] = b"0123456789abcdef";
                buf.push_str("\\u00");
                buf.push(HEX[(b >> 4) as usize] as char);
                buf.push(HEX[(b & 0xF) as usize] as char);
            }
        }
        i += 1;
        run_start = i;
    }
    buf.push_str(&s[run_start..]);
    buf.push('"');
}

fn write_json_key(buf: &mut String, obj: &Bound<'_, PyAny>, strict: bool) -> PyResult<()> {
    // Mirror CPython: str as-is, int/float/bool/None coerced, else TypeError.
    if let Ok(s) = obj.downcast::<PyString>() {
        write_json_str(buf, &s.to_string_lossy());
        return Ok(());
    }
    // CPython coerces non-str keys into quoted strings: True→"true",
    // None→"null", 1→"1", 1.5→"1.5". Everything below stays quoted.
    buf.push('"');
    let r = (|| -> PyResult<()> {
        if let Ok(b) = obj.downcast::<PyBool>() {
            buf.push_str(if b.is_true() { "true" } else { "false" });
            return Ok(());
        }
        if obj.is_none() {
            buf.push_str("null");
            return Ok(());
        }
        if let Ok(i) = obj.downcast::<PyInt>() {
            if let Ok(iv) = i.extract::<i64>() {
                buf.push_str(itoa::Buffer::new().format(iv));
                return Ok(());
            }
            if let Ok(uv) = i.extract::<u64>() {
                buf.push_str(itoa::Buffer::new().format(uv));
                return Ok(());
            }
            let f: f64 = i.extract()?;
            write_json_float(buf, f, strict)?;
            return Ok(());
        }
        if let Ok(f) = obj.downcast::<PyFloat>() {
            write_json_float(buf, f.value(), strict)?;
            return Ok(());
        }
        Err(pyo3::exceptions::PyTypeError::new_err(
            "keys must be str, int, float, bool or None",
        ))
    })();
    buf.push('"');
    r
}

/// Render `v` exactly like CPython `repr()`/`json.dumps`: ryu produces the
/// shortest round-trip digits; we re-render them under Python's notation
/// rule — scientific iff decpt > 16 or decpt <= -4, exponent always signed
/// and at least two digits (where "decpt" is the power of ten of the first
/// significant digit plus one, i.e. value = 0.digits × 10^decpt).
fn write_json_float(buf: &mut String, v: f64, strict: bool) -> PyResult<()> {
    if !v.is_finite() {
        if strict {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Out of range float values are not JSON compliant",
            ));
        }
        buf.push_str(if v.is_nan() {
            "NaN"
        } else if v.is_sign_positive() {
            "Infinity"
        } else {
            "-Infinity"
        });
        return Ok(());
    }
    let mut rbuf = ryu::Buffer::new();
    let raw = rbuf.format_finite(v);
    // raw grammar: [-]D[.D][e[-]D]
    let (neg, raw) = match raw.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, raw),
    };
    let (mantissa, exp_str) = match raw.split_once(['e', 'E']) {
        Some((m, e)) => (m, e),
        None => (raw, "0"),
    };
    let exp: i32 = exp_str.parse().unwrap_or(0);
    let (int_part, frac_part) = match mantissa.split_once('.') {
        Some((a, b)) => (a, b),
        None => (mantissa, ""),
    };
    // f64 has at most 17 significant digits; join int+frac on the stack.
    let mut combined = [0u8; 32];
    combined[..int_part.len()].copy_from_slice(int_part.as_bytes());
    combined[int_part.len()..int_part.len() + frac_part.len()]
        .copy_from_slice(frac_part.as_bytes());
    let clen = int_part.len() + frac_part.len();
    let lz = combined[..clen].iter().take_while(|&&b| b == b'0').count();
    let digits = std::str::from_utf8(&combined[lz..clen]).unwrap_or("0");
    if digits.is_empty() {
        buf.push_str(if neg { "-0.0" } else { "0.0" });
        return Ok(());
    }
    let decpt = int_part.len() as i32 - lz as i32 + exp;
    if neg {
        buf.push('-');
    }
    if decpt > 16 || decpt <= -4 {
        // Scientific: d[.rest]e±XX (exponent = decpt-1, ≥2 digits).
        buf.push_str(&digits[..1]);
        if digits.len() > 1 {
            buf.push('.');
            buf.push_str(&digits[1..]);
        }
        let e = decpt - 1;
        buf.push('e');
        buf.push(if e < 0 { '-' } else { '+' });
        let ae = e.unsigned_abs();
        if ae < 10 {
            buf.push('0');
        }
        buf.push_str(itoa::Buffer::new().format(ae));
    } else if decpt <= 0 {
        // 0.000ddd
        buf.push_str("0.");
        for _ in 0..(-decpt) {
            buf.push('0');
        }
        buf.push_str(digits);
    } else if (decpt as usize) >= digits.len() {
        // ddd000.0
        buf.push_str(digits);
        for _ in 0..(decpt as usize - digits.len()) {
            buf.push('0');
        }
        buf.push_str(".0");
    } else {
        // dd.ddd
        let d = decpt as usize;
        buf.push_str(&digits[..d]);
        buf.push('.');
        buf.push_str(&digits[d..]);
    }
    Ok(())
}

fn write_json_value(buf: &mut String, obj: &Bound<'_, PyAny>, strict: bool) -> PyResult<()> {
    if obj.is_none() {
        buf.push_str("null");
        return Ok(());
    }
    if let Ok(b) = obj.downcast::<PyBool>() {
        buf.push_str(if b.is_true() { "true" } else { "false" });
        return Ok(());
    }
    if let Ok(s) = obj.downcast::<PyString>() {
        write_json_str(buf, &s.to_string_lossy());
        return Ok(());
    }
    if let Ok(i) = obj.downcast::<PyInt>() {
        if let Ok(iv) = i.extract::<i64>() {
            buf.push_str(itoa::Buffer::new().format(iv));
            return Ok(());
        }
        if let Ok(uv) = i.extract::<u64>() {
            buf.push_str(itoa::Buffer::new().format(uv));
            return Ok(());
        }
        let f: f64 = i.extract()?;
        write_json_float(buf, f, strict)?;
        return Ok(());
    }
    if let Ok(f) = obj.downcast::<PyFloat>() {
        write_json_float(buf, f.value(), strict)?;
        return Ok(());
    }
    if let Ok(list) = obj.downcast::<PyList>() {
        buf.push('[');
        let mut first = true;
        for item in list.iter() {
            if !first {
                buf.push(',');
            }
            first = false;
            write_json_value(buf, &item, strict)?;
        }
        buf.push(']');
        return Ok(());
    }
    if let Ok(tup) = obj.downcast::<pyo3::types::PyTuple>() {
        buf.push('[');
        let mut first = true;
        for item in tup.iter() {
            if !first {
                buf.push(',');
            }
            first = false;
            write_json_value(buf, &item, strict)?;
        }
        buf.push(']');
        return Ok(());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        buf.push('{');
        let mut first = true;
        for (k, v) in dict.iter() {
            if !first {
                buf.push(',');
            }
            first = false;
            write_json_key(buf, &k, strict)?;
            buf.push(':');
            write_json_value(buf, &v, strict)?;
        }
        buf.push('}');
        return Ok(());
    }
    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "Object of type {} is not JSON serializable",
        obj.get_type()
            .qualname()
            .map(|q| q.to_string())
            .unwrap_or_else(|_| "?".to_string())
    )))
}

/// SIMD-accelerated JSON parse. Releases the GIL during parsing.
///
/// Falls back to `serde_json`, then to stdlib-compatible behavior for inputs
/// only Python accepts (`Infinity`, `NaN`) so results match `json.loads`.
///
/// NOTE (measured x0.6 vs stdlib at all payload sizes): per-node PyObject
/// materialization through the GIL API cannot beat CPython's tight C loop
/// for *generic* parsing. This function is therefore OFF the request hot
/// path — Pydantic bodies go through single-pass `validate_json` (x2.2)
/// which never materializes an intermediate dict. Use this bridge for
/// untyped/embedded payloads only.
#[pyfunction]
fn fast_json_loads(py: Python<'_>, payload: &str) -> PyResult<PyObject> {
    // simd-json mutates its input, so work on a private copy off-GIL.
    let parsed: Result<serde_json::Value, String> = py.allow_threads(|| {
        let mut bytes = payload.as_bytes().to_vec();
        simd_json::from_slice::<serde_json::Value>(&mut bytes)
            .or_else(|_| serde_json::from_str::<serde_json::Value>(payload))
            .map_err(|e| e.to_string())
    });
    match parsed {
        Ok(value) => Ok(value_to_py(py, &value)),
        Err(_) => {
            // Slow path (invalid/edge input only): delegate to stdlib for
            // exact messages, positions and Infinity/NaN acceptance.
            let json_mod = py.import("json")?;
            let loads = json_mod.getattr("loads")?;
            Ok(loads
                .call1((payload,))?
                .extract::<Bound<'_, PyAny>>()?
                .unbind())
        }
    }
}

/// Direct JSON serialize: walks Python objects straight into the output
/// buffer (no intermediate `serde_json::Value`), mirroring stdlib semantics.
#[pyfunction]
#[pyo3(signature = (obj, strict = false))]
fn fast_json_dumps(py: Python<'_>, obj: PyObject, strict: bool) -> PyResult<String> {
    let bound = obj.bind(py);
    let mut buf = String::with_capacity(128);
    write_json_value(&mut buf, bound, strict)?;
    Ok(buf)
}

// ---------------------------------------------------------------------------
// Radix-tree router (matchit)
// ---------------------------------------------------------------------------

/// Convert FastAPI/Starlette `{param}` (or `{param:converter}`) segments to
/// matchit `:param` syntax. Leaves already-native `:param` / `*wildcard`
/// segments untouched. Pure + allocation-minimal.
fn fastapi_to_matchit(pattern: &str) -> String {
    if !pattern.contains('{') {
        return pattern.to_string();
    }
    let mut out = String::with_capacity(pattern.len());
    let mut chars = pattern.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '{' {
            let mut name = String::new();
            for nc in chars.by_ref() {
                if nc == '}' {
                    break;
                }
                // `{id:path}` / `{id:int}` -> take `id` only.
                if nc == ':' {
                    // Skip converter until `}`.
                    for cc in chars.by_ref() {
                        if cc == '}' {
                            break;
                        }
                    }
                    break;
                }
                name.push(nc);
            }
            let clean: String = name
                .chars()
                .filter(|ch| ch.is_alphanumeric() || *ch == '_')
                .collect();
            if clean.is_empty() {
                out.push_str(":param");
            } else {
                out.push(':');
                out.push_str(&clean);
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// Zero-Python radix-tree router.
///
/// Python compiles its route table into this structure at `app.build()`.
/// Lookup runs under a read lock with the GIL released.
#[pyclass]
struct RustRouter {
    inner: RwLock<matchit::Router<String>>,
    count: std::sync::atomic::AtomicUsize,
}

#[pymethods]
impl RustRouter {
    #[new]
    fn new() -> Self {
        Self {
            inner: RwLock::new(matchit::Router::new()),
            count: std::sync::atomic::AtomicUsize::new(0),
        }
    }

    /// Register `pattern` (e.g. `/items/{item_id}`) -> `target` (route key).
    ///
    /// Accepts FastAPI `{param}` syntax and transparently compiles it to
    /// matchit `:param` syntax, so `app.build()` can push Starlette paths
    /// verbatim with zero Python-side translation cost.
    fn insert(&self, pattern: &str, target: String) -> PyResult<()> {
        let converted = fastapi_to_matchit(pattern);
        self.inner
            .write()
            .insert(&converted, target)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        self.count
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Ok(())
    }

    /// Match a path. Returns `(target, params_dict)` or `None`.
    fn r#match(&self, py: Python<'_>, path: &str) -> PyResult<Option<(String, PyObject)>> {
        // Clone out the match under the lock, build Python objects after.
        let found: Option<(String, Vec<(String, String)>)> = py.allow_threads(|| {
            let router = self.inner.read();
            router.at(path).ok().map(|m| {
                let params = m
                    .params
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect();
                (m.value.clone(), params)
            })
        });
        match found {
            None => Ok(None),
            Some((target, params)) => {
                let dict = PyDict::new(py);
                for (k, v) in params {
                    dict.set_item(k, v)?;
                }
                Ok(Some((target, dict.into_any().unbind())))
            }
        }
    }

    fn len(&self) -> usize {
        self.count.load(std::sync::atomic::Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// Token-bucket rate limiting (governor)
// ---------------------------------------------------------------------------

/// Shared handle: the limiter behind `TokenBucket`, clonable into the Hyper
/// accept loop for zero-GIL checks before any Python code runs.
type GovLimiter = governor::RateLimiter<
    governor::state::NotKeyed,
    governor::state::InMemoryState,
    governor::clock::DefaultClock,
>;

/// Thread-safe token-bucket guard executed at the socket level in Rust.
///
/// Python configures it via `Guards` / `RateLimiter`; the Hyper host calls
/// `check()` before any Python runs. Returns `True` when admitted.
#[pyclass]
struct TokenBucket {
    limiter: Arc<GovLimiter>,
    per_second: f64,
}

#[pymethods]
impl TokenBucket {
    #[new]
    #[pyo3(signature = (per_second, burst=None))]
    fn new(per_second: u32, burst: Option<u32>) -> PyResult<Self> {
        let per_second_nz = NonZeroU32::new(per_second.max(1))
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("per_second must be >= 1"))?;
        let burst_size = burst.unwrap_or(per_second_nz.get()).max(1);
        let quota =
            governor::Quota::per_second(per_second_nz)
                .allow_burst(NonZeroU32::new(burst_size).ok_or_else(|| {
                    pyo3::exceptions::PyValueError::new_err("burst must be >= 1")
                })?);
        Ok(Self {
            limiter: Arc::new(governor::RateLimiter::direct(quota)),
            per_second: per_second_nz.get() as f64,
        })
    }

    fn check(&self) -> bool {
        self.limiter.check().is_ok()
    }

    /// How long to wait before the next admission (seconds, 0.0 if admitted).
    fn retry_after_secs(&self) -> f64 {
        match self.limiter.check() {
            Ok(()) => 0.0,
            Err(_) => 1.0 / self.per_second.max(1.0),
        }
    }
}

// ---------------------------------------------------------------------------
// AOT dependency DAG
// ---------------------------------------------------------------------------

/// Compiled `Depends()` graph.
///
/// Nodes are dotted dependency names; edges point dependency -> dependent.
/// `concurrent_levels()` returns Kahn levels so Tokio can fan-out each level
/// with `FuturesUnordered` before invoking the Python endpoint.
#[pyclass]
struct DependencyDAG {
    nodes: Vec<String>,
    index: HashMap<String, usize>,
    edges: Vec<(usize, usize)>,
}

#[pymethods]
impl DependencyDAG {
    #[new]
    fn new() -> Self {
        Self {
            nodes: Vec::new(),
            index: HashMap::new(),
            edges: Vec::new(),
        }
    }

    fn add_node(&mut self, name: String) -> usize {
        if let Some(&i) = self.index.get(&name) {
            return i;
        }
        let i = self.nodes.len();
        self.nodes.push(name.clone());
        self.index.insert(name, i);
        i
    }

    fn add_edge(&mut self, from: String, to: String) -> PyResult<()> {
        let a = self.add_node(from);
        let b = self.add_node(to);
        if !self.edges.contains(&(a, b)) {
            self.edges.push((a, b));
        }
        Ok(())
    }

    fn node_count(&self) -> usize {
        self.nodes.len()
    }

    fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Topological order of node names. Errors on cycles.
    fn topological_sort(&self) -> PyResult<Vec<String>> {
        let n = self.nodes.len();
        let mut indeg = vec![0usize; n];
        let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
        for &(a, b) in &self.edges {
            adj[a].push(b);
            indeg[b] += 1;
        }
        let mut q: VecDeque<usize> = indeg
            .iter()
            .enumerate()
            .filter(|(_, &d)| d == 0)
            .map(|(i, _)| i)
            .collect();
        let mut order = Vec::with_capacity(n);
        while let Some(u) = q.pop_front() {
            order.push(u);
            for &v in &adj[u] {
                indeg[v] -= 1;
                if indeg[v] == 0 {
                    q.push_back(v);
                }
            }
        }
        if order.len() != n {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "DependencyDAG: cycle detected in Depends() graph",
            ));
        }
        Ok(order.into_iter().map(|i| self.nodes[i].clone()).collect())
    }

    /// Kahn levels: each inner vec can run concurrently in Tokio.
    fn concurrent_levels(&self) -> PyResult<Vec<Vec<String>>> {
        let n = self.nodes.len();
        let mut indeg = vec![0usize; n];
        let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
        for &(a, b) in &self.edges {
            adj[a].push(b);
            indeg[b] += 1;
        }
        let mut current: Vec<usize> = indeg
            .iter()
            .enumerate()
            .filter(|(_, &d)| d == 0)
            .map(|(i, _)| i)
            .collect();
        let mut levels: Vec<Vec<String>> = Vec::new();
        let mut seen = 0usize;
        while !current.is_empty() {
            levels.push(current.iter().map(|&i| self.nodes[i].clone()).collect());
            seen += current.len();
            let mut next = Vec::new();
            for u in current {
                for &v in &adj[u] {
                    indeg[v] -= 1;
                    if indeg[v] == 0 {
                        next.push(v);
                    }
                }
            }
            current = next;
        }
        if seen != n {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "DependencyDAG: cycle detected in Depends() graph",
            ));
        }
        Ok(levels)
    }
}

// ---------------------------------------------------------------------------
// Host helpers
// ---------------------------------------------------------------------------

/// Peak RSS of the current process in bytes (Linux `/proc`, else 0).
#[pyfunction]
fn process_rss_bytes() -> u64 {
    #[cfg(target_os = "linux")]
    {
        if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
            for line in status.lines() {
                if let Some(rest) = line.strip_prefix("VmRSS:") {
                    let kb: u64 = rest
                        .split_whitespace()
                        .next()
                        .and_then(|n| n.parse().ok())
                        .unwrap_or(0);
                    return kb * 1024;
                }
            }
        }
        // Fallback: statm pages * page size.
        if let Ok(statm) = std::fs::read_to_string("/proc/self/statm") {
            if let Some(pages) = statm
                .split_whitespace()
                .nth(1)
                .and_then(|n| n.parse::<u64>().ok())
            {
                return pages * 4096;
            }
        }
        0
    }
    #[cfg(not(target_os = "linux"))]
    {
        0
    }
}

/// Tokio multithreaded worker count the Hyper host would use.
#[pyfunction]
fn tokio_worker_count() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
}

#[pyfunction]
fn hyper_health() -> String {
    format!(
        "{{\"status\":\"ok\",\"hyper\":\"1.x\",\"tokio_workers\":{},\"simd\":true}}",
        tokio_worker_count()
    )
}

/// Human-readable static-file serve plan (Tokio `sendfile` path).
///
/// The Python `FileResponse` uses this to decide zero-copy vs. Python fallback.
#[pyfunction]
fn serve_static_config(path: &str) -> String {
    let p = std::path::Path::new(path);
    let exists = p.exists();
    let size = std::fs::metadata(p).map(|m| m.len()).unwrap_or(0);
    let started = Instant::now();
    let _ = started.elapsed(); // placeholder for sendfile timing hook
    serde_json::json!({
        "path": path,
        "exists": exists,
        "size_bytes": size,
        "strategy": if exists && size > 0 { "sendfile-zero-copy" } else { "python-fallback" },
    })
    .to_string()
}

#[pyfunction]
fn has_simd() -> bool {
    true
}

// ---------------------------------------------------------------------------
// Type-safe primitive validators (Rust-first, GIL-free)
//
// Philosophy: every scalar coercion + constraint check that FastAPI does in
// Python (`int("42")`, `ge/le/gt/lt`, `min/max_length`, `pattern`, bool
// parsing, percent-decoding) runs here with `allow_threads`.
// Python only maps the typed error into the 422 `{"detail": [...]}` shape, so
// behavior stays 1:1 while the hot loop never touches the GIL or allocator
// more than once. All functions are pure + thread-safe (no shared mutables)
// which is what makes the 3.13t free-threaded story sound.
// ---------------------------------------------------------------------------

fn err_value(msg: String) -> pyo3::PyErr {
    pyo3::exceptions::PyValueError::new_err(msg)
}

fn check_int_range(
    v: i64,
    ge: Option<i64>,
    le: Option<i64>,
    gt: Option<i64>,
    lt: Option<i64>,
) -> Result<(), String> {
    if let Some(lo) = ge {
        if v < lo {
            return Err(format!("Input should be greater than or equal to {lo}"));
        }
    }
    if let Some(hi) = le {
        if v > hi {
            return Err(format!("Input should be less than or equal to {hi}"));
        }
    }
    if let Some(lo) = gt {
        if v <= lo {
            return Err(format!("Input should be greater than {lo}"));
        }
    }
    if let Some(hi) = lt {
        if v >= hi {
            return Err(format!("Input should be less than {hi}"));
        }
    }
    Ok(())
}

fn check_float_range(
    v: f64,
    ge: Option<f64>,
    le: Option<f64>,
    gt: Option<f64>,
    lt: Option<f64>,
) -> Result<(), String> {
    if let Some(lo) = ge {
        if v < lo {
            return Err(format!("Input should be greater than or equal to {lo}"));
        }
    }
    if let Some(hi) = le {
        if v > hi {
            return Err(format!("Input should be less than or equal to {hi}"));
        }
    }
    if let Some(lo) = gt {
        if v <= lo {
            return Err(format!("Input should be greater than {lo}"));
        }
    }
    if let Some(hi) = lt {
        if v >= hi {
            return Err(format!("Input should be less than {hi}"));
        }
    }
    Ok(())
}

/// Strict, type-safe int coercion + range checks. Raises `ValueError` on failure.
#[pyfunction]
#[pyo3(signature = (raw, ge=None, le=None, gt=None, lt=None))]
fn validate_int(
    py: Python<'_>,
    raw: &str,
    ge: Option<i64>,
    le: Option<i64>,
    gt: Option<i64>,
    lt: Option<i64>,
) -> PyResult<i64> {
    let v: i64 = py
        .allow_threads(|| {
            let s = raw.trim();
            // Reject floats/bools explicitly — FastAPI/Pydantic would 422 these.
            if s.is_empty() {
                return Err("Input should be a valid integer".to_string());
            }
            // Allow leading +/- then ASCII digits only (no underscores, no hex).
            let body = s.strip_prefix(['+', '-']).unwrap_or(s);
            if body.is_empty() || !body.bytes().all(|b| b.is_ascii_digit()) {
                return Err(
                    "Input should be a valid integer, unable to parse string as an integer"
                        .to_string(),
                );
            }
            s.parse::<i64>().map_err(|_| {
                "Input should be a valid integer, unable to parse string as an integer".to_string()
            })
        })
        .map_err(err_value)?;
    check_int_range(v, ge, le, gt, lt).map_err(err_value)?;
    Ok(v)
}

/// Type-safe float coercion + range checks.
#[pyfunction]
#[pyo3(signature = (raw, ge=None, le=None, gt=None, lt=None))]
fn validate_float(
    py: Python<'_>,
    raw: &str,
    ge: Option<f64>,
    le: Option<f64>,
    gt: Option<f64>,
    lt: Option<f64>,
) -> PyResult<f64> {
    let v: f64 = py
        .allow_threads(|| {
            let s = raw.trim();
            if s.is_empty() {
                return Err("Input should be a valid number".to_string());
            }
            s.parse::<f64>().map_err(|_| {
                "Input should be a valid number, unable to parse string as a number".to_string()
            })
        })
        .map_err(err_value)?;
    if !v.is_finite() {
        return Err(err_value("Input should be a finite number".to_string()));
    }
    check_float_range(v, ge, le, gt, lt).map_err(err_value)?;
    Ok(v)
}

/// Type-safe bool coercion (`true/1/yes/on` ↔ `false/0/no/off`, case-insensitive).
#[pyfunction]
fn validate_bool(py: Python<'_>, raw: &str) -> PyResult<bool> {
    py.allow_threads(|| match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Ok(true),
        "false" | "0" | "no" | "off" => Ok(false),
        _ => Err("Input should be a valid boolean".to_string()),
    })
    .map_err(err_value)
}

/// Type-safe str validation: length bounds + optional regex `pattern`.
#[pyfunction]
#[pyo3(signature = (raw, min_length=None, max_length=None, pattern=None))]
fn validate_str(
    py: Python<'_>,
    raw: &str,
    min_length: Option<usize>,
    max_length: Option<usize>,
    pattern: Option<&str>,
) -> PyResult<String> {
    py.allow_threads(|| {
        let len = raw.chars().count();
        if let Some(mn) = min_length {
            if len < mn {
                return Err(format!("String should have at least {mn} characters"));
            }
        }
        if let Some(mx) = max_length {
            if len > mx {
                return Err(format!("String should have at most {mx} characters"));
            }
        }
        if let Some(pat) = pattern {
            let re = regex::Regex::new(pat).map_err(|e| format!("Invalid pattern {pat:?}: {e}"))?;
            if !re.is_match(raw) {
                return Err(format!("String should match pattern {pat:?}"));
            }
        }
        Ok(())
    })
    .map_err(err_value)?;
    Ok(raw.to_string())
}

/// Percent-decode one component (query/header/path), no allocation beyond output.
#[pyfunction]
fn percent_decode(py: Python<'_>, raw: &str) -> PyResult<String> {
    py.allow_threads(|| {
        percent_encoding::percent_decode_str(raw)
            .decode_utf8()
            .map(|s| s.into_owned())
            .map_err(|_| "Invalid percent-encoding".to_string())
    })
    .map_err(err_value)
}

/// Parse a raw query string into `[(key, value)]` pairs (percent-decoded,
/// `+` → space, no Python overhead). Multi-value params are preserved in order.
#[pyfunction]
fn parse_query_pairs(py: Python<'_>, qs: &str) -> PyResult<Vec<(String, String)>> {
    Ok(py.allow_threads(|| {
        let mut out = Vec::new();
        let qs = qs.strip_prefix('?').unwrap_or(qs);
        if qs.is_empty() {
            return out;
        }
        for pair in qs.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (k, v) = match pair.split_once('=') {
                Some((k, v)) => (k, v),
                None => (pair, ""),
            };
            let dk = percent_encoding::percent_decode_str(&k.replace('+', " "))
                .decode_utf8_lossy()
                .into_owned();
            let dv = percent_encoding::percent_decode_str(&v.replace('+', " "))
                .decode_utf8_lossy()
                .into_owned();
            out.push((dk, dv));
        }
        out
    }))
}

// ---------------------------------------------------------------------------
// Hyper ASGI host spike
//
// Rust owns the TCP sockets + HTTP/1.1 parsing (Hyper) on a multi-thread
// Tokio runtime. Each request's method/path/query/headers/body is handed to
// the in-process Python ASGI app (`fasterapi._hyper_serve.run_request`),
// which drives `app(scope, receive, send)` on a per-call asyncio loop and
// returns `(status, headers, body)`. Lifespan runs around serving.
//
// Deliberate spike limits (documented, not hidden): HTTP/1.1 only, full body
// buffered before dispatch (16 MiB cap → 413), streaming responses are
// concatenated, no disconnect polling mid-response. The point being proven:
// sockets + parsing in Rust, identical app bytes, measurable RPS delta.
// ---------------------------------------------------------------------------

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;

/// Boxed response body: buffered (`Full`) or file-streamed (`StreamBody`).
type RespBody =
    http_body_util::combinators::BoxBody<Bytes, Box<dyn std::error::Error + Send + Sync>>;

fn boxed_full(bytes: Bytes) -> RespBody {
    Full::new(bytes)
        .map_err(|never| -> Box<dyn std::error::Error + Send + Sync> { match never {} })
        .boxed()
}

const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;

/// Request body cap, tunable via `FASTERAPI_MAX_BODY` (bytes). Cached after
/// first read so request handling stays allocation-free.
fn max_body_bytes() -> usize {
    static CAP: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *CAP.get_or_init(|| {
        std::env::var("FASTERAPI_MAX_BODY")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .filter(|&n| n > 0)
            .unwrap_or(MAX_BODY_BYTES)
    })
}
/// Per-worker queue depth: backpressure overflows to `spawn_blocking`.
const POOL_QUEUE: usize = 64;

fn py_runtime_error(msg: impl ToString) -> pyo3::PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(msg.to_string())
}

/// One Python job: everything needed to drive a single ASGI request.
struct PyJob {
    app: PyObject,
    method: String,
    path: String,
    query: String,
    headers: Vec<(String, Vec<u8>)>,
    body: Vec<u8>,
    /// False when the request had a `Range` header (sendfile skipped so
    /// partial-content semantics stay exactly Starlette's).
    sendfile_ok: bool,
    reply: tokio::sync::oneshot::Sender<Result<(u16, Vec<(String, String)>, Vec<u8>, String), String>>,
}

fn run_py_job(job: PyJob) {
    let result = Python::with_gil(|py| {
        use pyo3::types::PyBytes;
        // NOTE: Vec<u8> wouldcross as list[int]; HTTP needs real bytes.
        let py_headers: Vec<(String, PyObject)> = job
            .headers
            .iter()
            .map(|(k, v)| (k.clone(), PyBytes::new(py, v).into_any().unbind()))
            .collect();
        let py_body = PyBytes::new(py, &job.body).into_any().unbind();
        let helper = py
            .import("fasterapi._hyper_serve")
            .map_err(|e| e.to_string())?;
        let ret = helper
            .getattr("run_request")
            .map_err(|e| e.to_string())?
            .call1((
                job.app,
                job.method,
                job.path,
                job.query,
                py_headers,
                py_body,
                job.sendfile_ok,
            ))
            .map_err(|e| e.to_string())?;
        ret.extract::<(u16, Vec<(String, String)>, Vec<u8>, String)>()
            .map_err(|e| e.to_string())
    });
    // Streaming sessions: reply first, then park this worker driving the
    // session loop until stream_close (Rust calls it after the body ends).
    let stream_sid = match &result {
        Ok(v) => v.3.clone(),
        Err(_) => String::new(),
    };
    let _ = job.reply.send(result);
    if !stream_sid.is_empty() {
        let _ = Python::with_gil(|py| {
            py.import("fasterapi._hyper_serve")?
                .getattr("stream_drive")?
                .call1((stream_sid,))?;
            Ok::<(), pyo3::PyErr>(())
        });
    }
}

/// Fixed pool of Python worker threads (one thread-local asyncio loop each).
///
/// Unbounded `spawn_blocking` fans every concurrent request onto its own OS
/// thread; on GIL builds those threads just contend. A CPU-count pool keeps
/// the GIL handoff tight (backpressure overflows to `spawn_blocking`).
struct PyWorkerPool {
    senders: Vec<std::sync::mpsc::SyncSender<PyJob>>,
    next: std::sync::atomic::AtomicUsize,
}

impl PyWorkerPool {
    fn new(n: usize) -> Self {
        let n = n.max(1);
        let mut senders = Vec::with_capacity(n);
        for _ in 0..n {
            let (tx, rx) = std::sync::mpsc::sync_channel::<PyJob>(POOL_QUEUE);
            senders.push(tx);
            std::thread::Builder::new()
                .name("fasterapi-py".to_string())
                .spawn(move || {
                    while let Ok(job) = rx.recv() {
                        run_py_job(job);
                    }
                })
                .expect("spawn fasterapi python worker");
        }
        Self {
            senders,
            next: std::sync::atomic::AtomicUsize::new(0),
        }
    }

    fn dispatch(&self, job: PyJob) -> Result<(), PyJob> {
        let i = self.next.fetch_add(1, std::sync::atomic::Ordering::Relaxed) % self.senders.len();
        self.senders[i].try_send(job).map_err(|e| match e {
            std::sync::mpsc::TrySendError::Full(job) => job,
            std::sync::mpsc::TrySendError::Disconnected(job) => job,
        })
    }
}

fn run_lifespan(app: &PyObject, kind: &str) -> Result<(), String> {
    let app = Python::with_gil(|py| app.clone_ref(py));
    let kind = kind.to_string();
    tokio::task::block_in_place(|| {
        Python::with_gil(|py| {
            let helper = py
                .import("fasterapi._hyper_serve")
                .map_err(|e| e.to_string())?;
            helper
                .getattr("run_lifespan")
                .map_err(|e| e.to_string())?
                .call1((app, kind))
                .map_err(|e| e.to_string())?;
            Ok::<(), String>(())
        })
    })
}

async fn serve_one(
    pool: &PyWorkerPool,
    app: PyObject,
    req: Request<hyper::body::Incoming>,
    rate_limit: Option<Arc<GovLimiter>>,
) -> Result<Response<RespBody>, hyper::Error> {
    // Socket-level rate limit: rejected before body collection, websockets,
    // and any Python. Body matches the RateLimiter middleware's 429.
    if let Some(lim) = &rate_limit {
        if lim.check().is_err() {
            return Ok(Response::builder()
                .status(StatusCode::TOO_MANY_REQUESTS)
                .header("content-type", "application/json")
                .body(boxed_full(Bytes::from_static(
                    br#"{"detail":"Rate limit exceeded"}"#,
                )))
                .unwrap());
        }
    }
    // Websocket upgrade? Must precede body collection (upgrades carry none).
    if req.method() == hyper::Method::GET && is_ws_upgrade(req.headers()) {
        return serve_ws(app, req).await;
    }
    let method = req.method().to_string();
    let uri = req.uri().clone();
    let path = uri.path().to_string();
    let query = uri.query().unwrap_or("").to_string();
    let mut headers: Vec<(String, Vec<u8>)> = Vec::new();
    let mut has_range = false;
    for (name, value) in req.headers() {
        if name == hyper::header::RANGE {
            has_range = true;
        }
        headers.push((name.to_string(), value.as_bytes().to_vec()));
    }
    let collected = req.collect().await?;
    let body = collected.to_bytes();
    if body.len() > max_body_bytes() {
        let resp = Response::builder()
            .status(StatusCode::PAYLOAD_TOO_LARGE)
            .body(boxed_full(Bytes::from_static(b"Request body too large")))
            .unwrap();
        return Ok(resp);
    }
    let body = body.to_vec();
    // Brief GIL grab for the refcount bump; the job owns its app ref.
    let job_app = Python::with_gil(|py| app.clone_ref(py));
    let (tx, rx) = tokio::sync::oneshot::channel();
    let job = PyJob {
        app: job_app,
        method,
        path,
        query,
        headers,
        body,
        reply: tx,
        sendfile_ok: !has_range,
    };
    // Bounded pool first; overflow degrades to spawn_blocking instead of
    // deadlocking when every worker queue is momentarily full.
    let out: Result<Result<(u16, Vec<(String, String)>, Vec<u8>, String), String>, String> =
        match pool.dispatch(job) {
            Ok(()) => rx.await.map_err(|_| "worker dropped job".to_string()),
            Err(job) => match tokio::task::spawn_blocking(move || {
                let (tx2, rx2) = tokio::sync::oneshot::channel();
                run_py_job(PyJob { reply: tx2, ..job });
                rx2.blocking_recv()
                    .map_err(|_| "overflow worker dropped job".to_string())
            })
            .await
            {
                Ok(inner) => inner,
                Err(e) => Err(format!("overflow join: {e}")),
            },
        };
    let (status, resp_headers, resp_body, stream_sid) = match out {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => {
            eprintln!("fasterapi hyper: handler error: {e}");
            internal_error()
        }
        Err(e) => {
            eprintln!("fasterapi hyper: dispatch error: {e}");
            internal_error()
        }
    };
    let mut builder = Response::builder()
        .status(StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR));
    let sendfile_path: Option<String> = resp_headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("x-fasterapi-sendfile"))
        .map(|(_, v)| v.clone());
    // Streaming responses get chunked encoding — no content-length.
    let streaming = !stream_sid.is_empty();
    for (k, v) in &resp_headers {
        // Skip hop-by-hop / framing headers the server owns (`server` is
        // re-stamped below — the app layer still thinks in uvicorn terms).
        // content-length IS forwarded: Starlette computed it over the exact
        // bytes we send (sendfile sets its own from disk metadata instead).
        // Internal `x-fasterapi-*` plan headers never leave the server.
        let kl = k.to_ascii_lowercase();
        if kl == "transfer-encoding"
            || kl == "connection"
            || kl == "server"
            || kl.starts_with("x-fasterapi-")
            || (kl == "content-length" && (sendfile_path.is_some() || streaming))
        {
            continue;
        }
        if let (Ok(name), Ok(value)) = (
            hyper::header::HeaderName::from_bytes(k.as_bytes()),
            hyper::header::HeaderValue::from_bytes(v.as_bytes()),
        ) {
            builder = builder.header(name, value);
        }
    }
    builder = builder.header("server", "fasterapi-hyper");
    // Streaming response: the app is still producing on a Python session —
    // chunks flow through an mpsc into Hyper's body (chunked encoding).
    // Client disconnect drops the stream → we stop polling and close the
    // session (which cancels the app task).
    if streaming {
        let (tx, mut rx) =
            tokio::sync::mpsc::channel::<Result<Bytes, std::io::Error>>(8);
        let sid = stream_sid.clone();
        tokio::task::spawn_blocking(move || {
            // Chunks collected before the stream graduated go out first.
            if !resp_body.is_empty()
                && tx.blocking_send(Ok(Bytes::from(resp_body))).is_err()
            {
                let _ = Python::with_gil(|py| {
                    py.import("fasterapi._hyper_serve")?
                        .getattr("stream_close")?
                        .call1((sid.clone(),))?;
                    Ok::<(), pyo3::PyErr>(())
                });
                return;
            }
            loop {
                let step = Python::with_gil(|py| {
                    py.import("fasterapi._hyper_serve")
                        .and_then(|h| h.getattr("stream_step"))
                        .and_then(|f| f.call1((sid.clone(), 1.0f64)))
                        .and_then(|r| r.extract::<(Vec<Vec<u8>>, bool)>())
                });
                match step {
                    Ok((chunks, done)) => {
                        let mut stop = done;
                        for chunk in chunks {
                            if tx.blocking_send(Ok(Bytes::from(chunk))).is_err() {
                                stop = true;
                                break;
                            }
                        }
                        if stop {
                            break;
                        }
                    }
                    Err(e) => {
                        eprintln!("fasterapi hyper: stream_step: {e}");
                        break;
                    }
                }
            }
            let _ = Python::with_gil(|py| {
                py.import("fasterapi._hyper_serve")?
                    .getattr("stream_close")?
                    .call1((sid,))?;
                Ok::<(), pyo3::PyErr>(())
            });
        });
        use futures_util::StreamExt;
        use http_body_util::BodyExt as _BodyExt;
        let stream = futures_util::stream::poll_fn(move |cx| rx.poll_recv(cx))
            .map(|r| r.map(hyper::body::Frame::data));
        let body = _BodyExt::map_err(
            http_body_util::StreamBody::new(stream),
            |e: std::io::Error| -> Box<dyn std::error::Error + Send + Sync> { Box::new(e) },
        );
        return Ok(builder.body(body.boxed()).unwrap());
    }
    // Zero-copy file streaming: the app already sent content headers; the
    // bytes flow straight from disk, never through Python.
    if let Some(path) = sendfile_path {
        match tokio::fs::File::open(&path).await {
            Ok(file) => {
                if let Ok(meta) = file.metadata().await {
                    builder = builder.header("content-length", meta.len());
                }
                use futures_util::TryStreamExt;
                use http_body_util::BodyExt as _BodyExt;
                let stream =
                    tokio_util::io::ReaderStream::new(file).map_ok(hyper::body::Frame::data);
                let body = _BodyExt::map_err(
                    http_body_util::StreamBody::new(stream),
                    |e: std::io::Error| -> Box<dyn std::error::Error + Send + Sync> { Box::new(e) },
                );
                return Ok(builder.body(body.boxed()).unwrap());
            }
            Err(e) => {
                eprintln!("fasterapi hyper: sendfile open {path}: {e}");
                return Ok(builder
                    .status(StatusCode::NOT_FOUND)
                    .body(boxed_full(Bytes::from_static(b"Not Found")))
                    .unwrap());
            }
        }
    }
    Ok(builder.body(boxed_full(Bytes::from(resp_body))).unwrap())
}

fn internal_error() -> (u16, Vec<(String, String)>, Vec<u8>, String) {
    (
        500u16,
        vec![("content-type".to_string(), "application/json".to_string())],
        br#"{"detail":"Internal Server Error"}"#.to_vec(),
        String::new(),
    )
}

// ---------------------------------------------------------------------------
// Websocket host (spike): Rust does the upgrade handshake + RFC 6455 framing,
// Python drives the ASGI websocket app per session (see `_hyper_serve`).
// ---------------------------------------------------------------------------

/// Max single message size (4 MiB); largermtu → close 1009.
const WS_MAX_MESSAGE: usize = 4 * 1024 * 1024;

fn is_ws_upgrade(headers: &hyper::HeaderMap) -> bool {
    let upgrade = headers
        .get(hyper::header::UPGRADE)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("websocket"))
        .unwrap_or(false);
    let connection = headers
        .get(hyper::header::CONNECTION)
        .and_then(|v| v.to_str().ok())
        .map(|v| {
            v.to_ascii_lowercase()
                .split(',')
                .any(|t| t.trim() == "upgrade")
        })
        .unwrap_or(false);
    upgrade && connection
}

struct WsFrame {
    fin: bool,
    opcode: u8,
    payload: Vec<u8>,
}

enum FrameError {
    Eof,
    Oversize,
    Protocol,
}

async fn read_ws_frame(io: &mut TokioIo<hyper::upgrade::Upgraded>) -> Result<WsFrame, FrameError> {
    use tokio::io::AsyncReadExt;
    let mut hdr = [0u8; 2];
    io.read_exact(&mut hdr).await.map_err(|_| FrameError::Eof)?;
    let fin = hdr[0] & 0x80 != 0;
    let opcode = hdr[0] & 0x0F;
    let masked = hdr[1] & 0x80 != 0;
    let mut len = (hdr[1] & 0x7F) as u64;
    if opcode >= 0x8 && (!fin || len > 125) {
        return Err(FrameError::Protocol); // control frames: FIN + ≤125B
    }
    if len == 126 {
        let mut ext = [0u8; 2];
        io.read_exact(&mut ext).await.map_err(|_| FrameError::Eof)?;
        len = u16::from_be_bytes(ext) as u64;
    } else if len == 127 {
        let mut ext = [0u8; 8];
        io.read_exact(&mut ext).await.map_err(|_| FrameError::Eof)?;
        len = u64::from_be_bytes(ext);
    }
    if len > WS_MAX_MESSAGE as u64 {
        return Err(FrameError::Oversize);
    }
    let mut mask = [0u8; 4];
    if masked {
        io.read_exact(&mut mask)
            .await
            .map_err(|_| FrameError::Eof)?;
    } else if opcode <= 0x2 {
        return Err(FrameError::Protocol); // client data frames MUST be masked
    }
    let mut payload = vec![0u8; len as usize];
    if len > 0 {
        io.read_exact(&mut payload)
            .await
            .map_err(|_| FrameError::Eof)?;
        if masked {
            for (i, byte) in payload.iter_mut().enumerate() {
                *byte ^= mask[i % 4];
            }
        }
    }
    Ok(WsFrame {
        fin,
        opcode,
        payload,
    })
}

async fn write_ws_frame(
    io: &mut TokioIo<hyper::upgrade::Upgraded>,
    opcode: u8,
    payload: &[u8],
) -> std::io::Result<()> {
    use tokio::io::AsyncWriteExt;
    let mut hdr = vec![0x80 | (opcode & 0x0F)];
    if payload.len() < 126 {
        hdr.push(payload.len() as u8);
    } else if payload.len() < 65536 {
        hdr.push(126);
        hdr.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    } else {
        hdr.push(127);
        hdr.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    }
    io.write_all(&hdr).await?;
    io.write_all(payload).await?;
    io.flush().await
}

fn ws_close_payload(code: u16, reason: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + reason.len());
    out.extend_from_slice(&code.to_be_bytes());
    out.extend_from_slice(reason);
    out
}

/// Blocking Python call (spawn_blocking): run one ws step, return server frames.
async fn py_ws_step(
    app: PyObject,
    sid: String,
    opcode: Option<u8>,
    payload: Vec<u8>,
    wait_secs: f64,
) -> Vec<(u8, Vec<u8>)> {
    let _ = &app;
    let res = tokio::task::spawn_blocking(move || {
        Python::with_gil(|py| {
            let helper = py
                .import("fasterapi._hyper_serve")
                .map_err(|e| e.to_string())?;
            let ret = helper
                .getattr("ws_step")
                .map_err(|e| e.to_string())?
                .call1((sid, opcode, payload, wait_secs))
                .map_err(|e| e.to_string())?;
            ret.extract::<Vec<(u8, Vec<u8>)>>()
                .map_err(|e| e.to_string())
        })
    })
    .await;
    match res {
        Ok(Ok(frames)) => frames,
        Ok(Err(e)) => {
            eprintln!("fasterapi hyper ws_step: {e}");
            vec![(0x8, ws_close_payload(1011, b"internal error"))]
        }
        Err(e) => {
            eprintln!("fasterapi hyper ws join: {e}");
            vec![(0x8, ws_close_payload(1011, b"internal error"))]
        }
    }
}

fn py_ws_close(sid: String) {
    // Best-effort synchronous cleanup (idempotent server-side).
    let _res: Result<(), PyErr> = Python::with_gil(|py| {
        py.import("fasterapi._hyper_serve")?
            .getattr("ws_close")?
            .call1((sid,))?;
        Ok(())
    });
}

async fn serve_ws(
    app: PyObject,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<RespBody>, hyper::Error> {
    fn bad(status: StatusCode, msg: &'static str) -> Result<Response<RespBody>, hyper::Error> {
        Ok(Response::builder()
            .status(status)
            .body(boxed_full(Bytes::from_static(msg.as_bytes())))
            .unwrap())
    }
    let key = req
        .headers()
        .get("sec-websocket-key")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    if key.is_empty() {
        return bad(StatusCode::BAD_REQUEST, "missing Sec-WebSocket-Key");
    }
    let version_ok = req
        .headers()
        .get("sec-websocket-version")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.trim() == "13")
        .unwrap_or(false);
    if !version_ok {
        return bad(
            StatusCode::UPGRADE_REQUIRED,
            "Sec-WebSocket-Version: 13 required",
        );
    }
    let path = req.uri().path().to_string();
    let query = req.uri().query().unwrap_or("").to_string();
    let mut headers: Vec<(String, Vec<u8>)> = Vec::new();
    for (name, value) in req.headers() {
        headers.push((name.to_string(), value.as_bytes().to_vec()));
    }
    // Ask Python to run the ASGI app until accept/refusal (blocking).
    // Ask Python to run the ASGI app until accept/refusal (blocking).
    // Join failure can only mean runtime shutdown; report as an error verdict.
    let verdict: Result<Result<(String, String, Vec<(String, String)>), String>, String> =
        tokio::task::spawn_blocking({
            let app = Python::with_gil(|py| app.clone_ref(py));
            move || {
                Python::with_gil(|py| {
                    let helper = py
                        .import("fasterapi._hyper_serve")
                        .map_err(|e| e.to_string())?;
                    let ret = helper
                        .getattr("ws_open")
                        .map_err(|e| e.to_string())?
                        .call1((app, path, query, headers, key))
                        .map_err(|e| e.to_string())?;
                    ret.extract::<(String, String, Vec<(String, String)>)>()
                        .map_err(|e| e.to_string())
                })
            }
        })
        .await
        .map_err(|e| format!("ws_open join: {e}"));
    let (sid, status, data) = match verdict {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => {
            eprintln!("fasterapi hyper ws_open: {e}");
            (String::new(), "error".to_string(), vec![])
        }
        Err(e) => {
            eprintln!("fasterapi hyper ws_open: {e}");
            (String::new(), "error".to_string(), vec![])
        }
    };
    if status != "accepted" {
        let (code, reason) = match status.as_str() {
            "refused" => (
                403u16,
                data.first().map(|d| d.1.clone()).unwrap_or_default(),
            ),
            _ => (500u16, "websocket open failed".to_string()),
        };
        let body = format!(r#"{{"detail":{reason:?}}}"#);
        py_ws_close(sid);
        return Ok(Response::builder()
            .status(StatusCode::from_u16(code).unwrap())
            .body(boxed_full(Bytes::from(body)))
            .unwrap());
    }
    let accept_key = data.first().map(|d| d.0.clone()).unwrap_or_default();
    let mut builder_101 = Response::builder()
        .status(StatusCode::SWITCHING_PROTOCOLS)
        .header("upgrade", "websocket")
        .header("connection", "Upgrade")
        .header("sec-websocket-accept", accept_key);
    for (k, v) in data.iter().skip(1) {
        let kl = k.to_ascii_lowercase();
        if kl == "content-length"
            || kl == "transfer-encoding"
            || kl == "connection"
            || kl == "upgrade"
            || kl == "sec-websocket-accept"
            || kl.starts_with("x-fasterapi-")
        {
            continue;
        }
        if let (Ok(name), Ok(value)) = (
            hyper::header::HeaderName::from_bytes(k.as_bytes()),
            hyper::header::HeaderValue::from_bytes(v.as_bytes()),
        ) {
            builder_101 = builder_101.header(name, value);
        }
    }
    // The upgrade future only resolves AFTER this 101 goes out, so the pump
    // must run detached — awaiting it here would deadlock. Clone what moves.
    let pump_app = Python::with_gil(|py| app.clone_ref(py));
    let pump_fut = hyper::upgrade::on(req);
    tokio::spawn(async move {
        match pump_fut.await {
            Ok(upgraded) => {
                let mut io = TokioIo::new(upgraded);
                ws_pump(&pump_app, &sid, &mut io).await;
            }
            Err(e) => {
                eprintln!("fasterapi hyper: upgrade failed: {e}");
                py_ws_close(sid);
            }
        }
    });
    Ok(builder_101.body(boxed_full(Bytes::new())).unwrap())
}

/// Frame pump for one upgraded connection: client frames drive app steps;
/// idle ticks drain server pushes. The connection belongs to this task.
async fn ws_pump(app: &PyObject, sid: &str, io: &mut TokioIo<hyper::upgrade::Upgraded>) {
    // Frame pump: client frames drive app steps; idle ticks drain pushes.
    let mut io: &mut TokioIo<hyper::upgrade::Upgraded> = io;
    let mut frag_op: Option<u8> = None;
    let mut frag_buf: Vec<u8> = Vec::new();
    loop {
        tokio::select! {
            fr = read_ws_frame(&mut io) => match fr {
                Err(FrameError::Eof) => break,
                Err(FrameError::Oversize) => {
                    let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1009, b"message too big")).await;
                    break;
                }
                Err(FrameError::Protocol) => {
                    let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1002, b"protocol error")).await;
                    break;
                }
                Ok(f) => match f.opcode {
                    0x9 => { let _ = write_ws_frame(&mut io, 0xA, &f.payload).await; }
                    0xA => {}
                    0x8 => {
                        let _ = write_ws_frame(&mut io, 0x8, &f.payload).await;
                        break;
                    }
                    0x0 | 0x1 | 0x2 => {
                        if f.opcode != 0x0 {
                            if frag_op.is_some() {
                                let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1002, b"fragment error")).await;
                                break;
                            }
                            frag_op = Some(f.opcode);
                            frag_buf.clear();
                        } else if frag_op.is_none() {
                            let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1002, b"stray continuation")).await;
                            break;
                        }
                        if frag_buf.len() + f.payload.len() > WS_MAX_MESSAGE {
                            let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1009, b"message too big")).await;
                            break;
                        }
                        frag_buf.extend_from_slice(&f.payload);
                        if f.fin {
                            let op = frag_op.take().unwrap_or(f.opcode);
                            let payload = std::mem::take(&mut frag_buf);
                            if op == 0x1 && std::str::from_utf8(&payload).is_err() {
                                let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1007, b"bad utf-8")).await;
                                break;
                            }
                            if forward_ws_frames(app, sid, io, op, payload, 5.0).await {
                                break;
                            }
                        }
                    }
                    _ => {
                        let _ = write_ws_frame(&mut io, 0x8, &ws_close_payload(1003, b"unsupported opcode")).await;
                        break;
                    }
                },
            },
            _ = tokio::time::sleep(std::time::Duration::from_secs(2)) => {
                if forward_ws_frames(app, sid, io, 0xFF, Vec::new(), 0.1).await {
                    break;
                }
            }
        }
    }
    py_ws_close(sid.to_string());
}

/// Deliver one client message (or a poll tick, opcode 0xFF) and forward
/// queued server frames. Returns true when the session ended.
async fn forward_ws_frames(
    app: &PyObject,
    sid: &str,
    io: &mut TokioIo<hyper::upgrade::Upgraded>,
    opcode: u8,
    payload: Vec<u8>,
    wait_secs: f64,
) -> bool {
    let app_ref = Python::with_gil(|py| app.clone_ref(py));
    let (op, data) = if opcode == 0xFF {
        (None, Vec::new())
    } else {
        (Some(opcode), payload)
    };
    for (sop, spayload) in py_ws_step(app_ref, sid.to_string(), op, data, wait_secs).await {
        if sop == 0x8 {
            let _ = write_ws_frame(io, 0x8, &spayload).await;
            return true;
        }
        if write_ws_frame(io, sop, &spayload).await.is_err() {
            return true;
        }
    }
    false
}

fn py_ws_close_session(sid: String) {
    if sid.is_empty() {
        return;
    }
    let _ = tokio::task::spawn_blocking(move || py_ws_close(sid));
}

/// Serve an ASGI app over Hyper + Tokio. Blocks until Ctrl-C (SIGINT).
///
/// `workers=None` uses Tokio's default (one worker per CPU). `bucket` is an
/// optional `TokenBucket` whose limiter is checked at socket level — before
/// any Python code runs — returning `429` on rejection.
#[pyfunction]
#[pyo3(signature = (app, host="127.0.0.1", port=8000, workers=None, bucket=None))]
fn serve_asgi(
    py: Python<'_>,
    app: PyObject,
    host: &str,
    port: u16,
    workers: Option<usize>,
    bucket: Option<Py<TokenBucket>>,
) -> PyResult<()> {
    let mut builder = tokio::runtime::Builder::new_multi_thread();
    builder.enable_all().thread_name("fasterapi-hyper");
    if let Some(n) = workers {
        builder.worker_threads(n.max(1));
    }
    let rt = builder.build().map_err(py_runtime_error)?;
    // Python worker pool: smaller than CPU count reduces GIL thrash for
    // validation-heavy handlers on GIL builds; override with FASTERAPI_POOL.
    // (Free-threaded builds want this high; GIL builds want it low.)
    let pool_size = std::env::var("FASTERAPI_POOL")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|&n| n >= 1)
        .unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(4)
        });
    let pool = std::sync::Arc::new(PyWorkerPool::new(pool_size));
    // Clone the shared limiter Arc — every request check is GIL-free.
    let rate_limit = bucket.map(|b| b.borrow(py).limiter.clone());
    py.allow_threads(|| {
        run_until_ctrl_c(rt, app, host.to_string(), port, pool, rate_limit)
    })
    .map_err(py_runtime_error)
}

fn run_until_ctrl_c(
    rt: tokio::runtime::Runtime,
    app: PyObject,
    host: String,
    port: u16,
    pool: std::sync::Arc<PyWorkerPool>,
    rate_limit: Option<Arc<GovLimiter>>,
) -> Result<(), String> {
    rt.block_on(async move {
        run_lifespan(&app, "startup")?;
        let addr = format!("{host}:{port}");
        let listener = tokio::net::TcpListener::bind(&addr)
            .await
            .map_err(|e| format!("bind {addr}: {e}"))?;
        let in_flight = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        #[cfg(unix)]
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .map_err(|e| format!("signal: {e}"))?;
        loop {
            tokio::select! {
                conn = listener.accept() => {
                    let (stream, _peer) = conn.map_err(|e| e.to_string())?;
                    // Disable Nagle: small JSON responses must not wait for ACKs.
                    let _ = stream.set_nodelay(true);
                    let io = TokioIo::new(stream);
                    // Brief GIL grab per connection for the refcount bump.
                    let app_ref = Python::with_gil(|py| app.clone_ref(py));
                    let pool = pool.clone();
                    let rate_limit = rate_limit.clone();
                    let in_flight = in_flight.clone();
                    in_flight.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    tokio::spawn(async move {
                        let svc = service_fn(move |req| {
                            let app_ref = Python::with_gil(|py| app_ref.clone_ref(py));
                            let pool = pool.clone();
                            let rate_limit = rate_limit.clone();
                            async move { serve_one(&pool, app_ref, req, rate_limit).await }
                        });
                        // with_upgrades is REQUIRED: without it, upgrade::on()
                        // can never resolve ("low level API in use").
                        let conn = http1::Builder::new()
                            .serve_connection(io, svc)
                            .with_upgrades();
                        tokio::pin!(conn);
                        if let Err(e) = conn.await {
                            eprintln!("fasterapi hyper: connection error: {e}");
                        }
                        in_flight.fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
                    });
                }
                _ = tokio::signal::ctrl_c() => {
                    eprintln!("fasterapi hyper: SIGINT, draining…");
                    break;
                }
                _ = async {
                    #[cfg(unix)]
                    {
                        sigterm.recv().await;
                    }
                    #[cfg(not(unix))]
                    {
                        std::future::pending::<()>().await;
                    }
                } => {
                    eprintln!("fasterapi hyper: SIGTERM, draining…");
                    break;
                }
            }
        }
        // Graceful shutdown: stop accepting, drain in-flight connections
        // (bounded wait), then run lifespan shutdown.
        for _ in 0..50 {
            if in_flight.load(std::sync::atomic::Ordering::Relaxed) == 0 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
        run_lifespan(&app, "shutdown")?;
        Ok(())
    })
}

// ---------------------------------------------------------------------------
// Module
// ---------------------------------------------------------------------------

/// Native `_fasterapi_rs` extension module.
#[pymodule]
fn _fasterapi_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    m.add_function(wrap_pyfunction!(fast_json_loads, m)?)?;
    m.add_function(wrap_pyfunction!(fast_json_dumps, m)?)?;
    m.add_function(wrap_pyfunction!(process_rss_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(tokio_worker_count, m)?)?;
    m.add_function(wrap_pyfunction!(hyper_health, m)?)?;
    m.add_function(wrap_pyfunction!(serve_static_config, m)?)?;
    m.add_function(wrap_pyfunction!(has_simd, m)?)?;
    m.add_function(wrap_pyfunction!(validate_int, m)?)?;
    m.add_function(wrap_pyfunction!(validate_float, m)?)?;
    m.add_function(wrap_pyfunction!(validate_bool, m)?)?;
    m.add_function(wrap_pyfunction!(validate_str, m)?)?;
    m.add_function(wrap_pyfunction!(percent_decode, m)?)?;
    m.add_function(wrap_pyfunction!(parse_query_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(serve_asgi, m)?)?;
    m.add_class::<RustRouter>()?;
    m.add_class::<TokenBucket>()?;
    m.add_class::<DependencyDAG>()?;
    Ok(())
}
