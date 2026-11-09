# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (main) | ✅ |

## Reporting a vulnerability

Do **not** open a public issue for security reports. Email the maintainer
via the contact on https://github.com/Seyamalam/fasterapi with:

- a minimal reproduction (a differential case against real FastAPI is ideal),
- the affected host (uvicorn path, Hyper host, or both),
- impact assessment if known.

Expect acknowledgment within a few days; fixes land as `fix:` commits with
a regression test, and the advisory is published once a release ships.

## Scope notes

- The **Hyper host** (`FASTERAPI_HYPER=1` / `--hyper`) is experimental:
  buffered bodies, `FASTERAPI_MAX_BODY` cap (16 MiB default), basic
  RFC 6455 WebSockets. Production deployments should use uvicorn until the
  host promotes to default.
- Rate limiting via `RateLimiter` is per-process. Multi-worker deployments
  need an external limiter for correct global accounting.
- Security scheme parsing (`OAuth2`, HTTP auth, API keys) mirrors FastAPI;
  credential handling itself is application code, not covered here.
