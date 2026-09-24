# Wake context compaction — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [agent sleep/wake and durable waits](2026-09-24-agent-sleep-wake.md) ·
[exclusive job queue](2026-09-24-exclusive-job-queue.md) ·
[managed long-running commands](2026-09-24-managed-long-running-commands.md) ·
[smart test selection](2026-09-24-smart-test-selection.md) ·
`docs/specs/design/aq-surface.md` §5–§6 · `docs/specs/design/session-runtime.md` §4.2, §5 ·
`docs/specs/provider-failover.md` (D13 hand-off)

## 1. The ask

"Context compaction at the handoff, so the wake-up doesn't dump a huge fresh context."

When an agent goes dormant waiting on a job ([sleep/wake](2026-09-24-agent-sleep-wake.md))
and is woken with the result, the first turn after waking should carry **what the agent
needs to continue** — not its entire prior transcript, a duplicated prime document and a
multi-megabyte test log. Two separable problems:

1. **Agent-state compaction** — what the agent knew and intended before it slept.
2. **Result compaction** — what the job produced, reduced to what the agent must act on.

## 2. What exists today

### 2.1 What a woken agent receives now, by wake path

| Wake path | What loads | Source |
|---|---|---|
| Live idle session nudged (sleep/wake option A) | Its whole live context + one line ``Handle `aq message status <id> --json`.`` | `_render_nudge`, `src/messages/delivery.py:388`; one message per nudge (`pending[:1]`) |
| Claude relaunched with `--resume <key>` (stall restart, rate-limit cooldown, supervisor wake) | The **entire prior transcript**, then the `SessionStart` hook re-runs `aq prime --hook-json` because its matcher is `resume\|compact` | `src/prime/templates/hooks/claude.json`; resume key carried by `_carry_resume_key` (`src/sessions/reconciler.py:1984`), read back in `_launch_session_for_task` (`src/orchestrator/execution.py:885`) |
| Fresh launch (Codex/Gemini: `resume.style: none`; or a Claude key whose transcript is missing — `_validated_resume_key`, `execution.py:672`) | The full prime document + whatever hand-off note exists | `PrimeRenderer.render_for_task` (`src/prime/renderer.py`) |

So a resumed Claude session wakes with *more* than it slept with: prior transcript + a
second copy of prime. `hook_envelopes.suppressed()` (`src/prime/hook_envelopes.py`) only
suppresses the hook when the bootstrap argv already delivered prime *on this start*; resume
and compaction re-prime by design ("compaction is exactly when re-priming pays for itself").

### 2.2 The prime document

`src/prime/` assembles ten sections (`aq-surface.md` §5.2): role, project role, task pointer,
task context (incl. inlined `spec_ref` sections, the last 5 comments truncated at 1,400 chars
each, the provider-failover hand-off), workspaces, **messages + latest hand-off note**
(`build_messages_section`, `src/prime/sections.py:454`), memory slots (empty while memory is
paused), tool guidance, completion protocol. It has a size *estimate*
(`PrimeDocument.tokens_est`, chars/4, `src/prime/models.py:121`) but **no budget**. A
`## Subtasks` block renders the task's durable checklist (titles only).

`src/prompt_builder.py` is not on this path: it builds prompts for the supervisor's and
playbooks' direct LLM calls, with advisory tier budgets (warn at 2×). Its budget idea is
reusable; its code is not the task-session path.

### 2.3 Hand-off notes that already exist

- **`aq handoff [--auto] [subject] [detail]`** → `_cmd_task_handoff`
  (`src/commands/surface_commands.py:329`) writes `task_context(type=handoff)` with
  `{subject, detail, session_id, auto, ts}`; prime renders the *latest* one verbatim in the
  messages section (`sections.py:~518`). "Latest" is insertion order — the comment notes
  `task_context` has no timestamp column.
- **`PreCompact` → `aq handoff --auto`** (`claude.json`). As wired, the hook passes no
  subject or detail and the CLI (`src/cli/agent_surface.py:133`) does not read the hook's
  stdin, so an auto hand-off appears to record an **empty** note, which prime renders as a
  bare `**handoff note:**` header. (Read from code, not observed live — verify.)
- **Non-auto `aq handoff`** emits `session.restart_requested`; the only other reference is
  its schema in `src/event_schemas.py:975`. **No subscriber** — the restart half of
  `aq-surface.md` §6.1 is unimplemented.
- **Provider-failover hand-off** (`src/orchestrator/provider_failover.py`,
  `inflight.build_handoff` / `handoff_comment`, `src/providers/inflight.py:356,400`):
  **daemon-written and purely factual** — session, profile, provider, model, verdict,
  branch, head, WIP commit, checkpoint status, subtask counts — stored as
  `task_metadata['provider_failover_handoff']` and rendered by prime in the task-context
  section with "check the branch tip and `git status` before redoing anything". No narrative,
  no LLM, nothing that can be wrong about what the agent *thought*. This is the strongest
  precedent in the codebase.
- **Subtasks** (`task_subtasks`) are a durable, agent-maintained progress record that
  already survives any session boundary.

### 2.4 Harness-native compaction and resume

- Claude Code: `/compact [instructions]`, auto-compaction near the context limit, `PreCompact`
  and `SessionStart(compact)` hooks (both wired). `--resume` reloads the compacted transcript
  if compaction happened before the session ended (believed; verify).
- Codex: has a compact command and `codex resume`, but our manifest keeps
  `resume.style: none` pending a mid-turn-kill test (`src/sessions/default_harnesses/codex.md`).
- Gemini: `--resume` takes an index, not a UUID → `none`; its compaction command and hook
  surface are unverified in our setup.
- Typing a slash command into a pane goes through the same nudge path as messages
  (`SessionLens.nudge`); whether our composer-clear/submit logic handles `/compact` cleanly
  on each harness is **unverified**.

### 2.5 Summarisation tools the daemon already has

- **LLM direct path** (`src/llm/`): `LLMClient.complete` with an `LLMCallSpec(
  intelligence_class="fast-low", caller="…")`; also `src/llm/cli.py`, one-shot tool-free calls
  through the logged-in agent CLIs ("creates neither an AQ task nor a session") — no API key
  needed on a subscription-only install.
- **Transcript readers** (`src/sessions/transcripts/claude.py`, `codex.py`; none for Gemini)
  normalise a session's on-disk conversation into `user|assistant|tool_use|tool_result`
  entries, so the daemon can read what the agent did.
- **Context size is observable**: the transcript watcher records per-turn
  `input/cache_read/cache_write` in `token_ledger`; the last turn's sum approximates the
  live context size at sleep time.

### 2.6 Results today

`aq test` (`src/cli/test_runner.py`) streams pytest output to the agent's tool call; the
slot report (`src/resources/slot_report.py`) records queueing events, not results. Pass-through
pytest options include `--junitxml` (`test_runner.py:208`), so a structured result file is
available if asked for. The known-failing baseline lives in a vault note
(`projects/agent-queue/notes/full-suite-baseline-<date>.md`, per `CLAUDE.md`).

## 3. Gaps

1. No budget on what a wake delivers; resume + re-prime + result is unbounded.
2. The automatic hand-off (`PreCompact`) is apparently empty, and nothing prompts a
   *structured* note at the moment an agent chooses to wait.
3. No distinction between "resume the transcript" and "start fresh from a note" as a
   deliberate choice per wake; today it falls out of harness support and key validity.
4. No result digests: a job's output reaches the agent as raw text or not at all.
5. No way to judge whether a hand-off was good enough (re-work after wake is invisible).

## 4. Implementation options

The options are not exclusive; D is orthogonal to A–C.

### Option A — Harness-native compaction before sleeping

Sketch: when a wait is registered with `dormancy=sleep`, the daemon (or the agent, as its
last act) runs the harness's compaction (`/compact <our instructions>` on Claude), letting
`PreCompact` write the note, then stops the session. On wake, `--resume` loads the compacted
transcript.

- Touches: sleep/wake's sleep path, harness manifests (a `compact_command` field),
  `SessionLens.nudge` for slash commands, the `PreCompact` hook (make it write a real note).
- Pros: the harness knows its own format; compaction quality is the vendor's problem;
  wake is a normal `--resume`.
- Cons: Claude-only today; costs a full-context turn at sleep time; opaque (we cannot see or
  test what was kept); typing into panes is fragile; still re-primes on resume.
- Size: **M** (Claude only).

### Option B — The agent writes a structured hand-off as it goes to sleep; wake fresh

Sketch: `aq wait … --note` (or a required `aq handoff` just before) captures fixed fields —
*goal, done so far, the exact next step, what I am waiting for and how to read its result,
files/branches touched, open doubts* — while the agent still has full context, in the same
turn that submits the job. The daemon appends failover-style **facts** it can verify itself
(branch, head, dirty files, WIP checkpoint, subtask states, wait id). On wake, launch a
**fresh** session: prime (with the note in section 6) + the result digest (D). No transcript.

- Touches: `_cmd_task_handoff` (structured payload, or a new `type`), prime rendering of the
  note, `BOOTSTRAP_PROMPT`/tool guidance, the wake launcher in sleep/wake, optionally a
  `task_context` timestamp column so "latest" is not insertion order.
- Pros: harness-agnostic (Codex/Gemini need it anyway since they cannot resume); cheap —
  marginal output tokens in a turn already being paid for; inspectable (`aq task show`);
  reuses the prime path and the failover precedent.
- Cons: lossy by construction; quality depends on the agent; needs enforcement (refuse
  `aq wait --sleep` without a note?); a fresh session re-reads files it already knew.
- Size: **S–M**.

### Option C — Daemon summarises the transcript via the LLM direct path

Sketch: at sleep (or at any session end without a note), the daemon reads the transcript
through `src/sessions/transcripts/`, trims to the tail and the tool calls that touched files,
and asks a `fast-*` class through `LLMClient.complete` / `src/llm/cli.py` for a note in B's
fields. Stored as a hand-off row with `author=daemon`.

- Touches: a summariser module, transcript readers, `llm:` config / CLI path, cost accounting,
  prime.
- Pros: needs no agent cooperation; also covers crashes, stall restarts and failover —
  every existing session boundary gets a note, not just planned sleeps.
- Cons: extra LLM spend on every sleep; can hallucinate or drop the one crucial detail;
  no Gemini transcript reader; transcripts may hold secrets that then go to another model
  call; needs a working LLM credential or CLI.
- Size: **M–L**.

### Option D — Result compaction (always wanted)

Sketch: the waker never delivers raw output. Each job kind has a **digester** producing a
bounded digest plus a pointer to the full artifact:

- `aq test`/pytest: counts, failing node ids, first N lines of each failure, and a diff
  against the recorded baseline ("3 failures, 2 already in the baseline, 1 new: …") —
  deterministic, from `--junitxml` or pytest's summary.
- Generic commands: exit code, last K lines, byte count, log path.
- Unstructured output (e.g. a voice transcript): optional LLM digest via the direct path,
  always with the original linked.

The wake message stays a pointer (as `_render_nudge` already does) and the digest is what
`aq wait show` / `aq message status` returns.

- Touches: [exclusive job queue](2026-09-24-exclusive-job-queue.md) /
  [managed commands](2026-09-24-managed-long-running-commands.md) result storage, the wait
  row's `result_digest`, a digester registry, `aq test` (write junit + a digest file).
- Pros: deterministic for the commonest case (tests); biggest single saving; useful even
  without sleeping (option A wakes).
- Cons: one digester per kind; a bad digest hides the failure that matters (mitigated by
  always linking the full log).
- Size: **S** for tests + generic; **M** with an LLM fallback.

## 5. Initial take (provisional)

- **D first, independent of everything else.** It pays off for every waking mode, and
  the pytest digester is deterministic.
- **B as the default agent-state hand-off**, with daemon-appended facts modelled on
  `inflight.build_handoff`. Codex and Gemini have no alternative, and it is cheap and
  visible.
- **Choose resume vs. fresh per wake, by measured context size.** If the last turn's
  `input+cache_read` in `token_ledger` is small, resume (Claude) and skip re-prime (keep only
  a small "since you slept" delta section). If it is large, go fresh from B's note. The
  threshold is an open question.
- **A only as an optimisation for Claude**, after measuring that its compaction plus resume
  beats B-fresh on re-work and tokens.
- **C deferred**, possibly as the fallback note writer for *unplanned* session ends (stall
  restart, failover), where no agent note exists. That is a separate, larger win.
- Fix the empty `PreCompact` note and give `session.restart_requested` a consumer, or
  delete it, whatever happens to the rest.

## 6. Open questions

1. **Who writes the agent-state summary: agent (B), harness (A) or daemon (C)?** Decides
   cost, which harnesses are covered, and who is accountable for a bad note.
2. **Resume or fresh on wake, and on what threshold?** Needs a measured distribution of
   context size at wait time (token_ledger) and of wait duration (cache expiry makes resume
   less of a saving after a long wait). Decides whether Option A is ever worth building.
3. **What is lost, and does it matter?** Candidates: rejected approaches ("I tried X, it
   failed because Y"), exact file:line locations, tool-output details. Should the note have
   a mandatory "don't retry" field? Decides the note schema.
4. **How do we verify a hand-off is good enough?** Options: an eval harness replaying real
   sleeps; measuring re-work after wake (files re-read, commands re-run); the woken agent
   restating the next step before acting. Decides whether we can compare A/B/C at all.
5. **Token budget for a wake.** Fixed (e.g. N tokens for prime + note + digest), per
   profile, or per intelligence class? Does prime need a hard budget with truncation order,
   rather than just `tokens_est`? Decides the renderer changes.
6. **Re-prime on resume.** Should `SessionStart(resume)` render a *delta* (new messages,
   the wait result, changed task state) instead of the full prime? Affects every resume
   today, not only sleep/wake.
7. **Enforcement.** Is a note mandatory for `aq wait --sleep` (refuse without one), or
   does the daemon fall back to facts-only / C?
8. **Digest trust boundary.** Digests of job output are untrusted content going into an
   agent's prompt (like transcripts, `src/sessions/questions.py` docstring). Do they need the
   same neutralisation as other injected text? Decides renderer escaping.
9. **Where do full results live and for how long?** Log file under the workspace (lost when
   the slot is reset), `<data_dir>`, or a DB row? Decides retention and whether a fresh
   session on a different slot can still read them.
10. **Secrets.** If C sends transcripts to a model, which redaction applies —
    `src/config_secrets.py` is config-shaped, not transcript-shaped.
11. **Does the supervisor need its own variant?** Its sessions are long-lived `named`
    sessions woken with `--resume` after `idle_timeout`; a narrative-update supervisor may
    accumulate the biggest contexts of all
    ([narrative updates](2026-09-24-supervisor-narrative-updates.md)).

## 7. Dependencies and sequencing

1. Measurement: context size at session end and wake-turn cost from `token_ledger`;
   verify the empty `PreCompact` note live.
2. D (result digests) — needs the job/result storage of the
   [exclusive job queue](2026-09-24-exclusive-job-queue.md) or
   [managed commands](2026-09-24-managed-long-running-commands.md); the pytest digester can
   land first inside `aq test` (and helps [smart test selection](2026-09-24-smart-test-selection.md)
   report against the baseline).
3. B (structured note + daemon facts) — lands with, or just before,
   [sleep/wake](2026-09-24-agent-sleep-wake.md) option A's `aq wait`.
4. Resume-vs-fresh selection — only once sleep/wake option B (real sleeping) is built.
5. A and C — optional follow-ups, justified by step 1's numbers and Q4's evaluation.

## 8. Non-goals

- Changing how harnesses compact internally.
- Memory/reflection (paused per `docs/specs/design/feature-pauses.md`); a hand-off note is
  task-scoped working state, not knowledge.
- Summarising human-facing output (digest, Discord) — see
  [narrative updates](2026-09-24-supervisor-narrative-updates.md).
- Re-architecting prime's section model; this spec only adds a budget, a delta mode and
  a better note.
