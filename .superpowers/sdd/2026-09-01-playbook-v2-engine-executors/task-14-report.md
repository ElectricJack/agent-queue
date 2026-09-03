# T-14 returned-review correction report

## Scope and starting point

Started from historical T-14 commits at `e8c36f2f`. The returned review named two gaps:

1. no durable per-tool commit boundary / interrupted-call receipt consumption;
2. no runtime named-profile policy-subset check.

The second has a supported, server-owned seam and is corrected here. The first cannot be corrected safely inside the frozen executor interface; the exact gap is recorded below.

## RED

Added `test_named_profile_that_widens_the_invoker_is_rejected_before_provider_io`. It supplies an authoritative profile with `ensure_task` plus `list_tasks` to a playbook principal limited to `ensure_task`; a model response is queued solely to prove no provider I/O occurs on denial.

Command:

```text
aq test tests/test_llm_executor.py -q
```

Observed output before the production change:

```text
FAILED test_named_profile_that_widens_the_invoker_is_rejected_before_provider_io
AssertionError: assert 'high' == 'unauthorized'
1 failed, 9 passed
```

The failure proves the former executor treated `step.profile_id` only as the LLM intelligence class and performed no runtime capability lookup or no-widening test.

## GREEN

`LiveLlmExecutor` now:

- reads the named profile through the already-exposed `EngineServices.db.get_profile()` authority used by command-principal resolution;
- derives the effective policy with `capability_policy_for`, using the resolver's plugin-name inventory when available;
- applies `check_delegation(parent_policy, profile_policy)` and fails closed as `unauthorized` for authority failure, an unknown profile, or a widening profile;
- narrows the principal with `ExecutionPrincipal.narrow()` and assigns the named profile ID before calling the provider. The narrowed principal is therefore used for both model-visible tool publication and every dispatch-side authorization decision.

This does not infer policy from the prompt, an LLM class, or an artifact fingerprint, and it does not bypass the contract/authorization dispatch boundary.

## Verification evidence

```text
aq test tests/test_llm_executor.py -q
10 passed, 11 warnings

aq test tests/test_llm_usage.py tests/test_llm_executor.py -q
14 passed, 11 warnings

aq test tests/llm tests/test_playbook_runner.py -q
447 passed, 29 warnings

ruff check src/playbooks/executors/llm.py tests/test_llm_executor.py
All checks passed!

git diff --check
exit 0
```

The broader runner command reports existing `AsyncMockMixin._execute_mock_call was never awaited` RuntimeWarnings in `src/playbooks/runner.py`; it completed with 447 passing tests and no failures.

## Files changed

- `src/playbooks/executors/llm.py`
- `tests/test_llm_executor.py`

## Self-review

- The runtime profile is obtained only from the database-backed authority already exposed to executors; a missing/throwing lookup denies rather than falls back.
- Profile delegation is monotonic: `check_delegation` rejects a widening profile and `narrow()` intersects rather than replaces the caller policy.
- The executor continues to authorize tool publication and invocation through the existing resolver/contract boundary. No command is made runnable merely by appearing in the LLM profile.
- The new test asserts observable behavior (reserved outcome plus no provider call), not helper internals.
- No frozen Package 4 engine/base/repository interface was changed.

## Explicit unresolved interface gaps

The durable tool-turn/interrupted-call requirement remains unimplementable in this task's frozen interface:

- `LLMClient.run_tools()` owns the complete provider/tool loop and returns only after all turns finish.
- `LiveLlmExecutor.execute()` has no repository or boundary callback; it can return only one `ExecutorResult`.
- `PlaybookEngine._advance_one_step()` invokes an executor once, then `PlaybookEngine._commit()` makes exactly one `commit_boundary()` call for the step attempt.
- `ExecutorResult`, `RunSnapshot`, and `StepReceipt` carry neither an LLM transcript/tool-turn checkpoint nor a started-but-unfinished provider-call receipt that a retry can consume.

Consequently, a crash after a tool side effect but before the executor returns is still replayable, and a started provider call cannot be marked interrupted or explicitly retried from durable state. Correcting this needs a dedicated cross-cutting interface change that introduces persisted LLM turn/attempt state and engine-owned commits between tool turns; adding ad hoc writes from `llm.py` would violate the locked one-engine/one-boundary contract and bypass the run repository.
