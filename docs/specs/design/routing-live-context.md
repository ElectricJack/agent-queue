# Live context for delegation

`task_route_plan` extends the mandatory router's existing `Snapshot` with
`live_context` and `live_summary`. These describe compatible worker profiles
and providers without changing policy, provider choice, fleet limits, gates,
or claim admission. The classifier remains task-only; provider preference belongs
to the reviewed policy and deterministic placement.

## Routine hosted preference

A lane harness selector is an exact harness id or a prefix with a single
trailing `*`. Any other glob character makes the policy invalid, so routing
stops with `routing.invalid_policy` instead of admitting a harness a malformed
exclusion meant to remove. A design lane's `prefer` names exact harnesses. A
narrow lane excludes every matching harness from general candidates, using the
same matcher as lane admission. The route reason and
`aq doctor --check routing.unmatched_selectors` name any selector in the active
policy that matches no installed harness. That is a report, not a refusal:
OpenCode is installed by the operator, so the shipped lanes can match nothing.

The shipped `narrow-hosted` lane selects `opencode-zen*`, including new preview
variants. Integration and development repair origins keep `narrow: false` even
when the task kind is `bugfix` or classification reports narrow, test-verified
work; neither local OpenCode nor a Zen variant is eligible. Changing this policy
requires activating the rebuilt reviewed routing artifact.

Ordinary train repairs retain `created_by_kind: system` for their ordinary
claim and managed publication lifecycle. Routing resolves their origin to
`integration_repair` from the immutable `Repair input`, verified against the
ordinary filing identity. This projection applies to existing filings as well
as new ones, without reclassifying them as legacy operation delegates. A
generic system filing with no ordinary repair input keeps its original origin.
Source-CI repairs are filed with `created_by_kind: source_ci_repair` and route
as `integration_repair` too; a repair filed before that origin existed is
recognised by its source-CI record. Tasks filed with
`created_by_kind: integration_writer`, including verifier tasks of kind `test`,
also use the integration repair origin and stay off OpenCode lanes. Replay
reads the routed origin from the route's rule.

HTTP command callers cannot supply underscore-prefixed private arguments,
including `_created_by_kind` and `_created_by_id`; elevation does not change
this boundary. Internal services retain those arguments when they file
directly through `CommandHandler`.

`aq task route-override` changes the route only; it never changes task status.
It accepts a task no worker holds and no session is starting, running or
draining, including a READY task just stopped; stop intent alone does not prove
the old worker is gone. To run stopped work on the new route, restart a BLOCKED
task through `task_recover`, or resume a PAUSED
one with `resume_task`; their recovery holds, retry budget and preserved-work
handoff all apply.

An optional `prefer_harnesses` list on a kind (overridable by origin) favors a
compatible hosted harness after eligible local/design lane preferences. The
shipped implementation, fix, refactor, test, docs, chore and sync rules prefer
Codex to conserve Claude capacity. Class fit, exclusions, workspace requirements,
explicit provider intent, reserved cells and benchmark arms are applied first.
Research and design retain their own rules. Provider load never raises the class.

Preference requires an available provider, usage at or below that provider's
existing soft limit (or explicitly unknown), spare profile slots including
routed backlog, and positive effective headroom when observed. It compares no
percentages between providers or unequal quota windows. Fresh quota windows
reduce each provider's own score above its soft limit; stale/reset windows remain
labelled evidence only. If no preferred hosted candidate qualifies, select a
compatible alternative with free observed capacity by the existing pressure
score. With no free candidate, preserve queued routing by pressure. Headroom is
an observation used to rank preference, never an admission reservation.

Apply recomputes this selection under the existing route lock. Persisted reasons
and decision evidence name selected class/profile/provider, load and headroom,
availability, quota freshness/age and snapshot age, including why a preferred
candidate was bypassed. Caller-supplied context cannot supply these facts.

Historical replay must use exported route evidence, report missing observations
and distribution changes, and distinguish a counterfactual from measured quota
savings. Ship source, immutable artifact and manifest together. Import/diff and
activate the reviewed hash through the playbook commands after replay; retain
the project's binding or explicitly review a project override. Never edit an
installed artifact, pin callers, reroute held work or change fleet limits.

The versioned context includes collection start/end and duration, provenance,
provider state/reason code/update age, and each observed quota window's scope,
percentage, observation/confirmation timestamps, reset and freshness. Missing
quota is unknown; stale or reset windows are retained as labelled observations
but do not become fresh scoring inputs. Weekly and five-hour windows remain
separate. Collector provenance is restricted to known probe/transcript labels.

Each compatible profile includes its enabled flag, class list, configured
effective limit, fleet busy/idle/starting/draining/unresponsive counts, project
supply, routed backlog, local idle claim capacity and launch/effective headroom.
Starting sessions and pending launches consume capacity. Preparing claims are
busy. Task workers' agent reservations also consume capacity before their
durable sessions exist, and are counted once when a session appears.
Draining, disabled and unresponsive workers are not idle claim capacity.
Pool idle capacity is local to the project because tokens/workspaces are scoped.

Launch headroom is bounded by the profile limit minus all live supply, the
remaining project worker cap, the global pool cap, acquirable workspace capacity
(including lazy worktree slots), and launch quarantine. Pending launches without
acquired workspaces conservatively reduce workspace observations. Existing local
idle workers can still claim when new launches have no workspace/cap headroom.
Effective headroom subtracts routed backlog from idle plus launch capacity.
Shared project, workspace and fleet headroom must never be summed across
profiles. These are observational upper bounds, not reservations or a promise
of immediate admission; identity, delivery and human gates remain authoritative.
Zero headroom permits routing queued work and does not create another scheduler.

The active-work summary contains task-kind counts only. Output is capped at 64
profiles, 16 providers, eight quota windows per provider, 32 classes per profile,
and the finite task-kind vocabulary; omission is explicit. The readable summary
is capped at 2400 characters. No account labels, task descriptions, transcripts,
session credentials, workspace paths or captured launch errors are included.
Quarantine reports `launch_backoff` and expiry, rather than captured stderr.

`task_route_apply` refreshes eligibility and load inside the existing fleet
advisory-lock transaction, checks provider/harness/lifecycle/class compatibility,
and reselects on current routed backlog. It stores the bounded apply observation
in the route record. Context supplied by a caller is never trusted for eligibility
or admission. Concurrent decisions observe previously committed routes; claim
and guarded route writes continue to protect held tasks.

Verification lives in `tests/test_routing_router.py` and
`tests/test_routing_planner.py`: supply states, stale/missing quota, provider
degradation/disablement, caps/workspaces/quarantine, pending launch accounting,
bounded/private output and concurrent apply freshness.


## OpenCode family admission and private arguments

Every OpenCode CLI family member is excluded from the general worker pool.
Family recognition uses the installed harness provider and command as well as
its id, so a custom id or an active narrow selector naming only `opencode-zen`
cannot admit another family member to general repair, verifier or writer work.
Explicit narrow lane admission and its task classification requirements remain
policy controlled. `routing.unmatched_selectors` also warns about installed
OpenCode family members that no active narrow lane selector covers, using the
project harness scope and the active artifact's literal policy.

MCP and typed HTTP routes remove every caller argument beginning with `_`
before dispatch. Typed routes then attach their server-derived authenticated
scope. Internal origin and routing constraints remain available to trusted
in-process command callers only.
