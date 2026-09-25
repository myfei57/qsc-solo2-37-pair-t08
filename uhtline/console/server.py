"""Threaded HTTP server for the operator console."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import ServiceError, ValidationError
from ..app.runtime import Runtime
from .app import ConsoleApp
from .pages import PAGE_NAMES
from .response import error_body, error_status, html_bytes, json_bytes

MAX_BODY_BYTES = 1_048_576


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    """Translate one HTTP exchange into a ConsoleApp call."""

    server_version = "LineConsole/1.0"

    @property
    def console(self) -> ConsoleApp:
        return self.server.console  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        self.console.log(format % args)

    def _write_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValidationError("request body exceeds the permitted size", bytes=length, maximum=MAX_BODY_BYTES)
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body is not valid JSON", reason=str(exc)) from exc
        if not isinstance(parsed, dict):
            raise ValidationError("request body must be a JSON object", value=type(parsed).__name__)
        return parsed

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        try:
            if method == "GET" and path in ("/",) + tuple(f"/{name}" for name in PAGE_NAMES):
                name = "overview" if path == "/" else path.lstrip("/")
                self._write_bytes(HTTPStatus.OK, html_bytes(self.console.page(name)), "text/html; charset=utf-8")
                return
            body = self._body() if method == "POST" else {}
            status, response = self.console.handle(method, path, query, body)
            self._write_bytes(status, json_bytes(response), "application/json; charset=utf-8")
        except ServiceError as exc:
            self._write_bytes(error_status(exc), json_bytes(error_body(exc)), "application/json; charset=utf-8")
        except Exception as exc:  # pragma: no cover - defensive process boundary
            fallback = ValidationError("unhandled console failure", reason=str(exc))
            self._write_bytes(fallback.status, json_bytes(fallback.as_dict()), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


class ConsoleServer:
    """Owns the HTTP listener and its clean start and stop lifecycle."""

    def __init__(self, runtime: Runtime, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.runtime = runtime
        self.app = ConsoleApp(runtime)
        self._server = ThreadingHTTPServer((host, port), ConsoleRequestHandler)
        self._server.daemon_threads = True
        self._server.console = self.app  # type: ignore[attr-defined]

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.25)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = ["ConsoleServer", "ConsoleRequestHandler", "MAX_BODY_BYTES"]
