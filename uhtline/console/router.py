"""Small path router used by the standard-library HTTP server."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import ValidationError

Handler = Callable[..., Any]


@dataclass
class Route:
    method: str
    pattern: str
    handler: Handler
    description: str
    regex: re.Pattern[str] = field(init=False)
    names: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        names: list[str] = []
        pieces: list[str] = []
        cursor = 0
        for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", self.pattern):
            pieces.append(re.escape(self.pattern[cursor : match.start()]))
            names.append(match.group(1))
            pieces.append(f"(?P<{match.group(1)}>[^/]+)")
            cursor = match.end()
        pieces.append(re.escape(self.pattern[cursor:]))
        self.regex = re.compile("^" + "".join(pieces) + "$")
        self.names = tuple(names)

    def match(self, path: str) -> dict[str, str] | None:
        found = self.regex.match(path)
        return None if found is None else found.groupdict()


class Router:
    """Registers route patterns and resolves one request path."""

    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, description: str) -> None:
        normalized = method.upper()
        if any(route.method == normalized and route.pattern == pattern for route in self._routes):
            raise ValidationError("duplicate route", method=normalized, pattern=pattern)
        self._routes.append(Route(normalized, pattern, handler, description))

    def get(self, pattern: str, handler: Handler, description: str) -> None:
        self.add("GET", pattern, handler, description)

    def post(self, pattern: str, handler: Handler, description: str) -> None:
        self.add("POST", pattern, handler, description)

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        normalized = method.upper()
        allowed: set[str] = set()
        for route in self._routes:
            params = route.match(path)
            if params is None:
                continue
            if route.method != normalized:
                allowed.add(route.method)
                continue
            return route.handler, params
        if allowed:
            raise ValidationError("method is not allowed", path=path, method=normalized, allowed=sorted(allowed))
        raise ValidationError("route not found", path=path, method=normalized)

    def inventory(self) -> list[dict[str, Any]]:
        return [
            {
                "method": route.method,
                "pattern": route.pattern,
                "description": route.description,
                "parameters": list(route.names),
            }
            for route in self._routes
        ]

    def count(self) -> int:
        return len(self._routes)


__all__ = ["Handler", "Route", "Router"]
