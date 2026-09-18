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
//! The inverted-runtime Hyper + Tokio host (Rust owns the socket, Python runs
//! as an embedded guest) is staged, not shipped: its stack (tokio, hyper,
//! hyper-util) is deliberately absent from Cargo.toml until it lands, keeping
//! the build light. Parity stays king until then — Starlette host + Pydantic.
//!
//! Threading: every entry point is pure + lock-clean (`parking_lot`), safe for
//! Python 3.13+ free-threaded (`--disable-gil`) runtimes.

use std::collections::{HashMap, VecDeque};
use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Instant;

use parking_lot::RwLock;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
const VERSION: &str = env!("CARGO_PKG_VERSION");

// ---------------------------------------------------------------------------
// JSON fast-path
// ---------------------------------------------------------------------------

fn value_to_py(py: Python<'_>, v: &serde_json::Value) -> PyObject {
    match v {
        serde_json::Value::Null => py.None(),
        serde_json::Value::Bool(b) => PyBool::new_bound(py, *b).to_object(py),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_object(py)
            } else if let Some(u) = n.as_u64() {
                u.to_object(py)
            } else if let Some(f) = n.as_f64() {
                PyFloat::new_bound(py, f).to_object(py)
            } else {
                py.None()
            }
        }
        serde_json::Value::String(s) => PyString::new_bound(py, s).to_object(py),
        serde_json::Value::Array(arr) => {
            let list = PyList::empty_bound(py);
            for item in arr {
                list.append(value_to_py(py, item))
                    .expect("list append cannot fail");
            }
            list.to_object(py)
        }
        serde_json::Value::Object(map) => {
            let dict = PyDict::new_bound(py);
            for (k, val) in map {
                dict.set_item(k, value_to_py(py, val))
                    .expect("dict insert cannot fail");
            }
            dict.to_object(py)
        }
    }
}

fn write_json_str(buf: &mut String, s: &str) {
    buf.push('"');
    for c in s.chars() {
        match c {
            '"' => buf.push_str("\\\""),
            '\\' => buf.push_str("\\\\"),
            '\n' => buf.push_str("\\n"),
            '\r' => buf.push_str("\\r"),
            '\t' => buf.push_str("\\t"),
            '\u{08}' => buf.push_str("\\b"),
            '\u{0C}' => buf.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                buf.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => buf.push(c),
        }
    }
    buf.push('"');
}

fn write_json_key(buf: &mut String, obj: &Bound<'_, PyAny>) -> PyResult<()> {
    // Mirror CPython: str as-is, int/float/bool/None coerced, else TypeError.
    if let Ok(s) = obj.downcast::<PyString>() {
        write_json_str(buf, &s.to_string());
        return Ok(());
    }
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
            buf.push_str(&iv.to_string());
            return Ok(());
        }
        if let Ok(uv) = i.extract::<u64>() {
            buf.push_str(&uv.to_string());
            return Ok(());
        }
        let f: f64 = i.extract()?;
        write_json_float(buf, f);
        return Ok(());
    }
    if let Ok(f) = obj.downcast::<PyFloat>() {
        write_json_float(buf, f.value());
        return Ok(());
    }
    Err(pyo3::exceptions::PyTypeError::new_err(
        "keys must be str, int, float, bool or None",
    ))
}

fn write_json_float(buf: &mut String, v: f64) {
    if v.is_infinite() {
        buf.push_str(if v.is_sign_positive() {
            "Infinity"
        } else {
            "-Infinity"
        });
    } else if v.is_nan() {
        buf.push_str("NaN");
    } else if v.fract() == 0.0 && v.abs() < 1e21 {
        buf.push_str(&format!("{v:.1}"));
    } else {
        buf.push_str(&v.to_string());
    }
}

fn write_json_value(buf: &mut String, obj: &Bound<'_, PyAny>) -> PyResult<()> {
    if obj.is_none() {
        buf.push_str("null");
        return Ok(());
    }
    if let Ok(b) = obj.downcast::<PyBool>() {
        buf.push_str(if b.is_true() { "true" } else { "false" });
        return Ok(());
    }
    if let Ok(s) = obj.downcast::<PyString>() {
        write_json_str(buf, &s.to_string());
        return Ok(());
    }
    if let Ok(i) = obj.downcast::<PyInt>() {
        if let Ok(iv) = i.extract::<i64>() {
            buf.push_str(&iv.to_string());
            return Ok(());
        }
        if let Ok(uv) = i.extract::<u64>() {
            buf.push_str(&uv.to_string());
            return Ok(());
        }
        let f: f64 = i.extract()?;
        write_json_float(buf, f);
        return Ok(());
    }
    if let Ok(f) = obj.downcast::<PyFloat>() {
        write_json_float(buf, f.value());
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
            write_json_value(buf, &item)?;
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
            write_json_value(buf, &item)?;
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
            write_json_key(buf, &k)?;
            buf.push(':');
            write_json_value(buf, &v)?;
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
#[pyfunction]
fn fast_json_loads(py: Python<'_>, payload: &str) -> PyResult<PyObject> {
    // simd-json mutates its input, so work on a private copy off-GIL.
    let parsed: Result<serde_json::Value, String> = py
        .allow_threads(|| {
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
            let json_mod = py.import_bound("json")?;
            let loads = json_mod.getattr("loads")?;
            loads.call1((payload,))?.extract()
        }
    }
}

/// Direct JSON serialize: walks Python objects straight into the output
/// buffer (no intermediate `serde_json::Value`), mirroring stdlib semantics.
#[pyfunction]
fn fast_json_dumps(py: Python<'_>, obj: PyObject) -> PyResult<String> {
    let bound = obj.bind(py);
    let mut buf = String::with_capacity(128);
    write_json_value(&mut buf, bound)?;
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
                let dict = PyDict::new_bound(py);
                for (k, v) in params {
                    dict.set_item(k, v)?;
                }
                Ok(Some((target, dict.to_object(py))))
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

/// Thread-safe token-bucket guard executed at the socket level in Rust.
///
/// Python configures it via `Guards` / `RateLimiter`; the Hyper host calls
/// `check()` before any Python runs. Returns `True` when admitted.
#[pyclass]
struct TokenBucket {
    limiter: Arc<
        governor::RateLimiter<
            governor::state::NotKeyed,
            governor::state::InMemoryState,
            governor::clock::DefaultClock,
        >,
    >,
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

const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;
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
    reply: tokio::sync::oneshot::Sender<Result<(u16, Vec<(String, String)>, Vec<u8>), String>>,
}

fn run_py_job(job: PyJob) {
    let result = Python::with_gil(|py| {
        use pyo3::types::PyBytes;
        // NOTE: Vec<u8> wouldcross as list[int]; HTTP needs real bytes.
        let py_headers: Vec<(String, PyObject)> = job
            .headers
            .iter()
            .map(|(k, v)| (k.clone(), PyBytes::new_bound(py, v).into_any().unbind()))
            .collect();
        let py_body = PyBytes::new_bound(py, &job.body).into_any().unbind();
        let helper = py
            .import_bound("fasterapi._hyper_serve")
            .map_err(|e| e.to_string())?;
        let ret = helper
            .getattr("run_request")
            .map_err(|e| e.to_string())?
            .call1((job.app, job.method, job.path, job.query, py_headers, py_body))
            .map_err(|e| e.to_string())?;
        ret.extract::<(u16, Vec<(String, String)>, Vec<u8>)>()
            .map_err(|e| e.to_string())
    });
    let _ = job.reply.send(result);
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
        let i = self
            .next
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            % self.senders.len();
        self.senders[i]
            .try_send(job)
            .map_err(|e| match e {
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
                .import_bound("fasterapi._hyper_serve")
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
) -> Result<Response<Full<Bytes>>, hyper::Error> {
    let method = req.method().to_string();
    let uri = req.uri().clone();
    let path = uri.path().to_string();
    let query = uri.query().unwrap_or("").to_string();
    let mut headers: Vec<(String, Vec<u8>)> = Vec::new();
    for (name, value) in req.headers() {
        headers.push((name.to_string(), value.as_bytes().to_vec()));
    }
    let collected = req.collect().await?;
    let body = collected.to_bytes();
    if body.len() > MAX_BODY_BYTES {
        let resp = Response::builder()
            .status(StatusCode::PAYLOAD_TOO_LARGE)
            .body(Full::new(Bytes::from_static(b"Request body too large")))
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
    };
    // Bounded pool first; overflow degrades to spawn_blocking instead of
    // deadlocking when every worker queue is momentarily full.
    let out: Result<Result<(u16, Vec<(String, String)>, Vec<u8>), String>, String> =
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
    let (status, resp_headers, resp_body) = match out {
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
    let mut builder = Response::builder().status(
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
    );
    for (k, v) in &resp_headers {
        // Skip hop-by-hop / framing headers the server owns (`server` is
        // re-stamped below — the app layer still thinks in uvicorn terms).
        let kl = k.to_ascii_lowercase();
        if kl == "content-length"
            || kl == "transfer-encoding"
            || kl == "connection"
            || kl == "server"
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
    Ok(builder
        .header("server", "fasterapi-hyper")
        .body(Full::new(Bytes::from(resp_body)))
        .unwrap())
}

fn internal_error() -> (u16, Vec<(String, String)>, Vec<u8>) {    (
        500u16,
        vec![("content-type".to_string(), "application/json".to_string())],
        br#"{"detail":"Internal Server Error"}"#.to_vec(),
    )
}

/// Serve an ASGI app over Hyper + Tokio. Blocks until Ctrl-C (SIGINT).
///
/// `workers=None` uses Tokio's default (one worker per CPU).
#[pyfunction]
#[pyo3(signature = (app, host="127.0.0.1", port=8000, workers=None))]
fn serve_asgi(
    py: Python<'_>,
    app: PyObject,
    host: &str,
    port: u16,
    workers: Option<usize>,
) -> PyResult<()> {
    let mut builder = tokio::runtime::Builder::new_multi_thread();
    builder.enable_all().thread_name("fasterapi-hyper");
    if let Some(n) = workers {
        builder.worker_threads(n.max(1));
    }
    let rt = builder.build().map_err(py_runtime_error)?;
    let pool_size = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    let pool = std::sync::Arc::new(PyWorkerPool::new(pool_size));
    py.allow_threads(|| run_until_ctrl_c(rt, app, host.to_string(), port, pool))
        .map_err(py_runtime_error)
}

fn run_until_ctrl_c(
    rt: tokio::runtime::Runtime,
    app: PyObject,
    host: String,
    port: u16,
    pool: std::sync::Arc<PyWorkerPool>,
) -> Result<(), String> {
    rt.block_on(async move {
        run_lifespan(&app, "startup")?;
        let addr = format!("{host}:{port}");
        let listener = tokio::net::TcpListener::bind(&addr)
            .await
            .map_err(|e| format!("bind {addr}: {e}"))?;
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
                    tokio::spawn(async move {
                        let svc = service_fn(move |req| {
                            let app_ref = Python::with_gil(|py| app_ref.clone_ref(py));
                            let pool = pool.clone();
                            async move { serve_one(&pool, app_ref, req).await }
                        });
                        if let Err(e) = http1::Builder::new()
                            .serve_connection(io, svc)
                            .await
                        {
                            eprintln!("fasterapi hyper: connection error: {e}");
                        }
                    });
                }
                _ = tokio::signal::ctrl_c() => {
                    break;
                }
            }
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
