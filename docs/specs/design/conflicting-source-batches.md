# Conflicting completed sources in one frozen candidate

Task: `bright-pinnacle-83`. Extends the Git-first train, exact completion
provenance and [batch repair contract](batch-conflict-repair-starts.md).

Root source admission proves an exact retained completion, an eligible open,
same-repository, non-draft PR and the configured review authorization. The
2026-10-23 smart batch selection Phase A supersedes admission of dirty root PRs:
GitHub `CONFLICTING` / `DIRTY` observations refuse admission as `pr_conflicting`,
including when PR workflows are suppressed. Before reading GitHub, a local
`merge-tree --write-tree` probe compares the fetched target and exact source.
It uses the build's merged-tree `merge=aq-generated` attributes: generated-only
conflicts proceed to construction and regeneration; source conflicts refuse as
`pr_conflicting` without a GitHub read. Unavailable local probes remain unknown.
Conflict refusals refund the selection quantum just like red roots. For both
leaves and epics, CommandHandler first tries a mechanical merge of the fetched
default tip into the original branch, using the build's regeneration mechanism.
A clean result is a fenced bot merge and a new immutable completion; its
admission checks and approvals still examine the original source head, with no
new PR CI run required. The combined candidate always owes full batch-tip CI.
A real source conflict reopens the original task on its branch with the paths
and default tip, instructing its worker to merge, run targeted tests and close.
No repair delegate is created. Durable head/default pairs prevent a second
reopen for the same inputs; original task age is preserved. A prepared bot
merge resumes after a lost push/provenance response. Infrastructure and
regeneration failures withhold only that source without reopening an author.
The hot generated-file fix (Phase A5) must ship before or with this admission
policy. Still-computing mergeability retains its short retry. Running workflows, red required checks,
malformed or unavailable observations keep their existing admission gates.
Historical daemon-bound source CI repairs remain the sole exception for a red source:
both the source and its independently admitted repair freeze in the same batch,
source first, with all configured review authorizations intact. A stale binding,
unavailable repair, red repair or unfinished repair prerequisite keeps the pair
out of the batch. Required final candidate checks validate the combined result.
Required human approvals, including approval before trusted local execution,
remain bound to the exact authored source head. Only the command-owned bot
completion may retain that source's admission evidence across a mechanical merge;
an ordinary worker close, changed policy or unrelated head cannot borrow it.

The train retains all ordinary admission constraints: current completion and
branch identity, prerequisite closure, source ancestry, explicit holds, and
aborted or withheld inputs. Reopened or actively claimed work cannot enter the
batch. Admitted sources are frozen together in dependency order. Conflicts
between admitted members, or introduced by target movement after freezing,
remain candidate integration work. Existing explicit epic refresh controls
remain available.

Phase A6 supersedes new whole-batch repairs for ordinary root and development
builds. A conflict found after freezing ejects that member and its transitive
task and Git dependents, records their exact source/target pair, and rebuilds
the healthy remainder from the target. Repeated visits retain the pair exclusion
until either head changes and retry the original-source refresh durably. An
empty remainder withholds publication. This does not relax dirty PR admission
before freezing. Existing repair writers, epic collection/refresh and unsafe
regenerator output retain the repair contract below.

For those retained repair paths, construction merges every frozen source onto
the fetched target with its merge parents intact. Generated overlaps take the
current artifact as a temporary tree input, then one regeneration sweep rebuilds
the derived artifacts after all sources have merged. A source conflict stops
automatic construction and retains
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

This contract also applies to completed children collected onto their epic
branch while other siblings are unfinished. A child/epic content conflict
freezes the exact child source and files one ordinary batch repair; repeated
visits and daemon restarts reuse its allocation. Target status names the merge
conflict and repair. If source identity, stack containment or an undelivered
prerequisite prevents admission, selection reports that blocker instead of
silently presenting the target as idle. Such refusals never authorize a repair
or bypass the admission guard.

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
union; dirty root refusal with suppressed PR CI; genuine source red and unavailable CI; explicit review
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

Ownership and storage-sink inventory drift has no dedicated CI guard (Jack
2026-10-19 decision). The source inventory still discovers and charges all owned
code, including relocated code and unreviewed sinks. Fixed size/table ceilings and the prohibition on new
integration storage categories remain enforced. Factory-policy page length is
advisory; its normative content and role references remain required.
