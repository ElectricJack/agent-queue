import pytest

from src.event_bus import EventBus, EventValidationError


@pytest.mark.parametrize("name", ["agent.question", "agent.question.updated"])
async def test_dev_bus_rejects_question_without_session_provenance(name):
    bus = EventBus(env="dev")
    with pytest.raises(EventValidationError, match="session_id"):
        await bus.emit(name, {"id": "q", "question": "Where is the config?"})
