# Verified wait task status classification

Implements section 6 of the approved event-driven hibernation implementation spec
(vault `projects/agent-queue/specs/2026-10-13-event-driven-agent-hibernation-and-verified-waits-implementa.md`).
The three suspended states are open work. WAITING retains a session and its claim;
HIBERNATED belongs to a continuation; RESUME_PENDING is awaiting admission and
acknowledgment. None is READY or claimable. A RESUME_PENDING launch in `starting`
reserves capacity, but a stale running session does not count as live.

The shared `SUSPENDED_TASK_STATUSES` and `OPEN_TASK_STATUSES` define the vocabulary.
Existing terminal-complement readers already include suspended work. A list that
controls routing, removal, repair or restart permission is an admission filter,
even if the approved baseline calls its module an open-work view: adding suspended
work would transfer ownership without cancelling its continuation.

| Location / list | Classification | Result |
|---|---|---|
| `claim_queries.claim_frontier_predicates`, targeted selection | Frontier | READY only; all three excluded |
| `hierarchy_queries.STALE_CONTAINER_STATUSES`, settlement/verification predicates | Frontier | Existing BLOCKED/PAUSED recovery and IN_PROGRESS verification remain unchanged; suspended containers cannot enter the frontier |
| `routing_queries.ROUTABLE_STATUSES`, `OVERRIDABLE_STATUSES` | Frontier | Queued states only; all three excluded |
| `task_reroute_queries` allowed reroute, queued-only and bulk predicates | Frontier | Suspended continuation ownership is excluded from ordinary reroute |
| `routing_commands` routable/override predicates | Frontier | Excludes all three |
| `integration.delegate_release` obsolete delegate eligibility | Frontier | Existing delegate proof is retained; suspended is not an obsolete-delegate authorization |
| Integration lifecycle/removal guard predicates | Frontier | Existing branch/delivery guards retained; no new removal authorization |
| `integration.repair` editable, resettable and delegate recovery lists | Frontier | Existing repair-specific statuses retained; no speculative READY recovery of suspended work |
| `blocked_state` promotion/recovery clauses | Frontier | Only existing DEFINED/READY/BLOCKED projection rules; suspended tasks remain outside claim promotion |
| `task_queries` completion filters, open children/counts, project move blockers, finished task predicates | Open work | Terminal complements already include all three; no added terminal values |
| `task_queries._PROJECTION_NEUTRAL_STATUSES` | Frontier | READY and IN_PROGRESS remain the explicit neutral projection optimization |
| `task_queries._apply_transition` | Frontier | Invalid moves involving suspended states are refused with `task.invalid_transition`, including before legacy enforcement rollout; guarded administrative force retains its existing override |
| `task_queries._resume_locked` | Frontier | Stopped WAITING owner is never restored; active continuation's uncleared outcome chooses RESUME_PENDING, otherwise HIBERNATED |
| `task_changes` child/open checks | Open work | Existing terminal complements include all three |
| `task_changes` class-hint route reset and lifecycle edit permissions | Frontier | Queued-only reset retained; suspended targets remain execution-owned and cannot be set by a change-set edit |
| `task_commands` completion filters and ordinary task/list/tree views | Open work | Existing terminal complements include suspended work |
| `task_commands` restart reservation, reroute undo, route hint reset, archive/remove dry-run permissions | Frontier | Existing explicit permissions retained; suspended work requires continuation-aware controls |
| `task_commands` worker reparent and class-hint locks | Frontier | Suspended work remains owned even without `assigned_agent_id` |
| `providers.availability_service.QUEUED_STATUSES` | Frontier | Provider holds are derived only on queued work; suspended continuation remains outside queued rerouting |
| `archive_queries` terminal/archive admission | Frontier | No blind archive of suspended work; one-shot disposition must stop the writer and close first |
| `orchestrator.monitoring` PAUSED timer/orphan/slot recovery | Frontier | Existing timer-owned PAUSED and READY checks retained; no suspended poll/reschedule loop |
| `sessions.obsolete._CLOSEABLE_FROM` | Open work | Operator one-shot close accepts all three; normal worker close still requires acknowledgment |
| `integration.development.OPEN_REPAIR_STATUSES` | Open work | Includes all three to prevent duplicate repair work |
| `integration.delivery_branches.LIVE_TASK_STATUSES` | Open work | Includes all three to retain branches while the continuation owns them |
| `pool_claims.LIVE_POOL_CLAIM_TASK_STATUSES` | Session liveness | WAITING included; HIBERNATED and RESUME_PENDING excluded |
| `claim_commands` held-claim retry | Session liveness | Suspended binding is preserved and returns `not_admissible` / `wait.task_suspended`; never released or served as ordinary work |
| `session_commands` normal close and one-shot writer stop | Session liveness | Normal close returns `wait.ack_required` on all three; WAITING can require a stop for one-shot dispositions |
| `sessions.reconciler` held ownership and premature drain acknowledgment | Session liveness | WAITING remains a held/live claim; other suspended states never become generic claim recovery owners |
| `orchestrator.pools._measure_pools` | Session liveness | WAITING counts busy, HIBERNATED excluded; only a starting RESUME_PENDING launch counts capacity |
| `database.metrics_queries` live session counts and hook session IDs | Session liveness | Same continuation exclusion and starting reservation rule as pool supply |
| `database.task_session_queries.LIVE_WORKER_TASK_STATUSES` | Session liveness | WAITING docks on its live attempt; continuation states have no worker marker |
| `database.agent_queries` active assignment predicates | Session liveness | WAITING included; existing WAITING_INPUT retained |
| `agents.service`, `agents.subagents`, `agents.terminals` active task sets | Session liveness | WAITING included, no HIBERNATED/RESUME_PENDING assignment |
| `commands.flock_commands`, `api.terminal_stream` active task sets | Session liveness | WAITING included, no phantom continuation terminal |
| `messages.session_lens` fenced pool task target | Session liveness | WAITING retains its exact-epoch message target; continuation states do not target the old session |
| `providers.allocation` task-seat ownership and session snapshot | Session liveness | WAITING consumes its live seat; HIBERNATED excluded, RESUME_PENDING counts only a starting launch |
| `providers.allocation.INTENT_TASK_STATUSES` | Open work | Includes all three so provider pins remain visible while a continuation owns the task |
| `database.agent_wait_queries` live owner | Session liveness | WAITING remains a valid legacy wait owner during staged cutover |
| `database.digest_queries` idle/wait status counts | Open work | Includes all three; legacy WAITING_INPUT remains readable |
| `mcp_registration` active task summaries | Open work | Includes all three |
| CLI status styles, sort order, summary filters, pipeline category map and notification labels | Open work | Enumerate and display all three |
| `surface_schema`, config status validation, task status parsing, `tools.definitions` | Open work | Derive full vocabulary from TaskStatus; tool schema also gains formerly missing ASSIGNED/WAITING_INPUT/PAUSED |
| API TaskDetail / TaskDict and task metrics response models | Open work | Status enum and three metric fields advertised in regenerated OpenAPI and clients |
| `taskStatus`, `statusPillModel`, `taskFilters`, `taskReason` | Open work | Three distinct suspended labels, no execution stripes; blocked predecessors do not replace suspension labels |
| `TaskActions`, `TaskRowActions`, `TaskFieldsEditor` | Open work | Pause available on suspended states; lifecycle/hint editor preserves wait ownership; WAITING_INPUT Answer remains unchanged |
| `Tasks`, `WorkIndex`, `TaskGraph`, project `Overview` | Open work | Status filters/tones include all three; Overview's generic counts and terminal complement already include them |
| Metrics sampler `_GRAPHED_STATUSES`, dashboard `metrics/series` | Open work | All three are individually charted |
| `task.updated` lifecycle payload and persisted graph-edit replay | Open work | Carries committed `status`; edit emitter refreshes the task after writes |

WAITING_INPUT stays readable and its writers are unchanged. Wait registration,
source verification, stop proof, wake acknowledgment, continuation cancellation,
intent tombstoning and human-wait projections belong to later service leaves.
The new transition rows supply those services' vocabulary without enabling v2 waits.
The integration ownership inventory charges the shared lifecycle imports and
retained workspace guards. Verified-wait records and continuations are session
storage, not delivery journals; the fixed integration baseline and limits remain
unchanged, with 45 integration tables.
The supervisor confirmed that section 6's CANCELLED transition row and section
4's task settlement set reference a status absent from the baseline TaskStatus.
That discrepancy does not authorize a fourth status in this leaf. Acceptance
covers the three assigned additions and existing COMPLETED/FAILED one-shot
dispositions; CANCELLED transitions are neither implemented nor tested. Owning
wait services must validate supported task statuses rather than silently accept
the unsupported CANCELLED value. Cancellation/tombstoning and human projections
remain inactive until those services integrate; the approved artifact is unchanged.

The branch descends from canonical completed schema prerequisite
`b30155ef61dbbc426d1ddc6731398f4be7abcc26`: revision 97 follows revision 96,
with the earlier collision corrected. That prerequisite incorporates the
workspace-kind revision 96 file byte-identical to `69a45b`; the latter commit
itself is not a Git ancestor of this branch.
