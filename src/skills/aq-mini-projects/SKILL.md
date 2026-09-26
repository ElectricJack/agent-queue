---
name: aq-mini-projects
description: Facilitate an explicitly requested, bounded ideation session in one aq task (quick mode), turning a brief or brain dump into a critiqued roadmap submitted for human plan review. Use for requested mini-project ideation; durable multi-task or cross-provider sessions are outside this skill.
allowed-tools:
  - Bash
---

# Mini-projects: quick ideation

Run one proposal round, one critique and one synthesis in the held task.
The facilitator alone writes the final artifact. This is an opt-in workflow;
do not start it during ordinary coding or turn its roadmap into implementation
tasks. It requires no external brainstorming skill.

## Establish the run

Run `aq prime` and read `aq task show <task-id>`. In a pool session,
`.aq/claim.json` proves the task and claim epoch you hold. Keep the ordinary
claim, heartbeat, cancellation and completion protocol throughout ideation.

Record the topic, scope/non-goals, constraints, decision rubric, source
references and limits before role work starts. Default to a maximum **90 minutes
for the whole task**, at most **three subordinate agents**, and **one revision**
of the proposal in synthesis. A shorter task deadline wins. Record the start
time and deadline; include all role work and submission within that budget.

Tell the requester the execution arrangement and expected cost up front:
one ordinary isolated worktree slot remains held, including while waiting;
serial passes share a context and model family. Sub-agents add model work and
may share family/context; quick mode provides no independent cross-provider
review. Record known model attribution, or label it unknown. Give an estimate
or budget if one is available; otherwise state that monetary cost is unknown.
Do not create thinker profiles, acquire extra workspaces or share a canonical
mutable checkout among writers. For code grounding, record the checkout HEAD
and cite files/symbols from that revision; roles only read that snapshot. If
the checkout moves, stop rather than mix revisions.

## Freeze the input

Read the task description once, or the requested brief file once. Preserve
the exact UTF-8 bytes, **at most 32 KiB (32,768 bytes)**; reject invalid UTF-8
or oversized input before role work. Do not silently truncate or normalize
newlines. If required fields are missing, clarify or explicitly record agreed
assumptions before freezing. The frozen brief must include project/topic,
scope/non-goals, constraints, decision rubric and source references.

Compute SHA-256 over those bytes. Append a snapshot envelope to the held
task's **task context** with `aq task set <task-id> --note <envelope>`:
`schema_version: 1`, task id, claim epoch (if present), run/start/deadline,
project/topic, source reference, repo revision if relevant, byte count,
`input_sha256`, and the full frozen brief. Use argument arrays or proper shell
quoting for arbitrary brief text; never interpolate it as executable shell.
`--note` writes task context; a progress comment alone is not the snapshot.

For a captured brief file, this Python fragment validates and fingerprints
the bytes without normalizing them (replace the path with the captured file):

```python
from hashlib import sha256
from pathlib import Path

brief_bytes = Path("brief.txt").read_bytes()
if len(brief_bytes) > 32 * 1024:
    raise ValueError("brief exceeds 32 KiB")
brief_text = brief_bytes.decode("utf-8", errors="strict")
input_sha256 = sha256(brief_bytes).hexdigest()
```

Confirm the context write succeeded before proceeding. Give every role the
same snapshot **content and hash explicitly**, plus the deadline, rubric and
repo revision. Do not send a mutable vault path or rely on dependency summaries
appearing in prime. Later source edits do not change this run: finish against
the frozen bytes, or stop it and request a new run with a new input hash. Never
replace the recorded snapshot mid-run.

## Proposal, critique, synthesis

Use serial role passes by default. Use harness sub-agents only when the
requester **explicitly requests them** and the harness supports them, at most
three total; they are subordinate to this task, not new AQ tasks. No recursive
delegation. An unavailable harness uses serial passes with that limitation
recorded. A failed or cancelled role is an incomplete run, not a silent serial
replacement. Sub-agents return text to the facilitator; they do not write the
final artifact, mutate the repo or call review commands.

1. **Proposer:** explore viable alternatives against the frozen rubric,
   including keeping the current behavior. Cover code evidence (path/symbol
   and revision where relevant), constraints, failure modes, measurable
   acceptance and costs/tradeoffs. Separate verified facts, assumptions and
   open questions. Return a proposal bound to the input hash.
2. **Critic:** read the actual proposal and frozen brief, verifying their
   hashes first. Challenge unsupported claims, missing invariants, failure
   paths and untestable acceptance. Use the REVIEW vocabulary below. A
   no-material-defects result names the evidence and criteria checked; do not
   manufacture findings. Return a critique referencing the proposal hash.
3. **Synthesizer (facilitator):** read the verified proposal and critique;
   resolve conflicts with reasons and make at most one proposal revision.
   Record every finding's disposition. Retain material dissent and uncertainty.
   Produce a prioritized roadmap with explicit dependencies, non-goals,
   measurable acceptance and cost/footprint. Do not start another critique
   round or treat agreement as human approval.

The self-contained **REVIEW rubric**:

- `[blocking]`: contradiction, missing invariant, unsafe failure path,
  unsupported claim or acceptance that cannot be tested.
- `[nit]`: wording/readability.
- Every finding includes an id, the claim, evidence (path/symbol and revision
  when code-grounded), a concrete failure scenario and a correction or viable
  alternative. These are ordinary ideation artifacts, **not** `review_comment`
  calls: no dispatched review task exists here.
- Use a disposition table: `finding_id | fixed/rebutted/deferred | rationale |
  evidence`. `fixed` identifies the changed text; `rebutted` supplies evidence
  and remains visible for the decider to judge. A deferred blocker requires
  an explicit exclusion and updated acceptance in the plan itself; otherwise
  it remains unresolved. List any unresolved blockers prominently.

## Artifact contract and incomplete runs

Each role's markdown is UTF-8, at most 32 KiB. Hash its exact bytes and record
schema version 1, role, task/claim, input hash, referenced artifact hashes,
content SHA-256 and creation time. Publish completed role bodies and their
envelopes in the held task's context via `--note` before passing them on; this
preserves partial work after the worktree is released. The final plan can
include the role text or refer to these explicit task-context hashes. Do not
overwrite published bodies; label the one permitted revision and its new hash.
This is a quick-mode convention, not a database immutability guarantee.

Verify both the input hash and referenced content hashes before each dependent
pass and before submission. Missing, failed or mismatched artifacts stop the
run: never invent an absent proposal or interpret silence as critique success.
At cancellation, terminal role failure, budget/deadline exhaustion or an
unresolved blocker within the agreed scope, stop synthesis/submission, cancel
outstanding subordinate work and record **incomplete**, the reason, retained
artifact hashes and unresolved items. Persist available partial artifacts and
close `fail` under the task protocol. No automatic whole-run retry. An
authorized retry uses a new task/claim identity and explicit artifact revision.

## Submit the single final plan

The final markdown (also at most 32 KiB) includes provenance and execution
limitations, alternatives/evidence, critique/dispositions, the prioritized
roadmap, dependencies, non-goals, acceptance, costs and material dissent.
Keep it as a local draft **without YAML frontmatter** (put provenance in the
body), so the review body hash equals the file's SHA-256. Submit it through
the existing review surface:

```bash
aq review submit --task-id <task-id> --file <plan.md> --kind plan --title "<topic> roadmap"
aq review show --review-id <returned-id> --json
```

Record the review id, revision, vault path, `revision.content_sha256`, input
hash and referenced role hashes in task context and the close summary. Verify
the review's content hash against the submitted bytes. On an ambiguous submit,
use `aq review list --task-id <task-id> --kind plan --json`, then show any
matching review and compare its body hash before considering a retry. Do not
create a duplicate review; if the result remains uncertain, report incomplete.
A refusal or unavailable submission is incomplete, not a
successful run; preserve the draft body in task context and report the error.

Close according to prime with status **plan submitted; awaiting human
decision**, using the pool `--claim-next --wait 60` protocol when applicable.
Waiting for the human plan decision is the workflow's end state; the worker
does not hold its slot waiting for that decision. Only the human or an
explicitly delegated decider decides the review. Closing the facilitator
approves nothing, and even approval does not authorize automatic task creation
or execution by this skill.

For a code-grounded serial example and failure read-throughs, see
`docs/guides/mini-projects.md` in the agent-queue repository. This skill contains
all essential instructions because harness installation copies only SKILL.md.
