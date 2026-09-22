"""Command line: one requirement sentence in, sandbox delivery out."""

from __future__ import annotations

import argparse

from aifactory.agents import sandbox_path
from aifactory.graph import build_graph
from aifactory.state import DEFAULT_TASK, initial_state


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aifactory",
        description="用一句话需求驱动 LangGraph 软件工厂，在 Docker 沙箱中交付 FastAPI 模块。",
    )
    parser.add_argument(
        "requirement",
        nargs="?",
        default=DEFAULT_TASK,
        help="需求描述。省略时使用默认的图书管理 API 示例。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    final = build_graph().invoke(initial_state(args.requirement))
    if final.get("test_result") == "SUCCESS":
        sandbox = sandbox_path().resolve()
        main_py = sandbox / "main.py"
        test_py = sandbox / "test_main.py"
        print("【交付】成功", flush=True)
        print(main_py, flush=True)
        print(test_py, flush=True)
        return 0
    print("【交付】失败，未能完成交付。", flush=True)
    return 1
