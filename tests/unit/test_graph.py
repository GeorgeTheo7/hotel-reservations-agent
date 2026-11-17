"""The run graph's shape (D75): the existing steps as nodes, in D71's order."""

from anemos.graph import run_graph

START, END = "__start__", "__end__"


def test_the_run_graph_has_the_steps_as_nodes_in_order() -> None:
    graph = run_graph().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert set(graph.nodes) == {
        START, "claim", "extract", "store_and_route", "agent_turn", "tool", "final_write",
        "await_owner", "summarise", "write_escalation", END,
    }  # fmt: skip
    assert edges == {
        (START, "claim"),
        ("claim", "extract"),
        ("claim", END),  # a thread reply: no model call (D70)
        ("extract", "store_and_route"),
        ("store_and_route", END),  # set aside, escalated by code, discarded
        ("store_and_route", "await_owner"),  # the stub drafter stored a draft
        ("store_and_route", "summarise"),  # escalated by code: summary first (D81)
        ("summarise", "write_escalation"),  # ... then its final transaction
        ("write_escalation", END),
        ("agent_turn", "summarise"),  # the loop ended in an escalation (D81)
        ("tool", "summarise"),
        ("summarise", "final_write"),
        ("store_and_route", "agent_turn"),  # D71 phase 1 committed
        ("agent_turn", "tool"),
        ("agent_turn", "agent_turn"),  # a rejected call, answered and counted
        ("agent_turn", "final_write"),
        ("tool", "agent_turn"),
        ("tool", "final_write"),  # a tool result the rules end at (D11)
        ("final_write", END),  # escalated, discarded
        ("final_write", "await_owner"),  # a draft stored: interrupt (D75)
        ("await_owner", END),  # resumed after the owner's action
    }
