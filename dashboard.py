"""Dark IDE dashboard for the AIFactory SSE pipeline."""

from __future__ import annotations

import json
from html import escape

import httpx
import pandas as pd
import sseclient
import streamlit as st

from aifactory.state import DEFAULT_TASK

API_URL = "http://localhost:8000/api/factory/run"

AGENTS = (
    ("architect", "Architect Agent"),
    ("coder", "Coder Agent"),
    ("test_generator", "Test Generator"),
    ("docker_tester", "Docker Tester"),
)

NODE_STATUS = {
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
}

STATUS_COLOR = {
    "等待中": ("#161616", "#8d8d8d", "#3a3a3a"),
    "进行中": ("#10243d", "#8ec5ff", "#2f6fed"),
    "已完成": ("#102418", "#7dffa8", "#1f8a4c"),
    "失败": ("#2a1212", "#ff8f8f", "#c44747"),
}

CSS = """
<style>
  .stApp, [data-testid="stAppViewContainer"] {
    background: #000000;
    color: #e8e8e8;
  }
  [data-testid="stHeader"] { background: #000000; }
  [data-testid="stSidebar"] {
    background: #070707;
    border-right: 1px solid #1c1c1c;
  }
  [data-testid="stSidebar"] * { color: #e8e8e8; }
  h1, h2, h3, p, label, span { color: #e8e8e8; }
  .metric-row { display: flex; gap: 12px; margin: 4px 0 18px; }
  .metric {
    flex: 1;
    background: #0c0c0c;
    border: 1px solid #242424;
    border-radius: 10px;
    padding: 14px 16px;
  }
  .metric .k {
    color: #8d8d8d;
    font-size: 12px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .metric .v {
    color: #f2f2f2;
    font-size: 28px;
    font-weight: 650;
    margin-top: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  .flow-card {
    border-radius: 10px;
    padding: 14px 14px 12px;
    margin-bottom: 10px;
    border: 1px solid #2a2a2a;
  }
  .flow-card .step {
    font-size: 11px;
    letter-spacing: 0.08em;
    color: #9a9a9a;
  }
  .flow-card .name {
    font-size: 16px;
    font-weight: 650;
    margin: 4px 0;
  }
  .flow-card .badge {
    display: inline-block;
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid currentColor;
  }
  .terminal {
    background: #000000;
    color: #b6f7c9;
    border: 1px solid #1d3a28;
    border-radius: 10px;
    padding: 14px;
    min-height: 360px;
    max-height: 560px;
    overflow: auto;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
  }
  .caret {
    display: inline-block;
    width: 8px;
    height: 14px;
    background: #3dd68c;
    margin-left: 2px;
    vertical-align: text-bottom;
    animation: blink 1s steps(1) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  div[data-testid="stCode"] { background: #070707; }
</style>
"""


def new_dashboard_state() -> dict:
    return {
        "pipeline_status": "待命",
        "retry_count": 0,
        "coverage": None,
        "nodes": {key: "等待中" for key, _label in AGENTS},
        "schema": "",
        "code": "",
        "test_code": "",
        "logs": "",
    }


def apply_event(state: dict, event: str, data: dict) -> dict:
    """Fold one SSE event into dashboard state. Does not touch Streamlit."""
    nxt = {
        "pipeline_status": state.get("pipeline_status", "待命"),
        "retry_count": int(state.get("retry_count") or 0),
        "coverage": state.get("coverage"),
        "nodes": dict(state.get("nodes") or {}),
        "schema": state.get("schema") or "",
        "code": state.get("code") or "",
        "test_code": state.get("test_code") or "",
        "logs": state.get("logs") or "",
    }
    for key, _label in AGENTS:
        nxt["nodes"].setdefault(key, "等待中")

    if event == "node_change":
        node = data.get("node")
        label = NODE_STATUS.get(str(data.get("status")), "等待中")
        if node in nxt["nodes"]:
            nxt["nodes"][node] = label
        if nxt["pipeline_status"] == "待命":
            nxt["pipeline_status"] = "运行中"
    elif event == "artifact":
        kind = data.get("kind")
        content = str(data.get("content") or "")
        if kind == "schema":
            nxt["schema"] = content
        elif kind == "code":
            nxt["code"] = content
        elif kind == "test":
            nxt["test_code"] = content
    elif event == "log":
        nxt["logs"] += str(data.get("line") or "") + "\n"
    elif event == "retry":
        nxt["retry_count"] = int(data.get("retry_count") or 0)
    elif event == "finish":
        nxt["retry_count"] = int(data.get("retry_count") or 0)
        nxt["coverage"] = data.get("coverage")
        nxt["pipeline_status"] = "完成" if data.get("status") == "success" else "异常"
    return nxt


class _ByteSource:
    """Byte iterator with close(), which sseclient-py expects from an HTTP body."""

    def __init__(self, response: httpx.Response):
        self._response = response

    def __iter__(self):
        yield from self._response.iter_bytes()

    def close(self) -> None:
        self._response.close()


def format_coverage(value: object) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return "—"


def _metrics_html(state: dict) -> str:
    cards = (
        ("流水线状态", state["pipeline_status"]),
        ("重试次数", f"{int(state['retry_count'])}/3"),
        ("测试覆盖率", format_coverage(state.get("coverage"))),
    )
    parts = ["<div class='metric-row'>"]
    for label, value in cards:
        parts.append(
            "<div class='metric'>"
            f"<div class='k'>{escape(label)}</div>"
            f"<div class='v'>{escape(str(value))}</div>"
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _flow_html(state: dict) -> str:
    frame = pd.DataFrame(
        [
            {"step": index, "node": key, "agent": label, "status": state["nodes"].get(key, "等待中")}
            for index, (key, label) in enumerate(AGENTS, start=1)
        ]
    )
    blocks = []
    for row in frame.itertuples(index=False):
        bg, fg, edge = STATUS_COLOR.get(row.status, STATUS_COLOR["等待中"])
        blocks.append(
            "<div class='flow-card' style='background:{bg};border-color:{edge}'>"
            "<div class='step'>STEP {step:02d}</div>"
            "<div class='name'>{name}</div>"
            "<span class='badge' style='color:{fg}'>{status}</span>"
            "</div>".format(
                bg=bg,
                edge=edge,
                step=int(row.step),
                name=escape(str(row.agent)),
                fg=fg,
                status=escape(str(row.status)),
            )
        )
    return "".join(blocks)


def _terminal_html(state: dict) -> str:
    body = escape(state.get("logs") or "")
    caret = "<span class='caret'></span>" if state.get("pipeline_status") == "运行中" else ""
    if not body:
        body = escape("等待 Docker 控制台输出…\n")
    return f"<div class='terminal'>{body}{caret}</div>"


def _paint(state: dict, slots: dict, *, include_downloads: bool = False) -> None:
    slots["metrics"].markdown(_metrics_html(state), unsafe_allow_html=True)
    slots["flow"].markdown(_flow_html(state), unsafe_allow_html=True)
    with slots["code"].container():
        shown = state["code"] if str(state["code"]).strip() else "# 尚未生成 main.py"
        st.code(shown, language="python")
        if include_downloads:
            st.download_button(
                "下载 main.py",
                data=state["code"],
                file_name="main.py",
                mime="text/x-python",
                key="download_main",
            )
    with slots["schema"].container():
        schema = state["schema"].strip()
        st.markdown(schema if schema else "_尚未生成 Schema_")
    with slots["test"].container():
        shown = state["test_code"] if str(state["test_code"]).strip() else "# 尚未生成 test_main.py"
        st.code(shown, language="python")
        if include_downloads:
            st.download_button(
                "下载 test_main.py",
                data=state["test_code"],
                file_name="test_main.py",
                mime="text/x-python",
                key="download_test",
            )
    slots["log"].markdown(_terminal_html(state), unsafe_allow_html=True)


def consume_stream(url: str, task: str, force_docker: bool, state: dict, slots: dict) -> dict:
    """POST the run and fold SSE events into state while the response is open."""
    payload = {"task": task, "force_docker": force_docker}
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST",
            url,
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            source = sseclient.SSEClient(_ByteSource(response))
            for incoming in source.events():
                try:
                    data = json.loads(incoming.data)
                except json.JSONDecodeError:
                    data = {"line": incoming.data}
                    name = "log"
                else:
                    name = incoming.event or "message"
                if not isinstance(data, dict):
                    data = {"line": str(data)}
                    name = "log"
                state = apply_event(state, name, data)
                _paint(state, slots, include_downloads=False)
    return state


def main() -> None:
    st.set_page_config(page_title="AIFactory", layout="wide", page_icon="🛠")
    st.markdown(CSS, unsafe_allow_html=True)
    if "factory" not in st.session_state:
        st.session_state.factory = new_dashboard_state()
    if "task_goal" not in st.session_state:
        st.session_state.task_goal = DEFAULT_TASK
    if "api_url" not in st.session_state:
        st.session_state.api_url = API_URL
    if "force_docker" not in st.session_state:
        st.session_state.force_docker = True

    st.sidebar.header("流水线")
    st.sidebar.text_area("任务目标", key="task_goal", height=110)
    st.sidebar.text_input("FastAPI URL", key="api_url")
    st.sidebar.checkbox("强制启用 Docker 沙盒", key="force_docker")
    launch = st.sidebar.button("🚀 启动自动化流水线", type="primary", use_container_width=True)

    st.title("AIFactory")
    st.caption("一句话需求 → 架构、编码、测试、Docker 沙盒。生成代码只在沙盒里执行。")
    metrics = st.empty()
    left, right = st.columns([1, 1.45], gap="large")
    with left:
        st.subheader("Flow Navigator")
        flow = st.empty()
    with right:
        st.subheader("Workspace")
        tab_code, tab_schema, tab_test, tab_log = st.tabs(
            ["代码实现", "数据结构", "测试用例", "Live Terminal Output"]
        )
        with tab_code:
            code_slot = st.empty()
        with tab_schema:
            schema_slot = st.empty()
        with tab_test:
            test_slot = st.empty()
        with tab_log:
            log_slot = st.empty()

    slots = {
        "metrics": metrics,
        "flow": flow,
        "code": code_slot,
        "schema": schema_slot,
        "test": test_slot,
        "log": log_slot,
    }
    if launch:
        st.session_state.factory = new_dashboard_state()
        st.session_state.factory["pipeline_status"] = "运行中"
        _paint(st.session_state.factory, slots, include_downloads=False)
        try:
            st.session_state.factory = consume_stream(
                st.session_state.api_url.strip() or API_URL,
                st.session_state.task_goal,
                bool(st.session_state.force_docker),
                st.session_state.factory,
                slots,
            )
        except Exception as exc:
            failed = apply_event(
                st.session_state.factory,
                "log",
                {"line": f"{type(exc).__name__}: {exc}"},
            )
            failed = apply_event(
                failed,
                "finish",
                {"status": "error", "retry_count": failed["retry_count"], "coverage": None},
            )
            st.session_state.factory = failed
    _paint(st.session_state.factory, slots, include_downloads=True)


if __name__ == "__main__":
    main()
