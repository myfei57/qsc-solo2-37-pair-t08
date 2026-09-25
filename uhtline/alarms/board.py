"""Alarm board: raise, clear and rank section alarms."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig
from ..core.ids import validate_token
from ..errors import NotFoundError, StateError, ValidationError
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore

SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES, start=1)}


class AlarmBoard:
    """Keeps the active alarms and a capped history of every transition."""

    document = "alarm-board"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.audit = audit
        self._active: dict[str, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._active = {str(key): dict(value) for key, value in (stored.payload.get("active") or {}).items()}
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "active": {key: self._active[key] for key in sorted(self._active)},
                "history": self._history[-self.config.alarm.history_limit :],
            },
        )

    def raise_alarm(
        self,
        code: str,
        *,
        severity: str,
        message: str,
        target: str = "line",
        reason: str = "",
    ) -> dict[str, Any]:
        label = validate_token(code, field_name="alarm code")
        if severity not in SEVERITIES:
            raise ValidationError("unknown alarm severity", severity=severity, permitted=list(SEVERITIES))
        existing = self._active.get(label)
        if existing is not None:
            existing["occurrences"] = int(existing.get("occurrences", 1)) + 1
            self.persist()
            return dict(existing)
        if len(self._active) >= self.config.alarm.maximum_active:
            raise StateError("the alarm board is full", active=len(self._active), maximum=self.config.alarm.maximum_active)
        alarm = {
            "code": label,
            "severity": severity,
            "message": str(message),
            "target": str(target),
            "reason": str(reason),
            "occurrences": 1,
            "raised_at": self.clock.timestamp(),
            "cleared_at": None,
        }
        self._active[label] = alarm
        self._history.append(dict(alarm))
        self.persist()
        self.audit.record("alarm-raise", label, f"{severity}: {message}", cause=None)
        return dict(alarm)

    def clear(self, code: str, *, reason: str) -> dict[str, Any]:
        label = str(code)
        alarm = self._active.pop(label, None)
        if alarm is None:
            raise NotFoundError("alarm is not active", code=label)
        alarm["cleared_at"] = self.clock.timestamp()
        alarm["clear_reason"] = str(reason)
        self._history.append(dict(alarm))
        self.persist()
        self.audit.record("alarm-clear", label, str(reason), cause=None)
        return dict(alarm)

    def active(self, *, severity: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        selected = [self._active[key] for key in sorted(self._active)]
        if severity is not None:
            selected = [alarm for alarm in selected if alarm["severity"] == severity]
        if target is not None:
            selected = [alarm for alarm in selected if alarm["target"] == str(target)]
        return [dict(alarm) for alarm in selected]

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def highest_severity(self) -> str | None:
        ranks = [SEVERITY_RANK[alarm["severity"]] for alarm in self._active.values()]
        if not ranks:
            return None
        return SEVERITIES[max(ranks) - 1]

    def counts(self) -> dict[str, Any]:
        by_severity = {name: 0 for name in SEVERITIES}
        for alarm in self._active.values():
            by_severity[alarm["severity"]] += 1
        return {
            "active": len(self._active),
            "total": len(self._history),
            "by_severity": by_severity,
            "highest": self.highest_severity(),
        }

__all__ = ["SEVERITIES", "SEVERITY_RANK", "AlarmBoard"]
