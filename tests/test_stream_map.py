"""Mapper and SSE wiring. No OpenAI calls and no Docker."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from aifactory.agents import DOCKER_SKIPPED_LOG, docker_sandbox_enabled, docker_tester_agent
from aifactory.server import app, format_sse
from aifactory.stream_map import map_graph_update


def _start(name: str) -> dict:
    return {"id": name, "name": name, "input": {}, "triggers": ["branch:to:" + name]}


def _done(name: str, result: dict) -> dict:
    return {"id": name, "name": name, "error": None, "result": result, "interrupts": []}


class MapGraphUpdateTests(unittest.TestCase):
    def test_success_path_events(self) -> None:
        chunks = [
            _start("architect_agent"),
            _done("architect_agent", {"api_schema": "# Book\n", "retry_count": 0, "test_result": "", "error_logs": ""}),
            _start("coder_agent"),
            _done("coder_agent", {"code": "print('app')\n"}),
            _start("test_generator_agent"),
            _done("test_generator_agent", {"test_code": "def test_ok():\n    assert True\n"}),
            _start("docker_tester_agent"),
            _done("docker_tester_agent", {"test_result": "SUCCESS", "error_logs": ""}),
        ]
        snap = None
        events: list[dict] = []
        for chunk in chunks:
            step, snap = map_graph_update(chunk, snap)
            events.extend(step)

        self.assertEqual(events[0], {"event": "node_change", "data": {"node": "architect", "status": "running"}})
        self.assertIn({"event": "artifact", "data": {"kind": "schema", "content": "# Book\n"}}, events)
        self.assertIn({"event": "artifact", "data": {"kind": "code", "content": "print('app')\n"}}, events)
        self.assertIn(
            {"event": "artifact", "data": {"kind": "test", "content": "def test_ok():\n    assert True\n"}},
            events,
        )
        self.assertEqual(events[-2], {"event": "log", "data": {"line": "pytest 通过。"}})
        self.assertEqual(
            events[-1],
            {"event": "finish", "data": {"status": "success", "retry_count": 0, "coverage": None}},
        )
        self.assertEqual(sum(1 for event in events if event["event"] == "finish"), 1)

    def test_failed_retry_then_cap(self) -> None:
        snap = {"retry_count": 0, "test_result": "", "active_node": None, "finished": False, "coverage": None}
        original = dict(snap)
        first, snap = map_graph_update(
            _done(
                "docker_tester_agent",
                {"test_result": "FAILED", "error_logs": "out\nerr", "retry_count": 1},
            ),
            snap,
        )
        self.assertEqual(original["retry_count"], 0)
        names = [event["event"] for event in first]
        self.assertEqual(names, ["node_change", "log", "log", "retry"])
        self.assertEqual(first[0]["data"], {"node": "docker_tester", "status": "failed"})
        self.assertEqual(first[-1]["data"], {"retry_count": 1})
        self.assertNotIn("finish", names)

        _running, snap = map_graph_update(_start("coder_agent"), snap)
        self.assertEqual(_running[0]["data"]["status"], "running")
        _third, snap = map_graph_update(
            _done(
                "docker_tester_agent",
                {"test_result": "FAILED", "error_logs": "still bad", "retry_count": 3},
            ),
            snap,
        )
        finish = _third[-1]
        self.assertEqual(finish["event"], "finish")
        self.assertEqual(finish["data"]["status"], "failed")
        self.assertEqual(finish["data"]["retry_count"], 3)
        self.assertIsNone(finish["data"]["coverage"])

    def test_exception_after_start_finishes_with_error(self) -> None:
        _running, snap = map_graph_update(_start("architect_agent"), None)
        events, snap = map_graph_update({"type": "exception", "message": "RuntimeError: no key"}, snap)
        self.assertEqual(events[0]["data"], {"node": "architect", "status": "failed"})
        self.assertEqual(events[1], {"event": "log", "data": {"line": "RuntimeError: no key"}})
        self.assertEqual(events[2]["data"]["status"], "error")
        self.assertTrue(snap["finished"])

    def test_updates_mode_chunk(self) -> None:
        events, _snap = map_graph_update({"coder_agent": {"code": "x = 1\n"}}, None)
        self.assertEqual(events[0]["data"], {"node": "coder", "status": "done"})
        self.assertEqual(events[1], {"event": "artifact", "data": {"kind": "code", "content": "x = 1\n"}})

    def test_skipped_docker_log_and_finish(self) -> None:
        events, _snap = map_graph_update(
            _done("docker_tester_agent", {"test_result": "SUCCESS", "error_logs": DOCKER_SKIPPED_LOG}),
            None,
        )
        self.assertIn({"event": "log", "data": {"line": DOCKER_SKIPPED_LOG}}, events)
        self.assertEqual(events[-1]["data"]["status"], "success")

    def test_coverage_number_is_forwarded(self) -> None:
        events, _snap = map_graph_update(
            _done(
                "docker_tester_agent",
                {"test_result": "SUCCESS", "error_logs": "", "coverage": 87},
            ),
            None,
        )
        self.assertEqual(events[-1]["data"]["coverage"], 87)

    def test_sse_frame_keeps_json_on_one_line(self) -> None:
        frame = format_sse({"event": "log", "data": {"line": "a\nb"}})
        self.assertTrue(frame.startswith("event: log\n"))
        self.assertIn("\\n", frame)
        self.assertTrue(frame.endswith("\n\n"))


class ServerTests(unittest.TestCase):
    def test_endpoint_streams_mapped_events_without_openai(self) -> None:
        seen = {}

        class FakeGraph:
            def stream(self, state, stream_mode="tasks"):
                seen["mode"] = stream_mode
                seen["task"] = state["task_description"]
                seen["docker"] = docker_sandbox_enabled.get()
                yield _start("architect_agent")
                yield _done("architect_agent", {"api_schema": "schema", "retry_count": 0})
                raise RuntimeError("stop-early")

        from fastapi.testclient import TestClient

        with patch("aifactory.server.build_graph", return_value=FakeGraph()):
            with TestClient(app) as client:
                with client.stream(
                    "POST",
                    "/api/factory/run",
                    json={"task": "图书", "force_docker": False},
                ) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("text/event-stream", response.headers["content-type"])
                    body = "".join(response.iter_text())

        self.assertEqual(seen["task"], "图书")
        self.assertFalse(seen["docker"])
        self.assertEqual(seen["mode"], "tasks")
        self.assertIn("event: node_change", body)
        self.assertIn('"status": "running"', body)
        self.assertIn("event: finish", body)
        self.assertIn('"status": "error"', body)
        payload = body.split("data: ", 1)[1].split("\n", 1)[0]
        self.assertEqual(json.loads(payload)["node"], "architect")

    def test_force_docker_defaults_to_true(self) -> None:
        seen = {}

        class FakeGraph:
            def stream(self, state, stream_mode="tasks"):
                seen["docker"] = docker_sandbox_enabled.get()
                yield _done("docker_tester_agent", {"test_result": "SUCCESS", "error_logs": ""})

        from fastapi.testclient import TestClient

        with patch("aifactory.server.build_graph", return_value=FakeGraph()):
            with TestClient(app) as client:
                response = client.post("/api/factory/run", json={"task": "只测默认"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(seen["docker"])
        self.assertIn('"status": "success"', response.text)


class SkipDockerTests(unittest.TestCase):
    def test_disabled_sandbox_does_not_call_docker(self) -> None:
        token = docker_sandbox_enabled.set(False)
        try:
            with patch("aifactory.agents.subprocess.run") as run:
                result = docker_tester_agent(
                    {
                        "task_description": "demo",
                        "api_schema": "",
                        "code": "print(1)\n",
                        "test_code": "def test_ok():\n    assert True\n",
                        "test_result": "",
                        "error_logs": "",
                        "retry_count": 0,
                    }
                )
            run.assert_not_called()
        finally:
            docker_sandbox_enabled.reset(token)
        self.assertEqual(result["test_result"], "SUCCESS")
        self.assertEqual(result["error_logs"], DOCKER_SKIPPED_LOG)


class DashboardStateTests(unittest.TestCase):
    def test_events_survive_in_dashboard_state(self) -> None:
        from dashboard import apply_event, format_coverage, new_dashboard_state

        state = new_dashboard_state()
        state = apply_event(state, "node_change", {"node": "architect", "status": "running"})
        state = apply_event(state, "artifact", {"kind": "code", "content": "print(1)\n"})
        state = apply_event(state, "log", {"line": "hello"})
        state = apply_event(state, "retry", {"retry_count": 1})
        state = apply_event(
            state,
            "finish",
            {"status": "success", "retry_count": 1, "coverage": 80},
        )
        self.assertEqual(state["nodes"]["architect"], "进行中")
        self.assertEqual(state["code"], "print(1)\n")
        self.assertEqual(state["logs"], "hello\n")
        self.assertEqual(state["retry_count"], 1)
        self.assertEqual(state["pipeline_status"], "完成")
        self.assertEqual(format_coverage(None), "—")
        self.assertEqual(format_coverage(80), "80")
        self.assertEqual(state["nodes"]["coder"], "等待中")


if __name__ == "__main__":
    unittest.main()
