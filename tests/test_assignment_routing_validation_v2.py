from types import SimpleNamespace
import json

import pytest

from src.assignment_routing import assignment_input_hash
from src.models import Task
from src.orchestrator.assignment_routing import (
    AssignmentRoutingValidationError,
    validate_assignment_response,
)


def test_input_hash_is_attached_by_aq_not_copied_by_the_model() -> None:
    task = Task(id="task-1", project_id="project", title="Small fix", description="Localized")
    options = [SimpleNamespace(intelligence_class="fast-low", provider="openai")]
    response = json.dumps({"decisions": [{
        "task_id": task.id,
        "intelligence_class": "fast-low",
        "provider": None,
        "reason": "Routine localized work.",
    }]})

    decisions = validate_assignment_response(response, [task], options)

    assert decisions[0].input_hash == assignment_input_hash(task)


def test_supplied_input_hash_must_still_match_for_older_custom_playbooks() -> None:
    task = Task(id="task-1", project_id="project", title="Small fix", description="Localized")
    options = [SimpleNamespace(intelligence_class="fast-low", provider="openai")]
    response = json.dumps({"decisions": [{
        "task_id": task.id,
        "input_hash": "wrong",
        "intelligence_class": "fast-low",
        "provider": None,
        "reason": "Routine localized work.",
    }]})

    with pytest.raises(AssignmentRoutingValidationError, match="input_hash mismatch"):
        validate_assignment_response(response, [task], options)
