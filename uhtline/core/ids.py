"""Deterministic identifier helpers used by the durable sub-systems."""

from __future__ import annotations

import re
from typing import Mapping

from ..errors import ValidationError

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def validate_token(value: str, *, field_name: str = "identifier", maximum: int = 64) -> str:
    """Return a trimmed identifier token or raise a validation failure."""

    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string", field=field_name)
    token = value.strip()
    if not token:
        raise ValidationError(f"{field_name} must not be empty", field=field_name)
    if len(token) > maximum:
        raise ValidationError(f"{field_name} is too long", field=field_name, maximum=maximum, length=len(token))
    if not TOKEN_PATTERN.match(token):
        raise ValidationError(f"{field_name} contains unsupported characters", field=field_name, value=value)
    return token


def format_identifier(prefix: str, sequence: int, width: int = 5) -> str:
    """Render a stable identifier from a prefix and a 1-based sequence number."""

    if sequence < 1:
        raise ValidationError("identifier sequence starts at one", prefix=prefix, sequence=sequence)
    return f"{validate_token(prefix, field_name='prefix')}-{sequence:0{width}d}"


def parse_sequence(identifier: str, prefix: str) -> int:
    """Recover the sequence number previously embedded by ``format_identifier``."""

    head, _, tail = identifier.partition("-")
    if head != prefix or not tail.isdigit():
        raise ValidationError("identifier does not belong to the requested prefix", identifier=identifier, prefix=prefix)
    return int(tail)


class IdentifierFactory:
    """Per-prefix counters; deterministic so two runs produce identical identifiers."""

    def __init__(self, starts: Mapping[str, int] | None = None, width: int = 5) -> None:
        self.width = width
        self._counters: dict[str, int] = {}
        for prefix, value in (starts or {}).items():
            self.seed(prefix, value)

    def seed(self, prefix: str, next_sequence: int) -> None:
        validate_token(prefix, field_name="prefix")
        if next_sequence < 1:
            raise ValidationError("identifier counters start at one", prefix=prefix, next_sequence=next_sequence)
        self._counters[prefix] = int(next_sequence)

    def peek(self, prefix: str) -> int:
        return self._counters.get(prefix, 1)

    def next(self, prefix: str) -> str:
        sequence = self.peek(prefix)
        identifier = format_identifier(prefix, sequence, self.width)
        self._counters[prefix] = sequence + 1
        return identifier

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self._counters.items()))


__all__ = ["IdentifierFactory", "format_identifier", "parse_sequence", "validate_token"]
