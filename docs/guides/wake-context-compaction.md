# Handoff notes and wake context

`aq handoff` accepts legacy positional subject/detail and a structured version 1
note. The daemon stores agent assertions separately from its observed facts.

```bash
aq handoff --auto --goal 'Finish renderer change' \
  --completed 'Added timestamp ordering' \
  --next-step 'Run the focused renderer tests' \
  --waiting-for 'Test slot' --file src/prime/sections.py \
  --decision 'Keep full prime on resume' \
  --do-not-repeat 'Capture a new full-suite baseline' \
  --uncertainty 'Confirm quoting of unusual filenames' \
  --idempotency-key checkpoint-1
```

All agent text together is limited to **8 KiB UTF-8**, and each repeatable list
allows at most 20 entries. Oversized input is rejected without storing a row.
`--schema-version 1` is optional. The retry key is optional and scoped to task
and claim epoch: concurrent retries reuse one row. An empty automatic hook is
a no-op, preserving the useful note; it is not a model-generated summary.

`--auto` stores a note only. A non-auto call records a restart request; there
is currently no subscriber that performs that restart. Replaying a keyed call
does not emit another restart request. An empty non-auto call may record a
labelled facts-only recovery checkpoint. It cannot replace a meaningful agent
note in prime.

The daemon observes current task, claim, session, branch, HEAD, work directory,
subtask counts, and dirty paths. It includes at most 20 dirty paths, each with a
256-byte display limit, plus the total observed count. Failed Git reads are
unknown rather than a claim that the checkout is clean. Job/wait identity lists
are empty in this independent slice; they require the future authoritative
JOB/WAIT stores. Note paths are assertions and are never used to read files.

## Prime budget

Full prime continues on resume and compaction. Its additional wake material in
the Messages section is bounded by **16 KiB**:

| Material | UTF-8 budget |
|---|---:|
| Latest meaningful handoff projection | 8 KiB |
| Result summary projections, reserved for JOB/OUTPUT | 6 KiB |
| Current daemon facts and changed-state pointers | 2 KiB |

This release renders the handoff and current-fact portions. Stored result
summaries, result pointers in wake delivery, three-summary paging, and baseline
annotations on stored job results belong to JOB/OUTPUT integration. No log
excerpt is inlined here. Ordinary inbox messages retain their existing delivery
behavior and are not classified as job wake results.

The renderer prioritizes next step and uncertainties over older completed-work
prose. Escaping can expand a valid 8 KiB note; oversized display blocks retain a
labelled excerpt and a pointer to `aq task show <task-id>` for the full context
row. Legacy oversized subject/detail notes are likewise bounded at display.
Role, authorization rules, current task ownership, and close protocol are outside
this optional budget and remain intact. A different claim/session/checkout or
HEAD labels handoff files stale and displays newly observed daemon facts.

Notes are quoted historical data with row/time provenance. Display strips
control sequences and escapes Markdown/HTML; this presentation does not turn
agent text into trusted instructions. No linked URL is fetched or pasted command
executed. Context archival does not remove already loaded harness conversation.
`tokens_est` remains characters divided by four, explicitly labelled an estimate.

## Recorded baseline helper

`src/resources/test_baseline.py` captures the latest valid local project note
named `full-suite-baseline-YYYY-MM-DD.md` at the caller's job-start instant. It
reads the existing recorded-note format (full source main SHA and a Known
failures section of exact backtick-quoted node ids), without parsing pytest
output or writing a baseline. It accepts canonical failure ids from the future
managed-command parser. Parameter values, including spaces, remain part of
identity.

A snapshot records the path, SHA-256 content hash, capture time, source commit,
and failure ids. A later edit of the note cannot change that snapshot. A newer
invalid note cannot displace the latest valid recorded evidence. A missing or
unparseable baseline makes comparison unavailable. A source commit different
from the caller's comparison commit, or an unknown comparison commit, is
`stale`; no arbitrary age threshold is imposed. `matched` membership never
changes an exit code or makes failed validation pass. Incomplete parser results
are labelled incomplete. A failure absent from the baseline is not proof that
this change caused it.

## Upgrade and verification

Revision `compact_handoff_v1` adds server timestamps, ordering index, and
per-claim retry uniqueness to `task_context`. Existing server `ts` values are
backfilled; rows with no provable timestamp sort at zero, with row id resolving
ties. Apply through the operator/daemon upgrade path, never from a worker slot.
Downgrade preserves note content; old and version 1 notes remain readable by the
new renderer. `task_handoff` keeps its existing agent grant in both worker
profiles and gains a typed command contract; no new grant is needed.

Tests cover empty hooks, concurrent retries, Unicode and display expansion,
equal-time ordering, historical notes, changed checkout/claim, immutable baseline
provenance, and migration upgrade/replay/downgrade. No token savings or native
harness compaction performance is claimed. Before enabling a future automatic
resume/compaction policy, perform a real-harness exercise on each enabled harness:
write a useful note, invoke its supported resume/compact path, verify the next
action and retained constraints/rejected approaches, and record input tokens and
redundant commands. That empirical exercise is separate from this deterministic
renderer release.
