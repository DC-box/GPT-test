"""Graph compile and routing checks. No OpenAI calls and no Docker."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.graph import END

from aifactory.agents import docker_run_command, docker_tester_agent, get_llm
from aifactory.fencing import strip_markdown_fences
from aifactory.graph import CODER_NODE, build_graph, route_after_docker
from aifactory.state import DEFAULT_TASK, initial_state


class RouteTests(unittest.TestCase):
    def test_success_ends(self) -> None:
        self.assertEqual(
            route_after_docker({"test_result": "SUCCESS", "retry_count": 1}),
            END,
        )

    def test_failed_retry_returns_coder(self) -> None:
        self.assertEqual(
            route_after_docker({"test_result": "FAILED", "retry_count": 1}),
            "coder_agent",
        )

    def test_failed_at_cap_ends(self) -> None:
        self.assertEqual(
            route_after_docker({"test_result": "FAILED", "retry_count": 3}),
            END,
        )


class CompileTests(unittest.TestCase):
    def test_compile_does_not_construct_llm(self) -> None:
        with patch("langchain_openai.ChatOpenAI", side_effect=AssertionError("llm")):
            app = build_graph()
        self.assertIsNotNone(app)
        nodes = set(app.get_graph().nodes)
        self.assertTrue(
            {
                "architect_agent",
                "coder_agent",
                "test_generator_agent",
                "docker_tester_agent",
            }.issubset(nodes)
        )

    def test_success_path_ends_without_providers(self) -> None:
        def architect(state):
            return {"api_schema": "schema", "retry_count": 0, "error_logs": "", "test_result": ""}

        def coder(state):
            return {"code": "print('ok')\n"}

        def testgen(state):
            return {"test_code": "def test_ok():\n    assert True\n"}

        def docker(state):
            return {"test_result": "SUCCESS", "error_logs": ""}

        with (
            patch("aifactory.graph.architect_agent", architect),
            patch("aifactory.graph.coder_agent", coder),
            patch("aifactory.graph.test_generator_agent", testgen),
            patch("aifactory.graph.docker_tester_agent", docker),
        ):
            final = build_graph().invoke(initial_state("demo"))
        self.assertEqual(final["test_result"], "SUCCESS")
        self.assertEqual(final["error_logs"], "")

    def test_three_failures_stop_at_coder_cap(self) -> None:
        coder_attempts: list[int] = []

        def architect(state):
            return {"api_schema": "schema", "retry_count": 0, "error_logs": "", "test_result": ""}

        def coder(state):
            coder_attempts.append(int(state.get("retry_count") or 0))
            return {"code": "code"}

        def testgen(state):
            return {"test_code": "tests"}

        def docker(state):
            retry_count = int(state.get("retry_count") or 0) + 1
            return {"test_result": "FAILED", "error_logs": "boom", "retry_count": retry_count}

        with (
            patch("aifactory.graph.architect_agent", architect),
            patch("aifactory.graph.coder_agent", coder),
            patch("aifactory.graph.test_generator_agent", testgen),
            patch("aifactory.graph.docker_tester_agent", docker),
        ):
            final = build_graph().invoke(initial_state("demo"))
        self.assertEqual(coder_attempts, [0, 1, 2])
        self.assertEqual(final["retry_count"], 3)
        self.assertEqual(final["test_result"], "FAILED")
        self.assertEqual(CODER_NODE, "coder_agent")


class FenceTests(unittest.TestCase):
    def test_outer_python_fence_keeps_interior_backticks(self) -> None:
        raw = "```python\nx = '```'\nprint(x)\n```"
        self.assertEqual(strip_markdown_fences(raw), "x = '```'\nprint(x)\n")

    def test_bare_fence(self) -> None:
        raw = "```\nprint('ok')\n```"
        self.assertEqual(strip_markdown_fences(raw), "print('ok')\n")

    def test_unfenced_backticks_are_not_deleted(self) -> None:
        raw = "x = '```python'\nprint(x)\n"
        self.assertEqual(strip_markdown_fences(raw), raw)

    def test_prose_around_one_fence(self) -> None:
        raw = "下面是代码：\n```python\nprint(1)\n```"
        self.assertEqual(strip_markdown_fences(raw), "print(1)\n")


class DockerTesterTests(unittest.TestCase):
    def _state(self) -> dict:
        return {
            "task_description": "demo",
            "api_schema": "schema",
            "code": "print('app')\n",
            "test_code": "def test_ok():\n    assert True\n",
            "test_result": "",
            "error_logs": "old",
            "retry_count": 0,
        }

    def test_command_uses_absolute_mount_and_exact_shell(self) -> None:
        command = docker_run_command(Path("sandbox_workspace"))
        mount = command[command.index("-v") + 1]
        host, guest = mount.split(":")
        self.assertTrue(Path(host).is_absolute())
        self.assertEqual(guest, "/app")
        self.assertEqual(command[-1], "pip install fastapi httpx pytest pydantic -q && pytest test_main.py")
        self.assertEqual(command[0], "docker")

    def test_success_clears_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AIFACTORY_SANDBOX": tmp}):
                completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
                with patch("aifactory.agents.subprocess.run", return_value=completed) as run:
                    result = docker_tester_agent(self._state())
                self.assertEqual(run.call_args.kwargs["timeout"], 60)
                self.assertEqual(result["test_result"], "SUCCESS")
                self.assertEqual(result["error_logs"], "")
                self.assertNotIn("retry_count", result)
                sandbox = Path(tmp)
                self.assertEqual((sandbox / "main.py").read_text(encoding="utf-8"), "print('app')\n")
                self.assertTrue((sandbox / "test_main.py").exists())

    def test_nonzero_pytest_is_failed_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AIFACTORY_SANDBOX": tmp}):
                completed = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="out\n", stderr="err\n"
                )
                with patch("aifactory.agents.subprocess.run", return_value=completed):
                    result = docker_tester_agent(self._state())
        self.assertEqual(result["test_result"], "FAILED")
        self.assertEqual(result["retry_count"], 1)
        self.assertIn("out\n", result["error_logs"])
        self.assertIn("err\n", result["error_logs"])

    def test_timeout_and_missing_docker_are_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AIFACTORY_SANDBOX": tmp}):
                timeout = subprocess.TimeoutExpired(cmd="docker", timeout=60)
                with patch("aifactory.agents.subprocess.run", side_effect=timeout):
                    timed = docker_tester_agent(self._state())
                with patch(
                    "aifactory.agents.subprocess.run",
                    side_effect=FileNotFoundError("docker"),
                ):
                    missing = docker_tester_agent(self._state())
        self.assertEqual(timed["test_result"], "FAILED")
        self.assertIn("TimeoutExpired", timed["error_logs"])
        self.assertEqual(timed["retry_count"], 1)
        self.assertEqual(missing["test_result"], "FAILED")
        self.assertIn("FileNotFoundError", missing["error_logs"])
        self.assertEqual(missing["retry_count"], 1)


class CliTests(unittest.TestCase):
    def test_default_requirement(self) -> None:
        from aifactory.cli import parse_args

        self.assertEqual(parse_args([]).requirement, DEFAULT_TASK)

    def test_success_prints_paths_and_failure_exits(self) -> None:
        from aifactory.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AIFACTORY_SANDBOX": tmp}):
                with patch("aifactory.cli.build_graph") as build:
                    build.return_value.invoke.return_value = {"test_result": "SUCCESS"}
                    code = main(["自定义需求"])
                    state = build.return_value.invoke.call_args.args[0]
                    self.assertEqual(state["task_description"], "自定义需求")
                    self.assertEqual(code, 0)
                    build.return_value.invoke.return_value = {"test_result": "FAILED", "retry_count": 3}
                    self.assertEqual(main(["自定义需求"]), 1)


class LlmConstructionTests(unittest.TestCase):
    def test_model_settings_and_no_inline_key(self) -> None:
        with patch("langchain_openai.ChatOpenAI") as cls:
            get_llm()
        cls.assert_called_once_with(model="gpt-4o-mini", temperature=0)


if __name__ == "__main__":
    unittest.main()
