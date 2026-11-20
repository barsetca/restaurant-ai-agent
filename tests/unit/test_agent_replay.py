from unittest.mock import AsyncMock

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_agent_graph
from app.agent.service import AgentService


class FakeOrderDialog:
    def __init__(self):
        self.calls = 0

    async def classify(self, state):
        del state
        return "order", {"action": "clear"}

    async def handle(self, state):
        del state
        self.calls += 1
        return {
            "response": f"mutation-{self.calls}",
            "draft_revision": self.calls,
            "error_code": None,
        }


class DestructiveOrderDialog:
    def __init__(self):
        self.clears = 0

    async def classify(self, state):
        if state.get("callback_action") == "confirm_new_order":
            return "order", {"action": "confirm_new_order"}
        return "order", {"action": "new_order"}

    async def handle(self, state):
        if state["action_proposal"]["action"] == "new_order":
            return {
                "response": "confirm",
                "order_stage": "awaiting_new_order_confirmation",
                "pending_destructive_action": {
                    "action": "new_order",
                    "return_stage": "idle",
                    "confirmation_id": "session00001",
                },
                "draft_revision": state.get("draft_revision", 1),
            }
        self.clears += 1
        return {
            "response": "cleared",
            "order_stage": "idle",
            "cart": [],
            "pending_destructive_action": None,
            "draft_revision": state.get("draft_revision", 1) + 1,
        }


async def test_duplicate_and_out_of_order_updates_do_not_repeat_mutation() -> None:
    order = FakeOrderDialog()
    graph = build_agent_graph(
        AsyncMock(), checkpointer=InMemorySaver(), order_dialog=order
    )
    service = AgentService(graph, persistence_required=True)

    assert await service.handle_message("one", "thread", update_id=100) == "mutation-1"
    assert await service.handle_message("one", "thread", update_id=100) == "mutation-1"
    assert (
        await service.handle_message("old", "thread", update_id=99)
        == "Это сообщение уже обработано."
    )
    assert order.calls == 1

    state = await graph.aget_state({"configurable": {"thread_id": "thread"}})
    assert state.values["draft_revision"] == 1
    assert state.values["highest_update_id"] == 100
    assert len(state.values["processed_updates"]) == 1
    assert len(state.values["recent_interactions"]) == 1

    for update_id in range(101, 141):
        await service.handle_message("next", "thread", update_id=update_id)
    bounded = await graph.aget_state({"configurable": {"thread_id": "thread"}})
    assert len(bounded.values["processed_updates"]) == 32
    assert bounded.values["highest_update_id"] == 140
    assert len(bounded.values["recent_interactions"]) == 10


async def test_duplicate_confirm_new_order_update_clears_exactly_once() -> None:
    order = DestructiveOrderDialog()
    graph = build_agent_graph(
        AsyncMock(), checkpointer=InMemorySaver(), order_dialog=order
    )
    service = AgentService(graph, persistence_required=True)
    config = {"configurable": {"thread_id": "destructive-thread"}}

    await graph.aupdate_state(
        config,
        {
            "cart": [
                {"dish_id": "00000000-0000-0000-0000-000000000001", "quantity": 1}
            ],
            "order_stage": "idle",
            "draft_revision": 1,
        },
    )
    prompted = await service.handle_message_result(
        "Новый заказ", "destructive-thread", update_id=200
    )
    assert prompted.order_stage == "awaiting_new_order_confirmation"

    first = await service.handle_message_result(
        "Начать новый заказ",
        "destructive-thread",
        update_id=201,
        callback_action="confirm_new_order",
        callback_revision=1,
        callback_confirmation_id="session00001",
    )
    replay = await service.handle_message_result(
        "Начать новый заказ",
        "destructive-thread",
        update_id=201,
        callback_action="confirm_new_order",
        callback_revision=1,
        callback_confirmation_id="session00001",
    )
    assert first.text == replay.text == "cleared"
    assert order.clears == 1
    state = await graph.aget_state(config)
    assert state.values["cart"] == []
    assert state.values["draft_revision"] == 2
