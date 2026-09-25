"""Read-only HTML pages rendered from the control snapshots."""

from __future__ import annotations

from typing import Any

PAGE_NAMES: tuple[str, ...] = ("overview", "records", "decisions", "cleaning")

STYLE = """
body { font-family: system-ui, sans-serif; margin: 0; background: #10151c; color: #e6edf3; }
header { padding: 18px 24px; background: #182230; border-bottom: 1px solid #27364a; }
nav a { color: #9fd0ff; margin-right: 16px; text-decoration: none; }
main { padding: 20px 24px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 24px; }
th, td { border: 1px solid #27364a; padding: 6px 10px; text-align: left; font-size: 14px; }
th { background: #1b2735; }
.pass, .ok { color: #5ddf9a; }
.fail, .blocked { color: #ff8b8b; }
"""


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table(title: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"<h2>{_escape(title)}</h2><p>no rows</p>"
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    head = "".join(f"<th>{_escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(row.get(column))}</td>" for column in columns) + "</tr>" for row in rows
    )
    return f"<h2>{_escape(title)}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _page(body: str) -> str:
    nav = "".join(f'<a href="/{name}">{name}</a>' for name in PAGE_NAMES)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>line console</title>"
        f"<style>{STYLE}</style></head><body><header><nav>{nav}</nav></header>"
        f"<main>{body}</main></body></html>"
    )


def render_overview(snapshot: dict[str, Any], health: dict[str, Any]) -> str:
    stage = snapshot["stage"]
    rows = [
        {"field": "stage", "value": stage["stage"]},
        {"field": "transitions", "value": stage["transitions"]},
        {"field": "watermark", "value": health["records"]["watermark"]},
        {"field": "visible records", "value": health["records"]["visible"]},
        {"field": "pending records", "value": health["records"]["pending"]},
        {"field": "alarms", "value": health["alarms"]["active"]},
        {"field": "audit valid", "value": health["audit_valid"]},
    ]
    gate_rows = snapshot["gates"]
    latch_rows = snapshot["latches"]
    return _page(
        _table("line state", rows)
        + _table("permits", [{"gate": gate["name"], "state": gate["state"], "evidence": gate["evidence"]} for gate in gate_rows])
        + _table("latches", [{"latch": latch["name"], "active": latch["active"], "reason": latch["reason"]} for latch in latch_rows])
    )


def render_records(control_snapshot: dict[str, Any], visible: list[dict[str, Any]], pending: list[dict[str, Any]]) -> str:
    return _page(
        _table("committed records", [{"sequence": item["sequence"], "id": item["record_id"], "kind": item["kind"]} for item in visible])
        + _table("staged records", [{"sequence": item["sequence"], "id": item["record_id"], "kind": item["kind"]} for item in pending])
        + _table("stream", [{"field": key, "value": value} for key, value in sorted(control_snapshot["records"].items())])
    )


def render_decisions(control_snapshot: dict[str, Any], decisions: list[dict[str, Any]]) -> str:
    counts = control_snapshot["decisions"]
    return _page(
        _table("decision counts", [{"field": key, "value": value} for key, value in sorted(counts["by_verdict"].items())])
        + _table(
            "recent decisions",
            [
                {
                    "id": item["decision_id"],
                    "kind": item["kind"],
                    "subject": item["subject"],
                    "verdict": item["verdict"],
                    "batch": item["batch_id"],
                }
                for item in decisions
            ],
        )
    )


def render_cleaning(control_snapshot: dict[str, Any], alarms: list[dict[str, Any]]) -> str:
    cleaning = control_snapshot["cleaning"]
    rows = [{"field": key, "value": value} for key, value in sorted(cleaning.items()) if key != "history"]
    return _page(
        _table("cleaning section", rows)
        + _table("active alarms", [{"code": alarm["code"], "severity": alarm["severity"], "message": alarm["message"]} for alarm in alarms])
    )


__all__ = [
    "PAGE_NAMES",
    "render_cleaning",
    "render_decisions",
    "render_overview",
    "render_records",
]
