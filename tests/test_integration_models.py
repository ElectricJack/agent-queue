"""Speculative CI is opt-in and requires independent hosted compute."""

import pytest
from pydantic import ValidationError

from src.integration.models import HierarchicalIntegrationPolicy, IntegrationTrainPolicy


def policy(**updates):
    from tests.test_integration_protection import POLICY

    return {**POLICY.model_dump(mode="json"), **updates}


def test_speculative_defaults_do_not_change_snapshots():
    dumped = IntegrationTrainPolicy().model_dump(mode="json")
    assert dumped == {"cadence_seconds": 300, "settling_cap_seconds": 1800}
    assert not IntegrationTrainPolicy().speculative_ci


@pytest.mark.parametrize("source", ["local", "hybrid"])
def test_speculative_ci_refuses_local_and_hybrid_root(source):
    with pytest.raises(ValidationError, match="speculative_ci_requires_hosted_root"):
        HierarchicalIntegrationPolicy.model_validate(policy(
            train={"speculative_ci": True, "speculative_public_hosted": True},
            ci={"source": source, "commands": {name: "python -m pytest tests/test_integration_train.py" for name in
                                               policy()["root"]["required_checks"]["names"]}},
        ))


@pytest.mark.parametrize("p90", [None, 60, 61])
def test_speculation_requires_public_free_hosted_or_subminute_measurement(p90):
    with pytest.raises(ValidationError, match="speculative_ci_compute_not_eligible"):
        IntegrationTrainPolicy(speculative_ci=True, speculative_batch_ci_p90_seconds=p90)


@pytest.mark.parametrize("evidence", [
    {"speculative_public_hosted": True}, {"speculative_batch_ci_p90_seconds": 59.9},
])
def test_hosted_speculation_accepts_eligible_compute(evidence):
    parsed = HierarchicalIntegrationPolicy.model_validate(policy(
        train={"speculative_ci": True, **evidence}, ci={"root": "hosted"},
    ))
    assert parsed.train.speculative_ci
    assert parsed.model_dump()["train"]["speculative_ci"] is True
