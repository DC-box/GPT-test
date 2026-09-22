"""SSE API in front of the existing factory graph."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aifactory.agents import docker_sandbox_enabled
from aifactory.graph import build_graph
from aifactory.state import initial_state
from aifactory.stream_map import map_graph_update

app = FastAPI(title="AIFactory")


class FactoryRunRequest(BaseModel):
    task: str
    force_docker: bool = Field(default=True)


def format_sse(event: dict) -> str:
    payload = json.dumps(event["data"], ensure_ascii=False)
    return f"event: {event['event']}\ndata: {payload}\n\n"


@app.post("/api/factory/run")
def run_factory(body: FactoryRunRequest) -> StreamingResponse:
    """Run the compiled factory graph and stream node, artifact, log, retry, and finish events."""

    async def generate():
        # Starlette pulls a sync generator via the threadpool, one next() at a
        # time, which drops ContextVar state. Run the whole graph on one thread
        # and hand frames back as they are produced.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def produce() -> None:
            token = docker_sandbox_enabled.set(body.force_docker)
            snap: dict = {}
            finished = False
            try:
                try:
                    graph = build_graph()
                    for chunk in graph.stream(initial_state(body.task), stream_mode="tasks"):
                        events, snap = map_graph_update(chunk, snap)
                        for event in events:
                            if event["event"] == "finish":
                                finished = True
                            loop.call_soon_threadsafe(queue.put_nowait, format_sse(event))
                except Exception as exc:
                    events, snap = map_graph_update(
                        {"type": "exception", "message": f"{type(exc).__name__}: {exc}"},
                        snap,
                    )
                    for event in events:
                        if event["event"] == "finish":
                            finished = True
                        loop.call_soon_threadsafe(queue.put_nowait, format_sse(event))
                if not finished:
                    events, _snap = map_graph_update({"type": "end"}, snap)
                    for event in events:
                        loop.call_soon_threadsafe(queue.put_nowait, format_sse(event))
            finally:
                docker_sandbox_enabled.reset(token)
                loop.call_soon_threadsafe(queue.put_nowait, None)

        worker = asyncio.create_task(asyncio.to_thread(produce))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            await worker

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
