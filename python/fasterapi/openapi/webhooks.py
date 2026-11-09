"""fasterapi.openapi.webhooks — webhook route surface parity.

FastAPI wires ``FastAPI(webhooks=router)``; the router's routes are emitted
under the OpenAPI ``webhooks`` key. This module exists for import-path
parity (``from fasterapi.openapi import webhooks``).
"""

from ..routing import APIRouter as APIRouter  # noqa: F401 (webhook routes are APIRouters)
