"""Shared state for the software factory graph."""

from typing import TypedDict

DEFAULT_TASK = (
    "写一个图书管理 API，包含添加图书、获取图书列表、按 ID 查询图书，以及带简单库存扣减逻辑。"
)

SANDBOX_DIR_NAME = "sandbox_workspace"


class FactoryState(TypedDict):
    task_description: str
    api_schema: str
    code: str
    test_code: str
    test_result: str
    error_logs: str
    retry_count: int


def initial_state(task_description: str) -> FactoryState:
    return {
        "task_description": task_description,
        "api_schema": "",
        "code": "",
        "test_code": "",
        "test_result": "",
        "error_logs": "",
        "retry_count": 0,
    }
