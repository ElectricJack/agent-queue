# Conflicting completed sources in one frozen candidate

Task: `bright-pinnacle-83`. Extends the Git-first train, exact completion
provenance and [batch repair contract](batch-conflict-repair-starts.md).

Source admission proves an exact retained completion, an eligible open,
same-repository, non-draft PR and the configured review authorization. A base
conflict is candidate integration work. When authenticated observations prove
that no pull-request workflow exists on the exact source head and GitHub reports
the exact PR dirty, missing PR CI does not prevent freezing that source alongside
other compatible completions targeting the same delivery branch. It is never a
passing check. A running workflow, red required checks, malformed or unavailable
observations and unknown mergeability keep their existing admission gates.
Historical daemon-bound source CI repairs remain the sole exception for a red source:
both the source and its independently admitted repair freeze in the same batch,
source first, with all configured review authorizations intact. A stale binding,
unavailable repair, red repair or unfinished repair prerequisite keeps the pair
out of the batch. Required final candidate checks validate the combined result.
Required human approvals, including approval before trusted local execution,
remain bound to the exact source head.

The train retains all ordinary admission constraints: current completion and
branch identity, prerequisite closure, source ancestry, explicit holds, and
aborted or withheld inputs. Reopened or actively claimed work cannot enter the
batch. Sources are frozen together in dependency order; neither leaf nor epic
authors must merge the default branch or repeat tests merely to clear a base
conflict. Existing explicit epic refresh controls remain available.

Construction merges every frozen source onto the fetched target with its merge
parents intact. Generated overlaps take the current artifact as a temporary
tree input, then one regeneration sweep rebuilds the derived artifacts after all
sources have merged. A source conflict stops automatic construction and retains
the exact partial head. One bounded ordinary batch repair owns the conflicting
member and every remaining frozen member. It resolves all source conflicts,
retains each exact source as an ancestor and runs regeneration once after the
combined merge. It cannot finish after only the first conflict. Repeated visits
and restarts reuse that repair and its existing attempt budget.

The publishing identity policy treats the exact frozen source heads as inherited
history, including members absent from the repair's starting head. New repair
commits still require the worker's allowed identity. A passing ordinary repair
close verifies every frozen source's ancestry before recording completion
provenance; an incomplete close retains the same claim and workspace to finish
the remaining merges. A failing close retains the normal bounded retry policy.

Candidate reuse requires the fetched target and **every frozen source** to be
ancestors of the candidate. Source trailers alone cannot make a partial repair
complete. A complete repaired candidate owes the unchanged trusted required
checks on its exact SHA, required tree verdicts, attestation and fenced
expected-old publication. Green source checks, a passing worker close, or checks
on a partial candidate cannot advance the target. Historical delivery proofs
remain readable and do not authorize a new publication.

A passing ordinary repair close reseals its still-open batch and wakes the
batch's delivery target, using the immutable repair input and current allocation
number. It does not replace frozen members, spend another attempt, release a
pause or abort, or declare the candidate green. The next target visit repeats
this idempotent handoff if close was interrupted after task completion; archived
completed repairs remain recoverable. Fresh Git and exact checks still decide
whether the repaired candidate can publish.

Doctor and the stall sweep flag an open sealed batch that has made no progress
past its train settling cap (30 minutes by default). The deadline starts at the
latest batch change, completed repair, or observed candidate/target/check-state
change. Live bounded visits and announced retry deadlines receive their bounded
grace; unfinished repair tasks retain their existing capacity diagnostics.
Paused, aborted and terminal batches are excluded. Observations never advance
delivery or authorize an automatic abort.

Acceptance uses real Git and disposable PostgreSQL: multiple sources conflicting
with main and one another; generated-only overlaps rebuilt once from the full
union; suppressed PR CI; genuine source red and unavailable CI; explicit review
gates; aborted inputs and prerequisites; an incomplete repair with source
trailers; one repair across repeat visits; and publication only after final
candidate checks on the complete ancestry.

## Bounded source CI recovery

New source-CI failures reopen the original completed leaf in place, retaining its
branch, existing PR and explicit route; derived routes are recomputed normally.
The command revalidates exact source identity,
policy generation, authorization, delivery and stopped ownership before the
transaction that records feedback and makes the task READY. One automatic repair
is allowed per original task, across source heads and checkpoint generations.
A further genuine failure, or red CI on a historical repair delegate, sends one
durable supervisor notification and creates no new task or PR. Existing delegates
keep their delivery, retirement and writer protections; no successor is filed.
Containers retain their completed state and notify the supervisor once to
coordinate repair of the original branch with its existing owner.
The new `source-ci-reopen` vault template seeds on existing installs without
overwriting customized or historical `source-ci-repair` delegate instructions.
The supervisor closes obsolete historical repair PRs after their source lands.
Cancellation-only observations keep their bounded infrastructure rerun policy.

Ownership and storage-sink inventory drift is advisory in ordinary CI. The
source inventory still discovers and charges all owned code, including relocated
code and unreviewed sinks. Fixed size/table ceilings and the prohibition on new
integration storage categories remain enforced. Factory-policy page length is
advisory; its normative content and role references remain required.
