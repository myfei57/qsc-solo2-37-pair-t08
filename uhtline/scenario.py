"""Deterministic scenarios that exercise the cross-module contracts."""

from __future__ import annotations

from typing import Any, Callable

from .app.runtime import Runtime, build_runtime
from .core.clock import ManualClock
from .errors import ServiceError

SENSOR = "TS-STERILE"
PREHEAT_SENSOR = "TS-PREHEAT"


def _attempt(action: Callable[[], Any]) -> dict[str, Any]:
    """Run an action and report either its result or the structured failure."""

    try:
        value = action()
    except ServiceError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message, "details": exc.details}
    return {"ok": True, "result": value}


def _open_batch(runtime: Runtime, batch_id: str = "B-2001") -> None:
    runtime.control.open_batch(batch_id, "milk", reason="scenario start")


def _sterile_run(runtime: Runtime) -> dict[str, Any]:
    control = runtime.control
    steps: list[dict[str, Any]] = []
    _open_batch(runtime)
    steps.append({"step": "open-batch", **_attempt(lambda: control.start_intake(reason="scenario"))})
    steps.append(
        {
            "step": "receive",
            **_attempt(lambda: control.receive(800.0, 6.0, batch_id="B-2001", reason="scenario", key="sc-1")),
        }
    )
    steps.append({"step": "commit-intake", **_attempt(lambda: control.commit_records())})
    steps.append({"step": "charge-balance", **_attempt(lambda: control.charge_balance(600.0, reason="scenario"))})
    steps.append(
        {
            "step": "persist-preheat",
            **_attempt(lambda: control.persist_preheat_temperature(PREHEAT_SENSOR, 87.4, reason="scenario")),
        }
    )
    steps.append({"step": "ramp", **_attempt(lambda: control.start_sterilization_ramp(137.0, reason="scenario"))})
    confirmation: dict[str, Any] = {}

    def confirm() -> dict[str, Any]:
        nonlocal confirmation
        confirmation = control.confirm_sterilization(reason="scenario")
        return confirmation

    steps.append({"step": "confirm", **_attempt(confirm)})
    steps.append(
        {
            "step": "sterilize-tank",
            **_attempt(
                lambda: control.sterilize_aseptic_tank(
                    confirmation_id=str(confirmation.get("confirmation_id")),
                    reason="scenario",
                )
            ),
        }
    )
    steps.append({"step": "hold", **_attempt(lambda: control.start_hold(reason="scenario"))})
    baseline: dict[str, Any] = {}

    def baseline_step() -> dict[str, Any]:
        nonlocal baseline
        baseline = control.confirm_flow_baseline(reason="scenario")
        return baseline

    steps.append({"step": "baseline", **_attempt(baseline_step)})
    steps.append(
        {
            "step": "dwell",
            **_attempt(
                lambda: control.evaluate_dwell(
                    baseline_id=str(baseline.get("baseline_id")),
                    raw_lph=11200.0,
                    reason="scenario",
                )
            ),
        }
    )
    steps.append({"step": "cool", **_attempt(lambda: control.start_cooling(reason="scenario"))})
    steps.append({"step": "fill", **_attempt(lambda: control.fill_aseptic_tank(700.0, reason="scenario", key="sc-2"))})
    steps.append({"step": "commit-fill", **_attempt(lambda: control.commit_records())})
    steps.append({"step": "complete", **_attempt(lambda: control.complete_batch("released", reason="scenario"))})
    return {"scenario": "sterile-run", "stage": control.stages.current().value, "steps": steps}


def _restart_recovery(runtime: Runtime) -> dict[str, Any]:
    control = runtime.control
    steps: list[dict[str, Any]] = []
    _open_batch(runtime, "B-2002")
    control.start_intake(reason="scenario")
    steps.append({"step": "receive", **_attempt(lambda: control.receive(700.0, 5.5, batch_id="B-2002", reason="scenario"))})
    steps.append({"step": "commit", **_attempt(lambda: control.commit_records())})
    steps.append({"step": "receive-staged", **_attempt(lambda: control.receive(300.0, 5.0, batch_id="B-2002", reason="scenario"))})
    before = control.stream_state()
    reloaded = build_runtime(runtime.config, runtime.store.root, ManualClock())
    after = reloaded.control.stream_state()
    return {
        "scenario": "restart-recovery",
        "steps": steps,
        "before_restart": before,
        "after_restart": after,
        "visible_after_restart": [item["kind"] for item in reloaded.control.visible_records(10)],
        "pending_after_restart": [item["kind"] for item in reloaded.control.pending_records()],
    }


def _recalibration_invalidates(runtime: Runtime) -> dict[str, Any]:
    control = runtime.control
    steps: list[dict[str, Any]] = []
    _open_batch(runtime, "B-2003")
    control.start_intake(reason="scenario")
    control.receive(700.0, 5.0, batch_id="B-2003", reason="scenario")
    control.charge_balance(500.0, reason="scenario")
    control.persist_preheat_temperature(PREHEAT_SENSOR, 87.0, reason="scenario")
    control.start_sterilization_ramp(137.0, reason="scenario")
    confirmation = control.confirm_sterilization(reason="scenario")
    steps.append({"step": "confirm", "ok": True, "confirmation_id": confirmation["confirmation_id"]})
    steps.append(
        {"step": "recalibrate", **_attempt(lambda: control.recalibrate_temperature(SENSOR, 1.01, 0.2, reason="recalibration"))}
    )
    steps.append(
        {
            "step": "sterilize-with-stale-confirmation",
            **_attempt(
                lambda: control.sterilize_aseptic_tank(
                    confirmation_id=confirmation["confirmation_id"],
                    reason="scenario",
                )
            ),
        }
    )
    fresh = control.confirm_sterilization(reason="scenario re-confirm")
    steps.append(
        {
            "step": "sterilize-with-fresh-confirmation",
            **_attempt(
                lambda: control.sterilize_aseptic_tank(confirmation_id=fresh["confirmation_id"], reason="scenario")
            ),
        }
    )
    return {"scenario": "recalibration-invalidates", "steps": steps, "warranties": control.warranties.state_counts()}


def _shutdown_order(runtime: Runtime) -> dict[str, Any]:
    control = runtime.control
    steps: list[dict[str, Any]] = []
    _open_batch(runtime, "B-2004")
    control.start_intake(reason="scenario")
    control.receive(700.0, 5.0, batch_id="B-2004", reason="scenario")
    control.charge_balance(500.0, reason="scenario")
    control.persist_preheat_temperature(PREHEAT_SENSOR, 87.0, reason="scenario")
    control.start_sterilization_ramp(137.0, reason="scenario")
    control.confirm_sterilization(reason="scenario")
    control.start_hold(reason="scenario")
    control.start_cooling(reason="scenario")
    steps.append({"step": "cooling-before-sterilization", **_attempt(lambda: control.stop_cooling(reason="scenario"))})
    steps.append({"step": "shutdown", **_attempt(lambda: control.shutdown(reason="scenario"))})
    return {"scenario": "shutdown-order", "steps": steps, "stage": control.stages.current().value}


def _cleaning_latch(runtime: Runtime) -> dict[str, Any]:
    control = runtime.control
    steps: list[dict[str, Any]] = []
    steps.append({"step": "enter-cleaning", **_attempt(lambda: control.start_cleaning(reason="scenario"))})
    steps.append(
        {"step": "confirm", **_attempt(lambda: control.confirm_cleaning_temperature(81.0, reason="scenario"))}
    )
    steps.append({"step": "alarm", **_attempt(lambda: control.cip.raise_alarm(reason="deviation detected"))})
    steps.append(
        {
            "step": "reset-with-cold-water",
            **_attempt(lambda: control.cip.reset_alarm(value_c=42.0, reason="operator reset")),
        }
    )
    steps.append(
        {
            "step": "reset-with-hot-water",
            **_attempt(lambda: control.cip.reset_alarm(value_c=79.5, reason="operator reset")),
        }
    )
    steps.append({"step": "start-pump", **_attempt(lambda: control.start_cleaning_pump(6000.0, reason="scenario"))})
    cycle = runtime.config.cleaning.minimum_cycle_seconds + 30.0
    steps.append({"step": "complete", **_attempt(lambda: control.finish_cleaning(cycle, reason="scenario"))})
    return {"scenario": "cleaning-latch", "steps": steps, "stage": control.stages.current().value}


def _hold_latch(runtime: Runtime) -> dict[str, Any]:
    control = runtime.control
    steps: list[dict[str, Any]] = []
    baseline = control.confirm_flow_baseline(reason="scenario baseline")
    short = control.evaluate_dwell(baseline_id=baseline["baseline_id"], raw_lph=17500.0, reason="high flow")
    steps.append({"step": "short-dwell", "dwell": short})
    steps.append(
        {
            "step": "recover-below-window",
            **_attempt(
                lambda: control.recover_hold(
                    130.0,
                    baseline_id=baseline["baseline_id"],
                    raw_lph=12000.0,
                    reason="operator",
                )
            ),
        }
    )
    steps.append(
        {
            "step": "recover-inside-window",
            **_attempt(
                lambda: control.recover_hold(
                    137.0,
                    baseline_id=baseline["baseline_id"],
                    raw_lph=12000.0,
                    reason="operator",
                )
            ),
        }
    )
    return {"scenario": "hold-latch", "steps": steps, "bypass_active": control.hold.bypass_active()}


SCENARIOS: dict[str, Callable[[Runtime], dict[str, Any]]] = {
    "sterile-run": _sterile_run,
    "restart-recovery": _restart_recovery,
    "recalibration-invalidates": _recalibration_invalidates,
    "shutdown-order": _shutdown_order,
    "cleaning-latch": _cleaning_latch,
    "hold-latch": _hold_latch,
}


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


def run_scenario(runtime: Runtime, name: str) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario: {name}")
    result = SCENARIOS[name](runtime)
    runtime.persist()
    return result


__all__ = ["SCENARIOS", "run_scenario", "scenario_names"]
