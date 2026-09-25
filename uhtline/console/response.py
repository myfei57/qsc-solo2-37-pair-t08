"""Response helpers shared by the console handlers and the HTTP server."""

from __future__ import annotations

import json
from typing import Any

from ..errors import ServiceError


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def html_bytes(payload: str) -> bytes:
    return payload.encode("utf-8")


def error_body(exc: ServiceError) -> dict[str, Any]:
    return exc.as_dict()


def error_status(exc: ServiceError) -> int:
    return exc.status


__all__ = ["error_body", "error_status", "html_bytes", "json_bytes"]
