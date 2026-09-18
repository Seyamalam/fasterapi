"""fasterapi.openapi.constants — spec constants (1:1 upstream values)."""

from __future__ import annotations

METHODS_WITH_BODY = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}
REF_PREFIX = "#/components/schemas/"
REF_TEMPLATE = "#/components/schemas/{model}"
