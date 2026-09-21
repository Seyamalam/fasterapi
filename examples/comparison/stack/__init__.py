"""Make the shared stack importable as ``stack`` from the comparison apps."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stack import db, schemas, security, settings  # noqa: E402,F401

__all__ = ["db", "schemas", "security", "settings"]
