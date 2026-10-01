# Worker context and continuation checkpoints

Task: `azure-vault-92.3` (2026-10-01).

Long workers checkpoint continuation state before native harness compaction.
The daemon never clears, kills, restarts or releases a live worker to reduce
context. Claims, waits, workspaces, routing and human/verification gates retain
their existing owners and semantics. Small tasks remain whole.

`sessions.worker_context_compact_tokens` defaults to 160000, accepts 100000–1000000,
and accepts 0 to inherit harness defaults. It applies only at new task/pool
launches: Claude receives `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; Codex receives
`-c model_auto_compact_token_limit=...`. Explicit harness overrides win. Named
sessions and other executables receive no derived setting. These are native
compaction controls, not a guaranteed hard bound on an in-flight response.
Claude's setting is a window, whose native trigger includes its own headroom.

`worker_context_checkpoint_tokens` defaults to 120000 (0 disables measured
checkpoint guidance); it must not exceed an enabled compact window.
`worker_context_unknown_checkpoint_turns` defaults to 40 (positive): without a
fresh metric, checkpoint at logical boundaries and at most this many tool turns
apart. Turn counts are a fallback cadence, never token or quota estimates.

Prime and handoff may read at most 256 KiB from the end of the session's pinned
transcript. Latest request input is Claude input + cache creation + cache read,
or Codex last-token-usage input (already inclusive of cached input). Output,
cumulative usage, model window size and quota readings are not context metrics.
Readings older than five minutes, malformed/missing readings, and a compaction
boundary after the last reading are unknown. Original transcript bytes remain
untouched and the snapshot retains their path for retrieval.

Both shipped harnesses use PreCompact to save an automatic, note-only snapshot,
and SessionStart on compact/resume to re-prime before continuing. Empty automatic
hooks save facts-only recovery without displacing useful agent notes. A snapshot
contains task/claim/session/workspace/branch/HEAD, active jobs, active waits
and result pointers, open gates, subtask progress and metric provenance.
Stores that cannot be read are explicitly unknown, not an empty success.

Agent notes additionally carry constraints and exact verification/error evidence
or retrievable paths, plus the next action, outstanding wait, completed work and
approaches not to repeat. Text remains bounded to 8 KiB at ingestion; oversize
input is rejected, never silently truncated. Wake presentation trims optional
completed-work prose first. Constraints/evidence that cannot fit after escaping
are replaced by a mandatory full-note retrieval pointer, never a partial quote.

Tool guidance asks workers to save verbose successful output to retrievable files
and present concise summaries, preserving full error evidence and instructions.
No transcript rewrite or lossy general tool-output filter is introduced.

Validation uses launch-spec tests, a long-session compaction-hook simulation with
real PostgreSQL identity/waits/gates, bounded-reader tests and a before/after
synthetic output comparison. Synthetic context/turn reductions are labelled;
production token savings are not claimed without a subsequent measured audit.

Native controls verified against [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[Codex hooks](https://learn.chatgpt.com/docs/hooks), and
[Claude environment variables](https://code.claude.com/docs/en/env-vars).

## Verification evidence

The operator's corrected audit confirms 368 calls for the largest sampled worker,
mean request input context 349868.73 tokens and peak 588548 tokens. This is the
recorded production baseline, not a quota percentage.

`test_long_worker_compaction_checkpoint_preserves_claim_job_wait_gate_and_next_action`
exercises enforced worker capabilities and real PostgreSQL jobs/waits/gates. A
170000-token synthetic reading reaches checkpoint guidance; a native-hook
simulation records a facts-only snapshot and re-primes at a simulated 25000 tokens.
The claim/session/workspace identity, one existing validation job, its active wait,
the human gate, exact error evidence and next action survive. No restart event or
duplicate job is created. Resolved wait result pointers also remain retrievable.
This validates orchestration and continuation, not a live harness's summarizer.

`test_repetitive_success_history_projects_to_one_note_with_exact_failure_and_raw_logs`
compares 40 repeated success-result blocks with one continuation note. The recorded
synthetic comparison is 720784 raw bytes versus 499 wake bytes; zero repeated
success blocks remain to reread, and one note contains the unchanged gate, exact
failure and next action. Original raw output remains retrievable. This is a
fixture comparison of reread material, not measured LLM reread turns or production
savings. Follow-up production auditing must measure those after deployment.

Focused and related area validation passed 635 tests. Ruff on the changed Python
files passed. Generated API/CLI/configuration/selection artifacts and both clients
were regenerated without installing packages or changing the shared environment.
