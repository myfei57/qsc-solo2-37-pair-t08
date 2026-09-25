"""Generation registry: every parameter block carries a monotonic generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..core.clock import Clock
from ..core.ids import validate_token
from ..errors import NotFoundError, StaleGenerationError, ValidationError
from ..persistence.store import DurableStore, payload_checksum


@dataclass(frozen=True)
class ParameterRevision:
    """One published generation of a parameter scope."""

    scope: str
    generation: int
    payload: dict[str, Any]
    checksum: str
    recorded_at: str
    reason: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParameterRevision":
        return cls(
            scope=str(value["scope"]),
            generation=int(value["generation"]),
            payload=dict(value.get("payload") or {}),
            checksum=str(value["checksum"]),
            recorded_at=str(value["recorded_at"]),
            reason=str(value.get("reason", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "generation": self.generation,
            "payload": self.payload,
            "checksum": self.checksum,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
        }


class GenerationRegistry:
    """Durable history of published generations, one lineage per scope."""

    document = "generations"

    def __init__(self, store: DurableStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self._history: dict[str, list[ParameterRevision]] = {}
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        scopes = stored.payload.get("scopes")
        if not isinstance(scopes, dict):
            raise ValidationError("generation document is malformed", document=self.document)
        for scope, revisions in scopes.items():
            if not isinstance(revisions, list):
                raise ValidationError("generation history is malformed", scope=scope)
            self._history[str(scope)] = [ParameterRevision.from_dict(item) for item in revisions]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "scopes": {
                    scope: [revision.as_dict() for revision in revisions]
                    for scope, revisions in sorted(self._history.items())
                }
            },
        )

    def scopes(self) -> list[str]:
        return sorted(self._history)

    def revisions(self, scope: str) -> list[ParameterRevision]:
        return list(self._history.get(scope, []))

    def revision(self, scope: str, generation: int) -> ParameterRevision | None:
        for revision in self._history.get(scope, []):
            if revision.generation == int(generation):
                return revision
        return None

    def current(self, scope: str) -> ParameterRevision:
        revisions = self._history.get(scope)
        if not revisions:
            raise NotFoundError("scope has no published generation", scope=scope)
        return revisions[-1]

    def generation(self, scope: str) -> int:
        return self.current(scope).generation

    def is_current(self, scope: str, generation: int) -> bool:
        try:
            return self.current(scope).generation == int(generation)
        except NotFoundError:
            return False

    def require_current(self, scope: str, generation: int) -> ParameterRevision:
        """Reject a caller that still holds a superseded generation."""

        revision = self.revision(scope, generation)
        if revision is None:
            raise StaleGenerationError(
                "generation was never published for this scope",
                scope=scope,
                generation=int(generation),
                current=self.generation(scope) if self._history.get(scope) else None,
            )
        if not self.is_current(scope, generation):
            raise StaleGenerationError(
                "generation is no longer current",
                scope=scope,
                generation=int(generation),
                current=self.current(scope).generation,
            )
        return revision

    def publish(self, scope: str, payload: Mapping[str, Any], *, reason: str) -> ParameterRevision:
        """Bump the scope lineage by one generation and persist the history."""

        label = validate_token(scope, field_name="scope")
        if not isinstance(payload, Mapping) or not payload:
            raise ValidationError("a published generation needs a non-empty payload", scope=label)
        body = {str(key): value for key, value in payload.items()}
        history = self._history.setdefault(label, [])
        revision = ParameterRevision(
            scope=label,
            generation=len(history) + 1,
            payload=body,
            checksum=payload_checksum(body),
            recorded_at=self.clock.timestamp(),
            reason=str(reason),
        )
        history.append(revision)
        self.persist()
        return revision

    def ensure(self, scope: str, payload: Mapping[str, Any], *, reason: str) -> ParameterRevision:
        """Publish the first generation of a scope and otherwise keep the lineage."""

        existing = self._history.get(str(scope))
        if existing:
            return existing[-1]
        return self.publish(scope, payload, reason=reason)

    def republish_if_changed(
        self,
        scope: str,
        payload: Mapping[str, Any],
        *,
        reason: str,
    ) -> ParameterRevision | None:
        """Bump the lineage only when the supplied payload differs from the head."""

        body = {str(key): value for key, value in payload.items()}
        existing = self._history.get(str(scope))
        if existing and existing[-1].checksum == payload_checksum(body):
            return None
        return self.publish(scope, body, reason=reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scopes": {
                scope: {
                    "generation": revisions[-1].generation,
                    "checksum": revisions[-1].checksum,
                    "history": len(revisions),
                }
                for scope, revisions in sorted(self._history.items())
                if revisions
            }
        }


__all__ = ["GenerationRegistry", "ParameterRevision"]
