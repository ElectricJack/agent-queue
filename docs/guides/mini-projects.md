# Mini-projects: quick ideation

Request the `aq-mini-projects` skill to turn a brain dump into a critiqued
roadmap in one held task. It runs one proposal, one critique and one synthesis,
then submits one plan for a human decision. The default whole-task budget is
90 minutes, with at most one proposal revision. Serial role passes work on
every harness. Up to three harness sub-agents may be used only when explicitly
requested and supported; they share this task, and quick mode does not establish
independent cross-provider review.

The [facilitator skill](../../src/skills/aq-mini-projects/SKILL.md) contains the
full input, artifact and failure contract plus the self-contained REVIEW
rubric. It is discovered by the existing write-if-absent skill installer; an
operator's installed copy survives upgrades. No new profile or grant is needed.
The durable five-node formula and artifact API are outside this quick-mode
delivery. [Document reviews](reviews.md) explains the human decision surface.

## A code-grounded worked example

This is a read-through, not evidence that a live review was submitted. Run it
only inside an authorized facilitator task; `TASK` below means the task id
confirmed by prime (and `.aq/claim.json` in a pool session).

The request: “Use quick ideation to improve discoverability of document review
commands. Use serial passes and read the repo; produce a roadmap, no code.”

1. Record one held worktree slot, shared context/family, known model or unknown
   attribution, no independent cross-provider coverage and monetary cost unknown
   unless an estimate is available. Set a deadline 90 minutes from task start,
   shortened if the task budget requires it. Record `git rev-parse HEAD` and
   use that revision for all code citations; stop if HEAD moves.
2. Capture this UTF-8 brief once as `brief.txt` in the held worktree:

   ```text
   Project/topic: agent-queue / document-review discoverability
   Scope: compare CLI help, guide links and skill routing.
   Non-goals: API/schema changes, review automation, implementation tasks.
   Constraints: existing review authority; one quick task; serial passes.
   Decision rubric: verifiable claims, user effort, maintenance, measurable acceptance.
   Source refs: src/cli/reviews.py; src/tools/definitions.py; docs/guides/reviews.md;
   src/skills/aq-cli/SKILL.md; src/commands/review_commands.py.
   ```

   Validate and hash the captured bytes with the skill's Python fragment. Keep
   the resulting `brief_text` and `input_sha256`; add start/deadline, task/claim
   and repo revision to the envelope. Append the full snapshot to task context
   before the proposal. For arbitrary text, pass the envelope as an argument
   rather than constructing a shell command:

   ```python
   import json
   import subprocess

   # envelope contains the validated frozen brief and the run metadata above.
   subprocess.run(
       ["aq", "task", "set", task_id, "--note", json.dumps(envelope, ensure_ascii=False)],
       check=True,
   )
   ```

   Check the command's success response. The source can subsequently change;
   the role inputs remain the captured brief and its verified hash. A task
   description source works the same way: read it once through `aq task show
   --json`, then encode that captured description as UTF-8.
3. The proposer compares current behavior, better guide links and skill routing.
   It checks the actual CLI and handler definitions at the recorded revision.
   Example evidence: `src/commands/review_commands.py:_cmd_review_dispatch`
   restricts dispatch to an operator or elevated session. Each alternative
   includes constraints, failure modes, acceptance and maintenance cost. Publish
   the proposal body, input hash and content hash to this task's context.
4. The critic reads those exact bytes. Suppose the proposal suggested worker
   dispatch: `[blocking] f-1` cites the handler restriction, explains that the
   command would be refused, and asks to restrict that instruction to the
   authorized operator/supervisor. `[nit] f-2` asks for a clearer guide label.
   Publish the critique with the same input hash and the proposal's content
   hash; do not call `aq review comment`.
5. The facilitator makes its one revision and records dispositions:

   | Finding | Disposition | Rationale / evidence |
   |---|---|---|
   | f-1 | fixed | Roadmap preserves dispatch authority; cited `_cmd_review_dispatch` at the recorded HEAD. |
   | f-2 | fixed | Suggested guide label names document review. |

   The roadmap prioritizes a link/routing improvement first, with acceptance
   that examples match CLI help and the handler. A later operator-oriented
   dispatch explanation depends on that shared vocabulary. It lists the API,
   schema and automatic dispatch as non-goals, states maintenance cost and
   retains any preference for leaving the current help unchanged as dissent.
   Verify hashes and limits for every role and the final plan. Unresolved
   in-scope blockers would instead end this run as incomplete.
6. Save the one final plan as `plan.md` without YAML frontmatter (provenance
   goes in the body), so the review body hash matches the file hash, and submit:

   ```bash
   aq review submit --task-id TASK --file plan.md --kind plan --title "Document-review discoverability roadmap"
   aq review show --review-id RETURNED_REVIEW_ID --json
   ```

   Verify `revision.content_sha256` against the plan bytes. Record that hash,
   review id/revision/vault path, the frozen input hash and role references in
   task context. Close the task following prime, reporting “plan submitted;
   awaiting human decision.” Do not create implementation tasks. The review
   stays open for the human or explicitly delegated decider; task close does
   not approve it.

## Failure read-throughs

| Situation | Expected behavior |
|---|---|
| Invalid UTF-8 or brief over 32,768 bytes | Reject before role work; request a corrected brief, never truncate. |
| Original file edited after freezing | Continue with the frozen bytes, or explicitly stop for a new run/hash. |
| Sub-agents requested but unsupported | Record serial fallback and shared-context/family limitations. |
| Proposal missing, role failed/cancelled, or content/input hash mismatches | Stop; retain available bodies/hashes in task context and report incomplete. Do not invent proposals or substitute a successful critique. |
| Deadline/cancellation during critique | Stop subordinate work; preserve partial evidence; no plan submission or whole-run retry. |
| Deferred blocking finding | Plan states the exclusion and changes acceptance; otherwise it stays unresolved and the run is incomplete. |
| Submission refused or ambiguous | Preserve the final body in task context; `aq review list --task-id TASK --kind plan --json` and inspect matching review hashes before any retry. A refusal or uncertain result is incomplete, not a submitted plan. |
| Human has not decided the submitted plan | Close the facilitator truthfully; leave the review gate open. Waiting holds no claimed facilitator task. |

Local scratch files are not durable after a task releases its worktree. The
task-context bodies preserve partial quick-mode artifacts; the submitted
review stores the final plan. These conventions do not add an immutable
artifact database or promise cross-task reads.
