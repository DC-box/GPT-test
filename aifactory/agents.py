"""Architect, coder, test generator, and Docker sandbox tester."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from aifactory.fencing import strip_markdown_fences
from aifactory.state import SANDBOX_DIR_NAME, FactoryState

DOCKER_IMAGE = "python:3.10-slim"
DOCKER_SHELL_CMD = "pip install fastapi httpx pytest pydantic -q && pytest test_main.py"
DOCKER_TIMEOUT_SECONDS = 60
MAX_SANDBOX_FAILURES = 3


def get_llm():
    """Chat model. The key is read from OPENAI_API_KEY by the client itself."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model="gpt-4o-mini", temperature=0)


def message_text(message: object) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                chunks.append(str(text))
            else:
                text = getattr(block, "text", None)
                chunks.append(str(text if text is not None else block))
        return "".join(chunks)
    return str(content)


def sandbox_path() -> Path:
    override = os.environ.get("AIFACTORY_SANDBOX")
    if override:
        return Path(override)
    return Path(SANDBOX_DIR_NAME)


def docker_run_command(sandbox: Path) -> list[str]:
    """Exact sandbox command. The mount source is an absolute path."""
    host_dir = str(sandbox.resolve())
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{host_dir}:/app",
        "-w",
        "/app",
        DOCKER_IMAGE,
        "bash",
        "-c",
        DOCKER_SHELL_CMD,
    ]


def architect_agent(state: FactoryState) -> dict:
    print("【架构师】正在设计 FastAPI 的 Pydantic 模型与路由契约。", flush=True)
    llm = get_llm()
    prompt = (
        "你是一名资深软件架构师。根据需求，只设计 FastAPI 的 Pydantic 模型与路由契约，"
        "不要写完整业务实现，不要写测试。\n"
        "输出应包含：请求/响应模型的字段、类型与校验；每个路由的方法、路径、"
        "请求体、响应体与状态码。数据保存在进程内存中，不使用数据库。\n\n"
        f"需求：\n{state.get('task_description', '')}"
    )
    schema = message_text(llm.invoke(prompt)).strip()
    return {
        "api_schema": schema,
        "retry_count": 0,
        "error_logs": "",
        "test_result": "",
    }


def coder_agent(state: FactoryState) -> dict:
    retry_count = int(state.get("retry_count") or 0)
    print(f"【编码】尝试编号 {retry_count}，正在编写 FastAPI 模块。", flush=True)
    llm = get_llm()
    schema = state.get("api_schema", "")
    if retry_count > 0:
        print(f"【重试】尝试编号 {retry_count}，正在根据错误日志修复。", flush=True)
        prompt = (
            "你是一名资深 Python 工程师。下面的 FastAPI 模块未通过 pytest。"
            "请根据错误日志修复 bug，并输出修复后的完整模块。\n"
            "约束：\n"
            "- 只输出 Python 代码，不要 Markdown，不要解释。\n"
            "- 单个模块可被 `from main import app` 导入。\n"
            "- 使用 FastAPI 与 Pydantic，数据放在内存里，不要数据库、不要访问网络。\n"
            "- 依赖仅限 Python 标准库、fastapi、pydantic。\n\n"
            f"API 契约：\n{schema}\n\n"
            f"当前代码：\n{state.get('code', '')}\n\n"
            f"错误日志：\n{state.get('error_logs', '')}"
        )
    else:
        prompt = (
            "你是一名资深 Python 工程师。根据 API 契约编写一个完整可运行的 FastAPI 模块。\n"
            "约束：\n"
            "- 只输出 Python 代码，不要 Markdown，不要解释。\n"
            "- 单个模块可被 `from main import app` 导入，变量名必须是 app。\n"
            "- 使用 FastAPI 与 Pydantic，数据放在内存里，不要数据库、不要访问网络。\n"
            "- 依赖仅限 Python 标准库、fastapi、pydantic。\n\n"
            f"API 契约：\n{schema}"
        )
    code = strip_markdown_fences(message_text(llm.invoke(prompt)))
    return {"code": code}


def test_generator_agent(state: FactoryState) -> dict:
    print("【测试】正在生成基于 TestClient 的 pytest。", flush=True)
    llm = get_llm()
    prompt = (
        "你是一名测试工程师。针对下面的 FastAPI 模块编写 pytest。\n"
        "约束：\n"
        "- 只输出 Python 代码，不要 Markdown，不要解释。\n"
        "- 使用 fastapi.testclient.TestClient。\n"
        "- 通过 `from main import app` 导入应用。\n"
        "- 覆盖主要路由的成功路径；有库存扣减时覆盖库存不足或扣减后的数量。\n"
        "- 不要启动真实网络服务，不要访问外部系统。\n\n"
        f"API 契约：\n{state.get('api_schema', '')}\n\n"
        f"实现代码：\n{state.get('code', '')}"
    )
    test_code = strip_markdown_fences(message_text(llm.invoke(prompt)))
    return {"test_code": test_code}


def _failed(state: FactoryState, logs: str) -> dict:
    retry_count = int(state.get("retry_count") or 0) + 1
    print(f"【沙箱】测试失败，retry_count={retry_count}。", flush=True)
    if retry_count < MAX_SANDBOX_FAILURES:
        print(f"【重试】retry_count={retry_count}，将回到编码节点。", flush=True)
    else:
        print(f"【重试】retry_count={retry_count}，已达到上限，停止重试。", flush=True)
    return {
        "test_result": "FAILED",
        "error_logs": logs,
        "retry_count": retry_count,
    }


def docker_tester_agent(state: FactoryState) -> dict:
    print("【沙箱】正在 Docker 中运行 pytest。", flush=True)
    try:
        sandbox = sandbox_path()
        sandbox.mkdir(parents=True, exist_ok=True)
        (sandbox / "main.py").write_text(state.get("code") or "", encoding="utf-8")
        (sandbox / "test_main.py").write_text(state.get("test_code") or "", encoding="utf-8")
        command = docker_run_command(sandbox)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:
        return _failed(state, f"{type(exc).__name__}: {exc}")

    if completed.returncode == 0:
        print("【沙箱】pytest 通过。", flush=True)
        return {"test_result": "SUCCESS", "error_logs": ""}

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    logs = f"{stdout}{stderr}"
    if not logs.strip():
        logs = f"pytest exited with code {completed.returncode}"
    return _failed(state, logs)
