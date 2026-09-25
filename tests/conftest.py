"""Shared fixtures for the HTTP level tests."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from uhtline.console.server import ConsoleServer

from .support import manual_runtime


@pytest.fixture(name="live_server")
def live_server_fixture(tmp_path: Path) -> Iterator[ConsoleServer]:
    server = ConsoleServer(manual_runtime(tmp_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop()
        thread.join(timeout=5)
