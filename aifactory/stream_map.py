"""Map LangGraph stream chunks onto dashboard SSE events.

The function is pure: it does not call a model, Docker, or the graph.
"""

from __future__ import annotations

from aifactory.agents import MAX_SANDBOX_FAILURES

NODE_NAMES = {
    "architect_agent": "architect",
    "coder_agent": "coder",
    "test_generator_agent": "test_generator",
    "docker_tester_agent": "docker_tester",
}

_ARTIFACTS = {
    "api_schema": "schema",
    "code": "code",
    "test_code": "test",
}


def map_graph_update(update: object, snapshot: dict | None = None) -> tuple[list[dict], dict]:
    """Turn one graph stream chunk into SSE events and the next snapshot.

    Accepts a ``stream_mode="tasks"`` payload, a ``stream_mode="updates"``
    dict, or a synthetic ``{"type": "exception"|"end"}`` marker used when the
    stream raises or closes without a terminal node result.
    """
    snap = _copy_snapshot(snapshot)
    if not isinstance(update, dict):
        return [], snap

    kind = update.get("type")
    if kind == "exception":
        return _exception_events(str(update.get("message") or "error"), snap)
    if kind == "end":
        return _end_events(snap)
    if _is_task_payload(update):
        return _task_events(update, snap)
    return _updates_events(update, snap)


def _copy_snapshot(snapshot: dict | None) -> dict:
    base = {
        "retry_count": 0,
        "test_result": "",
        "active_node": None,
        "finished": False,
        "coverage": None,
    }
    if snapshot:
        base.update(snapshot)
    return base


def _is_task_payload(update: dict) -> bool:
    return "name" in update and ("id" in update or "input" in update or "result" in update)


def _task_events(update: dict, snap: dict) -> tuple[list[dict], dict]:
    graph_name = str(update.get("name") or "")
    node = NODE_NAMES.get(graph_name)
    if node is None:
        return [], snap

    if "result" not in update and "error" not in update:
        snap["active_node"] = node
        return [_node(node, "running")], snap

    error = update.get("error")
    if error:
        message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        return _exception_events(message, snap, node=node)

    result = update.get("result") if isinstance(update.get("result"), dict) else {}
    return _result_events(graph_name, result, snap)


def _updates_events(update: dict, snap: dict) -> tuple[list[dict], dict]:
    events: list[dict] = []
    for graph_name, patch in update.items():
        if graph_name not in NODE_NAMES or not isinstance(patch, dict):
            continue
        step_events, snap = _result_events(graph_name, patch, snap)
        events.extend(step_events)
    return events, snap


def _result_events(graph_name: str, patch: dict, snap: dict) -> tuple[list[dict], dict]:
    node = NODE_NAMES[graph_name]
    snap["active_node"] = node
    if "retry_count" in patch and patch.get("retry_count") is not None:
        snap["retry_count"] = int(patch["retry_count"])
    if "test_result" in patch:
        snap["test_result"] = str(patch.get("test_result") or "")
    if "coverage" in patch:
        snap["coverage"] = patch.get("coverage")

    status = "failed" if graph_name == "docker_tester_agent" and snap["test_result"] == "FAILED" else "done"
    events = [_node(node, status)]
    for key, kind in _ARTIFACTS.items():
        content = patch.get(key)
        if isinstance(content, str) and content.strip():
            events.append({"event": "artifact", "data": {"kind": kind, "content": content}})

    logs = patch.get("error_logs")
    if isinstance(logs, str) and logs.strip():
        events.extend(_log(line) for line in logs.splitlines() if line.strip())
    elif graph_name == "docker_tester_agent" and snap["test_result"] == "SUCCESS":
        events.append(_log("pytest 通过。"))

    if graph_name == "docker_tester_agent" and snap["test_result"] == "FAILED":
        events.append({"event": "retry", "data": {"retry_count": int(snap["retry_count"])}})
        if int(snap["retry_count"]) >= MAX_SANDBOX_FAILURES and not snap["finished"]:
            events.append(_finish("failed", snap))
            snap["finished"] = True
    elif graph_name == "docker_tester_agent" and snap["test_result"] == "SUCCESS" and not snap["finished"]:
        events.append(_finish("success", snap))
        snap["finished"] = True
    return events, snap


def _exception_events(message: str, snap: dict, node: str | None = None) -> tuple[list[dict], dict]:
    events: list[dict] = []
    active = node or snap.get("active_node")
    if active in NODE_NAMES.values():
        events.append(_node(str(active), "failed"))
    if message.strip():
        events.append(_log(message.strip()))
    if not snap["finished"]:
        events.append(_finish("error", snap))
        snap["finished"] = True
    return events, snap


def _end_events(snap: dict) -> tuple[list[dict], dict]:
    if snap["finished"]:
        return [], snap
    if snap["test_result"] == "SUCCESS":
        status = "success"
    elif snap["test_result"] == "FAILED":
        status = "failed"
    else:
        status = "error"
    snap["finished"] = True
    return [_finish(status, snap)], snap


def _node(node: str, status: str) -> dict:
    return {"event": "node_change", "data": {"node": node, "status": status}}


def _log(line: str) -> dict:
    return {"event": "log", "data": {"line": line}}


def _finish(status: str, snap: dict) -> dict:
    coverage = snap.get("coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        coverage = None
    return {
        "event": "finish",
        "data": {
            "status": status,
            "retry_count": int(snap.get("retry_count") or 0),
            "coverage": coverage,
        },
    }
