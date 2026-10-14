# Manual integration and legacy obligation retirement

Authority: approved `rev-fair-lantern`, revision 2, §11.2 (canonical SHA256
`4518d9296649ddc2f0192ff73dd682864fb938e3d9687069282b4f6b0a86453f`), and the
supervisor's bounded implementation direction on `vivid-lantern-50`.

`aq integration off PROJECT` previews an operator transition to the existing
`disabled` effective/desired modes. `--apply --expected-plan-digest DIGEST`
applies only that exact preview. Manual/off is a surface name, not a new
persisted mode. The command is local-operator-only, increments the policy
generation, disables automatic scheduling and records its plan and principal.
It preserves tasks, claims, human holds, frozen batches, candidates, operations,
sources and attempts. It does not cancel running CI or writers. Publication
already inside its bounded critical section finishes before off commits;
subsequent train publication and admission are refused. Ordinary worker source
publication and task execution remain available. Existing generation-fenced
project policy configuration owns explicit re-enabling.

`aq integration retire-legacy PROJECT --input FILE` previews a single cutover
manifest. Input contains `entries`, each naming `task_id`, `completion_id`,
`source_sha`, and `content_sha` (the default-branch commit carrying equivalent
content), plus a nonempty `reason`. Apply requires the exact preview digest.
There is one immutable manifest per project/repository/default-target binding;
all selected generations are committed together. The identical apply is
idempotent; another manifest or later generation cannot extend that cutover.

The project must be disabled, with no unresolved integration publication,
live integration writer, held selected task, human hold or active/frozen
integration work. Paused/frozen work is preserved, and its presence blocks retirement
rather than being silently aborted. Selected tasks must already be completed
passing generations, scoped to the designated repository. Full source objects
must be retained by immutable remote completion provenance. The source must
not be an ancestor of the observed default target. The content commit must be
an ancestor of that target, and the old evaluator must accept a named
equivalence proof on that content commit. Unknown Git, missing originals,
changed task/completion/origin, policy generation, binding or remote head refuse
apply. Preview writes neither database state nor remote refs.

The manifest records the original source/provenance ref, content commit,
accepted equivalence reason, observed target, generation, operator and digest.
Its disposition is `delivered_by_other_means`: lifecycle retirement of a
delivery obligation, never a passing close or Git containment proof. Delivery
evidence for that exact default-target generation remains pending/unknown;
admission and completed-undelivered diagnostics explicitly exclude the retired
obligation. Other targets and later completion generations do not inherit it.
Source-retention refs remain protected. Metrics consumers can identify this
exclusion from the request's disposition without counting it as delivered.
After that binding's cutover, its default-target requests and frozen-input
proofs use strict ancestry. Unselected and later equivalent-only generations
remain pending work; they do not inherit a retirement. Other projects and
targets retain their current evaluator until their own explicit cutover.

This additive feature does not enable the full fast-flow protocol, weaken CI
or review gates, migrate live databases or change GitHub rulesets.
