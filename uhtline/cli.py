"""Command line entry points for serving, inspection and scenarios."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .app.runtime import build_runtime, restore_runtime
from .console.server import ConsoleServer
from .core.config import default_config, diff_configs, envelope_report, fast_test_config
from .errors import ServiceError
from .scenario import run_scenario, scenario_names


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uhtline", description="Process line control service")
    parser.add_argument("--data-dir", default=str(Path.cwd() / "data"), help="durable state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the HTTP console")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--quiet", action="store_true")

    status = subparsers.add_parser("status", help="print the live line state")
    status.add_argument("--health", action="store_true", help="print the health envelope instead")
    status.add_argument("--recovery", action="store_true", help="print the recovery report")

    subparsers.add_parser("stage", help="print the stage machine snapshot")

    records = subparsers.add_parser("records", help="inspect the record stream")
    records.add_argument("--pending", action="store_true")
    records.add_argument("--kind")
    records.add_argument("--limit", type=int, default=50)

    decisions = subparsers.add_parser("decisions", help="inspect the decision log")
    decisions.add_argument("--kind")
    decisions.add_argument("--verdict")
    decisions.add_argument("--batch")
    decisions.add_argument("--limit", type=int, default=50)

    subparsers.add_parser("generations", help="print the published generation lineages")
    subparsers.add_parser("warranties", help="print confirmations, snapshots and baselines")
    subparsers.add_parser("batches", help="print the batch registry")
    subparsers.add_parser("sensors", help="print the sensor map")

    alarms = subparsers.add_parser("alarms", help="inspect the alarm board")
    alarms.add_argument("--limit", type=int, default=20)

    audit = subparsers.add_parser("audit", help="inspect the audit chain")
    audit.add_argument("--limit", type=int, default=25)
    audit.add_argument("--target")

    precheck = subparsers.add_parser("precheck", help="read-only action precheck")
    precheck.add_argument("action")

    config = subparsers.add_parser("config", help="inspect the configured envelope")
    config.add_argument("--compare-default", action="store_true")

    scenario = subparsers.add_parser("scenario", help="run a deterministic scenario")
    scenario.add_argument("name", nargs="?", choices=scenario_names() + ["list"])
    scenario.add_argument("--fast", action="store_true", help="use the shortened commissioning envelope")

    step = subparsers.add_parser("step", help="advance the deterministic clock")
    step.add_argument("--seconds", type=float, default=1.0)

    commit = subparsers.add_parser("commit", help="advance the record watermark")
    commit.add_argument("--through", type=int)

    void = subparsers.add_parser("void", help="tombstone one record")
    void.add_argument("record_id")
    void.add_argument("--reason", default="operator rollback")

    backup = subparsers.add_parser("backup", help="write a durable backup image")
    backup.add_argument("--destination", required=True)

    restore = subparsers.add_parser("restore", help="restore a backup image")
    restore.add_argument("--source", required=True)

    return parser


def _run_server(
    args: argparse.Namespace,
    runtime: Any,
    quiet: bool,
    server_factory: type[ConsoleServer] = ConsoleServer,
) -> int:
    server = server_factory(runtime, host=args.host, port=args.port)
    if not quiet:
        print(f"console listening on http://{server.host}:{server.port}")
        print(f"health check at http://{server.host}:{server.port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    finally:
        runtime.persist()
    return 0


def main(argv: Sequence[str] | None = None, *, server_factory: type[ConsoleServer] = ConsoleServer) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command
    selected = fast_test_config() if command == "scenario" and getattr(args, "fast", False) else default_config()
    runtime = build_runtime(selected, args.data_dir)
    control = runtime.control
    if command == "serve":
        return _run_server(args, runtime, args.quiet, server_factory)
    if command == "status":
        if args.health:
            payload = control.health()
            payload["recovery"] = runtime.recovery_report()
            _json_print(payload)
        elif args.recovery:
            _json_print(runtime.recovery_report())
        else:
            _json_print(control.snapshot())
        return 0
    if command == "stage":
        _json_print(control.stages.snapshot())
        return 0
    if command == "records":
        if args.pending:
            _json_print({"pending": control.pending_records(), "state": control.stream_state()})
        elif args.kind:
            _json_print({"records": control.records_of_kind(args.kind), "state": control.stream_state()})
        else:
            _json_print({"visible": control.visible_records(args.limit), "state": control.stream_state()})
        return 0
    if command == "decisions":
        _json_print(
            {
                "counts": control.decisions.counts(),
                "decisions": control.decision_history(
                    kind=args.kind,
                    verdict=args.verdict,
                    batch_id=args.batch,
                    limit=args.limit,
                ),
            }
        )
        return 0
    if command == "generations":
        _json_print(control.generations.as_dict())
        return 0
    if command == "warranties":
        _json_print({"states": control.warranties.state_counts(), "inventory": control.warranties.inventory()})
        return 0
    if command == "batches":
        _json_print(control.batches.snapshot())
        return 0
    if command == "sensors":
        _json_print({"sensors": control.temperatures(), "flow_gain": control.flowmeter.gain})
        return 0
    if command == "alarms":
        _json_print(
            {
                "counts": control.alarms.counts(),
                "active": control.alarms.active(),
                "history": control.alarms.history(limit=args.limit),
            }
        )
        return 0
    if command == "audit":
        _json_print(
            {
                "integrity": control.audit.verify(),
                "entries": [entry.as_dict() for entry in control.audit.entries(target=args.target, limit=args.limit)],
                "targets": control.audit.targets(),
            }
        )
        return 0
    if command == "precheck":
        _json_print(control.precheck(args.action))
        return 0
    if command == "config":
        payload = envelope_report(runtime.config)
        if args.compare_default:
            payload["differences"] = diff_configs(default_config(), runtime.config)
        _json_print(payload)
        return 0
    if command == "scenario":
        if args.name in {None, "list"}:
            _json_print({"scenarios": scenario_names()})
            return 0
        _json_print(run_scenario(runtime, args.name))
        return 0
    if command == "step":
        _json_print(control.advance_time(args.seconds))
        runtime.persist()
        return 0
    if command == "commit":
        _json_print(control.commit_records(args.through))
        runtime.persist()
        return 0
    if command == "void":
        _json_print(control.void_record(args.record_id, reason=args.reason))
        runtime.persist()
        return 0
    if command == "backup":
        _json_print(runtime.backup(Path(args.destination)))
        return 0
    if command == "restore":
        recovered = restore_runtime(default_config(), args.data_dir, args.source)
        report = recovered.recovery_report()
        report["image"] = str(Path(args.source))
        _json_print(report)
        return 0
    parser.error(f"unsupported command: {command}")
    return 2


def run() -> None:
    try:
        raise SystemExit(main())
    except ServiceError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)


__all__ = ["main", "run"]
