# Benchmark routing and evidence export

AQ's bound project routing policy can name benchmark arms under
`benchmark_arms`. Each arm has an exact `class` and `harness`, the requested
model ID, and allowlisted observed model IDs or globs. Freeze the policy
digest before filing the cohort. The class must occur in `class_order`, and
the class's installed mapping must be checked against the requested model.
Do not change a live class mapping to switch cohorts.

```yaml
benchmark_arms:
  opus55:
    class: deep-high
    harness: claude
    requested_model: claude-opus-5-5
    observed_models: [claude-opus-5-5*]
```

File a task with exactly one `benchmark:<arm>` label. The router uses only
that policy cell, pins provider intent, and holds the arm when its provider
is unavailable. A missing or multiple selector is refused. Each routed
task stores the arm, requested model, observed-model allowlist and policy
digest; archive retains the route. For an arm whose exact model is unavailable,
hold the cohort and record that it was unmeasured. Do not relabel a substitute.
The routed model is a request; the token ledger's model is transcript
evidence, and the report checks it separately. Codex usage takes its model
from the transcript's `turn_context`; that source is identified as a
transcript setting, not a provider response. A missing context remains
unknown.

Use a frozen manifest listing every comparison attempt, including attempts
that failed before a model call. One task ID belongs to one paired attempt.
Include every scoring, repair and finalization task in the pair's `task_ids`.
Separate shared setup tasks and declare any allocation rule outside the
paired total. The export reads active and archived tasks, every session
attempt, and every token row for exactly those IDs.

```json
{
  "version": 1,
  "project_id": "my-project",
  "policy_sha256": "sha256:<frozen-policy-digest>",
  "rate_card_version": "benchmark-2026-09-28",
  "arms": {
    "opus55": {
      "class": "deep-high",
      "harness": "claude",
      "requested_model": "claude-opus-5-5",
      "observed_models": ["claude-opus-5-5*"]
    }
  },
  "pairs": [
    {
      "specimen": "angular-rock",
      "arm": "opus55",
      "attempt": "1",
      "task_ids": ["rock-author-1", "rock-score-1"],
      "provider_charges_usd": {"image": null, "tool": null, "retry": null},
      "stage_spans": [
        {"stage": "bake", "clock": "monotonic", "duration_ms": 1300}
      ]
    }
  ]
}
```

```bash
aq benchmark export --manifest cohort.json --output cohort-report.json
aq benchmark export --manifest cohort.json --opencode-export \
  'rock-author-1:<AQ-session-attempt-id>:opencode-session.json' \
  --output cohort-report.json
```

Create the OpenCode JSON with `opencode export <session-id>` and associate
it with the exact AQ task and session attempt. The importer records the
export's SHA-256 and OpenCode's `providerID`/`modelID` for each assistant
message. It does not add OpenCode token counts to the AQ ledger a second
time. An OpenCode model without export evidence stays unknown.

`stage_spans` are measurements from an external monotonic timer; AQ attempt
wall time is exported separately and can include waits. For AQ task stages,
record the span directly using `aq benchmark stage-record --task-id ...
--span-id ... --stage bake --started-monotonic-ns ...
--ended-monotonic-ns ...`. Use a stable span ID per measurement, capture both
timestamps on the same host and monotonic clock, and repeat the command on a
transport retry. The daemon deduplicates the span ID. A worker's session
attempt ID is captured when the span is recorded. Missing stages are
listed, never given a zero duration. The report lists each failed or
incomplete pair, every session retry and every ledger call. A historical
row with no stable attempt/call ID stays unattributed. Unknown image, tool,
retry or cache charges keep `cost_complete` false; the priced subtotal is
an estimated API cost, not subscription cash cost. The report includes the
effective rate-card hash so later exports can be compared against the same
price inputs.
