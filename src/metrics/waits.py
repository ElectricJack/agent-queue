"""Attach configured reference values to observed wait accounting."""

from src.waits.decision import project_policy


def with_wait_policy(snapshot, policy):
    if snapshot is None:
        return None
    for pid, row in snapshot["by_project"].items():
        effective = project_policy(policy, pid)
        row["resume_cost_minutes"] = effective.resume_cost_minutes
        for duration in row["durations"]:
            kind = effective.kinds.get(duration["condition_kind"])
            duration["expected_duration_seconds"] = kind.expected_duration_seconds if kind else None
    baseline = policy.metrics
    if baseline.pre_rollout_turn_rate is not None and baseline.cohort and baseline.window:
        overhead = snapshot["overhead_tokens_1h"]
        tokens = None
        if baseline.pre_rollout_tokens_per_turn is not None and overhead is not None:
            tokens = (
                baseline.pre_rollout_turn_rate
                * snapshot["wait_seconds_1h"]
                / 60
                * baseline.pre_rollout_tokens_per_turn
                - overhead
            )
        snapshot["tokens_saved_counterfactual"] = {
            "tokens": tokens,
            "cohort": baseline.cohort,
            "window": baseline.window,
        }
    return snapshot
