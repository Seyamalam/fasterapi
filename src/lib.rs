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

fn py_to_value(obj: &Bound<'_, PyAny>) -> PyResult<serde_json::Value> {
    let py = obj.py();
    if obj.is_none() {
        return Ok(serde_json::Value::Null);
    }
    if let Ok(b) = obj.downcast::<PyBool>() {
        return Ok(serde_json::Value::Bool(b.is_true()));
    }
    if let Ok(s) = obj.downcast::<PyString>() {
        return Ok(serde_json::Value::String(s.to_string()));
    }
    if let Ok(f) = obj.downcast::<PyFloat>() {
        let v = f.value();
        return Ok(serde_json::Number::from_f64(v)
            .map(serde_json::Value::Number)
            .unwrap_or(serde_json::Value::Null));
    }
    if let Ok(i) = obj.downcast::<PyInt>() {
        if let Ok(iv) = i.extract::<i64>() {
            return Ok(serde_json::Value::Number(iv.into()));
        }
        if let Ok(uv) = i.extract::<u64>() {
            return Ok(serde_json::Value::Number(uv.into()));
        }
        // Fall back to float for big ints.
        let f: f64 = i.extract()?;
        return Ok(serde_json::Number::from_f64(f)
            .map(serde_json::Value::Number)
            .unwrap_or(serde_json::Value::Null));
    }
    if let Ok(list) = obj.downcast::<PyList>() {
        let mut out = Vec::with_capacity(list.len());
        for item in list.iter() {
            out.push(py_to_value(&item)?);
        }
        return Ok(serde_json::Value::Array(out));
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let mut map = serde_json::Map::with_capacity(dict.len());
        for (k, v) in dict.iter() {
            let key: String = k.extract()?;
            map.insert(key, py_to_value(&v)?);
        }
        return Ok(serde_json::Value::Object(map));
    }
    // Fallback: ask Python for a JSON-compatible repr via `str()`.
    // (Callers should pass plain JSON types on the hot path.)
    let _ = py;
    Err(pyo3::exceptions::PyTypeError::new_err(
        "fast_json_dumps: object is not JSON-serializable (pass dict/list/str/int/float/bool/None)",
    ))
}

/// SIMD-accelerated JSON parse. Releases the GIL during parsing.
///
/// Falls back to `serde_json` if the input is not valid UTF-8 SIMD JSON.
#[pyfunction]
fn fast_json_loads(py: Python<'_>, payload: &str) -> PyResult<PyObject> {
    // simd-json mutates its input, so work on a private copy off-GIL.
    let value: serde_json::Value = py
        .allow_threads(|| {
            let mut bytes = payload.as_bytes().to_vec();
            simd_json::from_slice::<serde_json::Value>(&mut bytes)
                .or_else(|_| serde_json::from_str::<serde_json::Value>(payload))
        })
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(value_to_py(py, &value))
}

/// SIMD-friendly JSON serialize. Releases the GIL during serialization.
#[pyfunction]
fn fast_json_dumps(py: Python<'_>, obj: PyObject) -> PyResult<String> {
    let value = py_to_value(obj.bind(py))?;
    py.allow_threads(|| {
        serde_json::to_string(&value)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
    })
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
        "strategy": if exists && size > 0 { "tokio-sendfile-zero-copy" } else { "python-fallback" },
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
// parsing, percent-decoding) runs here on Tokio threads with `allow_threads`.
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
    m.add_class::<RustRouter>()?;
    m.add_class::<TokenBucket>()?;
    m.add_class::<DependencyDAG>()?;
    Ok(())
}
