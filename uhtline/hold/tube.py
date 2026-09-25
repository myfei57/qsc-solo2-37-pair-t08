"""Hold tube: dwell follows the accepted flow baseline, not the raw reading."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig
from ..errors import RangeError
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore
from ..stages import latches as latch_names
from ..stages.latches import LatchBoard
from ..versioning.warranties import Baseline, WarrantyBook

HOLD_VOLUME_LITRES = 20.0


class HoldTube:
    """Derives dwell time from a baseline and latches the bypass valve on loss."""

    document = "hold-tube"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        latches: LatchBoard,
        warranties: WarrantyBook,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.latches = latches
        self.warranties = warranties
        self.audit = audit
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(self.document, {"history": self._history})

    def dwell_seconds(self, litres_per_hour: float) -> float:
        if float(litres_per_hour) <= 0:
            raise RangeError("flow must be positive to derive dwell", field="litres_per_hour", value=float(litres_per_hour))
        return round(HOLD_VOLUME_LITRES / (float(litres_per_hour) / 3600.0), 4)

    def evaluate(
        self,
        *,
        baseline_id: str,
        raw_lph: float,
        reason: str,
    ) -> dict[str, Any]:
        """Reject a baseline that belongs to a superseded flow generation."""

        baseline: Baseline = self.warranties.require_baseline(baseline_id, scope="flow-calibration")
        gain = float(baseline.payload["gain"])
        corrected = round(float(raw_lph) * gain, 4)
        envelope = self.config.temperature
        flow_envelope = self.config.flow
        dwell = self.dwell_seconds(corrected)
        if not flow_envelope.minimum_litres_per_hour <= corrected <= flow_envelope.maximum_litres_per_hour:
            verdict = "flow-out-of-range"
        elif dwell < envelope.hold_minimum_seconds:
            verdict = "short"
        elif dwell > envelope.hold_maximum_seconds:
            verdict = "long"
        else:
            verdict = "pass"
        if verdict != "pass":
            self.latches.set(
                latch_names.HOLD_BYPASS,
                reason=f"dwell {verdict}",
                detail=f"{dwell:g}s outside [{envelope.hold_minimum_seconds:g}, {envelope.hold_maximum_seconds:g}]",
            )
        entry = {
            "action": "dwell",
            "baseline_id": baseline.baseline_id,
            "baseline_generation": baseline.generation,
            "gain": gain,
            "raw_lph": float(raw_lph),
            "corrected_lph": corrected,
            "dwell_seconds": dwell,
            "verdict": verdict,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record("hold-dwell", "hold", f"{verdict} at {dwell:g} s", cause=None)
        return dict(entry)

    def recover(
        self,
        value_c: float,
        *,
        baseline_id: str,
        raw_lph: float,
        reason: str,
    ) -> dict[str, Any]:
        """Clear the bypass latch only when temperature and dwell both recover."""

        in_spec = self.config.temperature.sterilization_in_spec(float(value_c))
        dwell = self.evaluate(baseline_id=baseline_id, raw_lph=raw_lph, reason=reason)
        satisfied = bool(in_spec and dwell["verdict"] == "pass")
        latch = self.latches.clear(
            latch_names.HOLD_BYPASS,
            reason=reason,
            satisfied=satisfied,
            detail=f"temperature_in_spec={in_spec} dwell={dwell['verdict']}",
        )
        self.audit.record("hold-recover", "hold", f"cleared={not latch.active}", cause=None)
        return {"latch": latch.as_dict(), "dwell": dwell, "temperature_in_spec": in_spec, "cleared": not latch.active}

    def bypass_active(self) -> bool:
        return self.latches.is_active(latch_names.HOLD_BYPASS)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def snapshot(self) -> dict[str, Any]:
        latest = None if not self._history else dict(self._history[-1])
        return {
            "hold_volume_litres": HOLD_VOLUME_LITRES,
            "bypass_active": self.bypass_active(),
            "latest": latest,
            "history": self.history(5),
        }


__all__ = ["HOLD_VOLUME_LITRES", "HoldTube"]
