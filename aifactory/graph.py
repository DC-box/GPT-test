"""Factory graph: architect → coder → tests → Docker, with a 3-failure cap."""

from langgraph.graph import END, START, StateGraph

from aifactory.agents import (
    MAX_SANDBOX_FAILURES,
    architect_agent,
    coder_agent,
    docker_tester_agent,
    test_generator_agent,
)
from aifactory.state import FactoryState

CODER_NODE = "coder_agent"


def route_after_docker(state: FactoryState) -> str:
    """SUCCESS ends. FAILED retries the coder while retry_count < 3, else ends."""
    if state.get("test_result") == "SUCCESS":
        return END
    if int(state.get("retry_count") or 0) < MAX_SANDBOX_FAILURES:
        return CODER_NODE
    return END


def build_graph():
    graph = StateGraph(FactoryState)
    graph.add_node("architect_agent", architect_agent)
    graph.add_node(CODER_NODE, coder_agent)
    graph.add_node("test_generator_agent", test_generator_agent)
    graph.add_node("docker_tester_agent", docker_tester_agent)

    graph.add_edge(START, "architect_agent")
    graph.add_edge("architect_agent", CODER_NODE)
    graph.add_edge(CODER_NODE, "test_generator_agent")
    graph.add_edge("test_generator_agent", "docker_tester_agent")
    graph.add_conditional_edges(
        "docker_tester_agent",
        route_after_docker,
        {
            CODER_NODE: CODER_NODE,
            END: END,
        },
    )
    return graph.compile()
