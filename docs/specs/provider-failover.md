# Provider failover: availability state and failover routing policy

<!-- aq:historical -->
> **Design record — not current documentation.** This spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../README.md) for what AQ does today.

**Date:** 2026-09-20
**Status:** proposed — the design the `bold-rapids` implementation tasks are held
to; written ahead of the code, and nothing here has shipped
**Epic:** `bold-rapids` — *Provider failover: re-route work when a provider runs
out of usage or loses its login*
**Implemented by:** `bold-rapids.2` (availability state), `.3` (pin semantics and
the re-route engine), `.4` (in-flight failures), `.5` (dashboard, docs, end-to-end)
**Builds on:** [Providers, models and token accounting](../concepts/providers.md),
[Agents and routing](../concepts/agents-and-routing.md),
[worker pools](../guides/worker-pools.md),
[assignment routing as a playbook](../superpowers/specs/2026-09-06-assignment-routing-as-playbook.md)

Every numbered decision below is marked **D<n>** so an implementation task can
cite it. Where a decision rejects an alternative, the alternative and the reason
are stated, because the next person to touch this will otherwise propose it
again.

## Problem

On 2026-09-20 the operator's OpenAI account ran out of usage. `codex login
status` answered "Not logged in" and `~/.codex/auth.json` was gone. From then
on every Codex launch matched the `login-required` startup dialog
(`src/sessions/default_harnesses/codex.md`), the tmux provider killed the
session (`src/sessions/tmux.py`, `die()`), and the orchestrator recorded
*"session died during startup"*. Seven open tasks sat on Codex profiles,
including two urgent fixes the supervisor had to move to Claude by hand. The
routing playbook went on offering Codex rows the whole time.

Nothing in AQ holds the sentence *"Codex is down"*. What exists instead:

| What happens today | Where | Why it is not enough |
|---|---|---|
| A startup death pauses the task for a flat **60 s**, does **not** consume `retry_count`, and leaves no task note | `_fail_session_launch`, `src/orchestrator/execution.py` (`backoff = 60`) | PAUSED → READY → PAUSED forever. No ladder, no terminal state, no provider-level conclusion. |
| The dialog that killed the session is known structurally (`SessionDiedDuringStartup.detail`) and thrown away; callers format the exception into free text | `src/sessions/provider.py`, `execution.py`, `src/orchestrator/pools.py` | The one strong, typed signal ("this was a login dialog") never reaches anything that could act on it. |
| A pool startup death quarantines `(project_id, profile_id)` for 60 s, in memory | `pools.LAUNCH_BACKOFF`, `Orchestrator._pool_quarantine` | Per project and per rung: N projects × M Codex rungs each rediscover the same dead login, and a restart forgets all of it. |
| A mid-task `RATE_LIMIT` exit pauses the task 900 s on the **same** provider, or quarantines the pool key | `src/sessions/exit_classifier.py`, `src/sessions/reconciler.py` | Waiting is the only strategy. The 900 s is a hard-coded default, not the provider's own `resets_at`. |
| The provider's own quota numbers are stored | `provider_usage_snapshots`, `src/providers/` | Read by the dashboard only. No scheduler, pool sizer or router consults them. |
| A secret-free login probe exists | `probe_login`, `src/install/logins.py` | Called by `aq install` only. The daemon reads the cached `vault/profile-activation.json` and never refreshes it. |
| The scheduler has a `provider_cooldowns` map; config has a `pause_retry:` block | `src/scheduler.py`, `src/config.py` (`PauseRetryConfig`) | **Both are dead**: nothing writes the map, nothing reads the block. |
| Route options carry `enabled` | `build_route_options`, `src/commands/routing_commands.py` | Filters on the operator's kill switch only — not on `ProfileActivation`, not on anything observed at runtime. |
| A task's route is `profile_id` + `intelligence_class` | `tasks` table; `src/assignment_routing.py` | `profile_id` says both *"run this class"* and *"run on this provider"*, so nothing can tell a human's deliberate provider choice from a default. |

Two constraints shape everything below:

* **Some classes exist on one provider only.** `astra-high` / `astra-low` carry
  only `openai`/`codex` slices (`src/prompts/default_intelligence_classes/`).
  The operator routes art-heavy design to Astra on purpose; silently running it
  on another model is wrong.
* **The Claude fleet is finite** — a bounded set of pool rows on five-hour usage
  windows. Failover that empties a dead provider's queue onto it turns one
  outage into two.

## Vocabulary

| Term | Meaning in this spec |
|---|---|
| **Provider** | The *login a session harness draws on*, identified by **provider key**: `claude`, `codex` (and `gemini`, or any operator harness, once it derives rungs). This is the id already used by `provider_logins()`, `WORKER_PROVIDERS`, `CatalogProfile.provider_id` and `provider_usage_snapshots.provider`. |
| **Vendor** | `anthropic` / `openai` / `google` — the intelligence-class slice key (`harness.provider` or `_infer_provider_from_harness`). An attribute of a provider, shown beside it, never the key. |
| **Direct path** | The daemon's own SDK calls (`src/llm/`, config `llm:`). A separate credential from any CLI login, tracked under the reserved provider key `llm`. |
| **Rung** | A derived worker profile `<class>-<harness>` (`rung_profile_id`, `src/profiles/catalog.py`). |
| **Equivalent rung** | For a task on rung `(class, harness)`: the enabled, active profile that `worker_route` maps to `(class, harness′)` for another provider. Same class, different provider. |
| **Launchable / unavailable** | The two halves of the state set — see D1. |
| **Hold** | A queued task that is deliberately not started because its provider is unavailable and policy says it may not move. A hold is *derived*, never a task status. |

**D0 — the availability unit is the harness login, not the vendor.** Every piece
of session-side evidence is harness-shaped: a startup dialog belongs to one CLI,
`codex login status` to one credential store, a usage snapshot to one
subscription. Keying on the vendor would merge things that fail independently —
an exhausted ChatGPT subscription says nothing about an `OPENAI_API_KEY` the
direct path uses, and a provider-agnostic harness such as OpenCode names its
vendor inside the model string (`ollama/qwen3.8:27b`) and has no vendor to key
on. A harness variant declared with `base:` shares its parent's login, so
`provider_key(harness) = harness.base or harness.id`; that function is the single
seam if an install ever needs two unrelated harnesses to share one account.
Operator surfaces accept a vendor as an **alias** when it is unambiguous
(`aq provider status openai` → `codex`); where the epic's acceptance criteria
say "openai unavailable", read "the `codex` provider (vendor `openai`)".

## 1. Provider availability model

### D1 — six states in two halves

| State | Half | Meaning | Carries |
|---|---|---|---|
| `available` | launchable | No evidence against it. | — |
| `degraded` | launchable | Usable, but something is off: quota high, one uncorroborated failure signal, unattributed launch failures, or on probation after an outage (`reason_code = recovering`). | `reason_code` |
| `exhausted` | unavailable | The account is out of usage. | `until` — the provider's own `resets_at`, or a backoff deadline |
| `unauthenticated` | unavailable | The CLI is not logged in. Only a human can fix it. | remediation text from `login_instructions()` |
| `failing` | unavailable | Launches die for a cause AQ cannot name (a broken CLI update, a crashed helper) and the failures are attributable to the provider (D3). | `until` — a backoff deadline |
| `disabled` | unavailable | An operator said so (D6). | `override_until`, `override_by`, `override_reason` |

The task brief listed five states; `failing` is added because the alternative
was to label a broken binary `exhausted`, which sends the operator to the wrong
fix, or to leave it `degraded`, which keeps launching into it — the exact retry
storm this epic exists to stop.

What the halves mean, everywhere:

* **Unavailable** — nothing launches against the provider: the push scheduler
  skips its workers, pool sizing targets zero, the pre-launch check refuses, and
  routing stops offering its rows. Tasks routed to it are re-routed or held (§3).
* **`degraded`** — launches proceed normally for work that is already routed
  there. A degraded provider is **never a failover target** (D14), sorts after
  `available` providers when routing places a `class_only` task, and shows amber
  on the dashboard. Degraded by itself never moves a task.

The *effective* state is the operator override while one is active, else the
state derived from evidence.

### D2 — evidence: typed, from structured sources first

Every collector calls one function,
`record_provider_evidence(provider, kind, signal, *, detail, task_id=None,
session_id=None, project_id=None, observed_at)`, and a **pure reducer**
`reduce(row, evidence, now, config) -> (row′, transition | None)` decides. The
reducer owns no clock and does no I/O, in the same spirit as `size_pools` and
the digest layer; every rule in D3–D5 is a unit test with a fake clock.

| Kind | Source | Signal | Trust |
|---|---|---|---|
| `usage_snapshot` | Newest non-stale row per `(provider, window, scope)` in `provider_usage_snapshots` — staleness per the existing `providers.*stale_after_seconds` horizons | `used_percent`, `resets_at` | **Structured** — the provider's own number |
| `auth_probe` | `probe_login` (`src/install/logins.py`) run off the event loop with a short timeout (D5) | `authenticated` / `not_authenticated` / `cannot_tell` | **Structured**. `cannot_tell` (timeout, `OSError`) is never evidence of anything. |
| `startup_dialog` | `SessionDiedDuringStartup.detail` names the quarantine dialog; the rule's new `signal` field says what it means | `auth` / `usage` | **Strong** — a typed match during startup |
| `exit_rate_limit` | `classify_exit` → `Verdict.RATE_LIMIT`; also the stall ladder's usage-limit screen (D13) | `usage` | **Medium** — pane text. `RATE_LIMIT_PATTERNS` includes a bare `429` and `resets at`, so one match proves little. The stall ladder's pattern set is far stricter, but it is still pane text and records the same kind. |
| `launch_failure` | Any other `SessionDiedDuringStartup` (`end_reason = startup_exit`) | — | **Weak** — may be the repository, not the provider |
| `launch_success` | The session's first authenticated API call with its session token (`prime`, `claim`, `heartbeat`), or its first transcript usage line, whichever is first | — | **Structured** |
| `llm_call` | Direct path only: adapter outcome mapped to `ok` / `auth` (401/403) / `usage` (429 with a quota body, `insufficient_quota`) / `error` | as named | **Structured** |

Only a startup death counts as `launch_failure`. The other callers of
`_fail_session_launch` — routing mismatch, missing harness file, no `work_dir`,
base-checkout refusal, integration attachment, workspace acquisition — are AQ's
own faults and are never provider evidence.

**The dialog signal lives in the harness file.** `DialogRule` gains an optional
`signal: "auth" | "usage"`. Shipped values: `codex`/`login-required` → `auth`,
`gemini`/`login-required` → `auth`, `claude`/`rate-limit` → `usage`. Vault
harness copies are operator-edited and do not pick up shipped changes, so when
`signal` is absent the collector falls back to a built-in name map
(`login-required` → `auth`; `rate-limit`, `usage-limit` → `usage`). What a
dialog *means* is harness knowledge and belongs beside the pattern that detects
it; the name map exists only so an un-reset vault still works.

To make this possible `.4` passes `exc.detail`, the harness id and the profile
id into the launch-failure path as fields, instead of only formatting the
exception into `reason`.

### D3 — trip rules, and the hysteresis that keeps one flaky launch from flipping a provider

No single unstructured observation changes the half a provider is in. A trip
needs either the provider's own structured statement or **two independent
signals**.

| To | Trips when | `until` |
|---|---|---|
| `unauthenticated` | (a) one `startup_dialog(auth)` **and** the auth probe it triggers answers `not_authenticated`; or (b) two consecutive `startup_dialog(auth)` with no `launch_success` between them, whatever the probe says (a token can be present and dead); or (c) two consecutive scheduled probes ≥ 60 s apart answer `not_authenticated` | none |
| `exhausted` | (a) a non-stale `usage_snapshot` on an **account-wide** window (scope empty or `all models`) at `used_percent ≥ usage.exhausted_percent`; or (b) two consecutive `startup_dialog(usage)`, or one plus a snapshot `≥ usage.degraded_percent`; or (c) `exit_rate_limit` from two distinct sessions inside `launch.window_seconds`, or one plus a snapshot `≥ usage.degraded_percent` | the fullest account-wide window's `resets_at` when known and in the future; else `now + rate_limit.cooldown_seconds × 2^level`, capped at `recovery.backoff_max_seconds` |
| `failing` | `launch.generic_failures_to_trip` consecutive `launch_failure` with no `launch_success`, inside `launch.window_seconds`, **and attributable**: the failures span ≥ 2 projects, *or* another provider recorded a `launch_success` in one of the same projects inside the window | `now + recovery.failing_backoff_seconds × 2^level`, capped |
| `degraded` | a snapshot `≥ usage.degraded_percent`; a **model-scoped** window (e.g. Claude's `week`/`Opus`) at or past `exhausted_percent`; one uncorroborated `startup_dialog` or `exit_rate_limit`; the `failing` count reached without attribution; or probation (D4) | — |

Consequences worth stating:

* **The 2026-09-20 incident trips in one launch.** First Codex launch dies on
  `login-required`; the collector starts `codex login status`; it answers "not
  logged in"; rule (a) fires. If the probe cannot tell, the second launch
  ~30 s later trips rule (b). `.2`'s acceptance bound is therefore **two
  launches**.
* **The state says what the evidence says, not what caused it.** That day the
  root cause was an exhausted account, but what AQ could observe was a missing
  login, so the state is `unauthenticated` and the remediation reads *"run
  `codex login`; if the account is out of usage the login will not stick until
  it resets"*. AQ does not guess past its evidence.
* **One flaky launch yields at most `degraded`**, which moves no task and
  suppresses nothing. A single stray `429` in a pane yields `degraded`.
* **A broken repository does not take a provider down.** Five startup deaths in
  one project while no other provider launched there is not attributable; the
  provider goes `degraded` with `reason_code = launch_failures_unattributed`
  and the existing per-`(project, profile)` key quarantine keeps doing its job.
* **Model-scoped exhaustion is visible but not acted on.** Provider-level
  granularity is the v1 unit. Mapping a scope label like `Opus` onto
  intelligence classes is prose matching, and getting it wrong pulls Sonnet work
  off a healthy account. A scoped window at 100 % marks the provider `degraded`
  with the scope in the reason; if launches then actually die on it, rules
  (b)/(c) apply like any other evidence. Recorded as a limitation in §9.

Percent thresholds carry a `usage.hysteresis_percent` band (default 2): leaving
`degraded` needs a reading below `degraded_percent − 2`.

### D4 — recovery is a half-open breaker, expressed as `degraded`

An unavailable provider never jumps straight to `available`. It becomes
`degraded` with `reason_code = recovering` — **probation** — and the first
`launch_success` completes the recovery.

| From | Moves to probation when |
|---|---|
| `exhausted` | `until + recovery.reset_grace_seconds` passes; or earlier, when a fresh snapshot shows the blocking window below `usage.degraded_percent` (an early reset, an upgraded plan). Claude's `/usage` probe keeps running every ten minutes whatever the state, so Claude recovers on evidence; Codex snapshots only arrive while a Codex session runs, so Codex recovers on the clock. |
| `unauthenticated` | the auth probe, run every `recovery.auth_probe_interval_seconds` while in this state, answers `authenticated`. `aq provider recheck <provider>` runs it now. |
| `failing` | `until` passes. |
| `disabled` | the override expires or is cleared; the state is then **re-derived from evidence**, which may well be another unavailable state. |

While on probation:

* at most **one** launch may be in flight for the provider (the canary) until
  the first `launch_success` — eight pool workers do not stampede a login that
  may still be dead;
* a single strong or medium failure signal returns it to the state it came from
  **without corroboration**, with `level + 1` (so the next backoff doubles);
* the first `launch_success` makes it `available`. `level` decays to 0 after
  `recovery.flap_window_seconds` with no trip.

No canary task is manufactured. If nothing is routed to a recovering provider it
simply stays `recovering` — launchable, harmless — until real work arrives. A
pool with `min_active > 0` supplies the canary by itself.

A `launch_success` observed while a provider is `exhausted` or `failing` (a
session that was already running and is still making turns) is recovery
evidence and moves it to probation early. It does not clear `unauthenticated` or
`disabled`.

### D5 — the auth probe runs in the daemon, carefully

`probe_login` is synchronous `subprocess.run` with a 30 s timeout and one retry —
fine for an installer, wrong for an event loop. The daemon runs it through
`asyncio.to_thread` with `auth_probe.timeout_seconds` (default 10), never two at
once per provider, and only:

* immediately after a `startup_dialog(auth)` (corroboration, D3);
* every `recovery.auth_probe_interval_seconds` while `unauthenticated`;
* every `auth_probe.interval_seconds` (default 600, `0` disables) while
  launchable, so a logout is noticed before a launch dies on it;
* once in `Orchestrator.initialize`, seeding state before the first tick;
* on `aq provider recheck`.

`vault/profile-activation.json` stays what it is — install-time eligibility,
written by `aq install`. Runtime availability does not rewrite it; routing
filters on both.

### D6 — operator override, always with an expiry story

```bash
aq provider set-state codex disabled --for 4h --reason "rotating the account"
aq provider set-state codex available --for 1h --reason "false positive, Opus-only limit"
aq provider set-state codex auto          # clear the override
```

* `disabled` forces the unavailable half. Default expiry
  `override.default_ttl_seconds` (4 h); `--until <ts>` or `--for <dur>` up to
  `override.max_ttl_seconds` (7 d);
  `--no-expiry` is accepted for `disabled` only.
* `available` forces the launchable half **against the evidence** and therefore
  always expires (no `--no-expiry`). Evidence keeps being collected and shown —
  *"evidence says exhausted until 14:00; overridden by human:cli until 13:10"* —
  and nothing is suppressed while it lasts, so today's pause-and-retry behaviour
  is what protects the box.
* `auto` clears the override, resets failure counters and `level`, and
  re-derives. It is also the "I just ran `codex login`" button, alongside
  `recheck`.
* Expiry is evaluated by the reducer on the orchestrator tick; an expired
  override emits the same state-change event as any other transition.
* Human and supervisor principals only. A task-scoped worker token is refused.

### D7 — where the state lives

Two tables, PostgreSQL, one writer (the daemon):

`provider_availability` — one row per provider key:

| Column | Note |
|---|---|
| `provider` PK, `vendor` | key and display attribute |
| `state`, `reason_code`, `reason` | derived state; `ck_provider_availability_state` names the six values |
| `since`, `until` | when the current state began; expected recovery, nullable |
| `level`, `last_trip_at` | flap backoff |
| `consecutive_failures`, `last_failure_at`, `last_success_at` | counters the reducer needs |
| `evidence` JSON | the last `evidence.keep` (20) items, newest first — what `aq provider status --verbose` prints |
| `override_state`, `override_until`, `override_by`, `override_reason`, `override_set_at` | D6; all NULL when none |
| `generation` | incremented on every change of *effective* state; re-route batches are keyed by it (D19) |
| `updated_at` | |

`provider_availability_transitions` — append-only: `id`, `provider`,
`from_state`, `to_state`, `reason_code`, `reason`, `until`, `generation`,
`actor` (`system` or a principal), `detail` JSON, `at`. This is the audit trail
and the dashboard card's history.

Why a table and not the in-memory maps that pool quarantine uses: a restart
must not forget that Codex is logged out and spend its first minute
rediscovering it across every project; and `aq provider status`, the API and
doctor need one answer.

The orchestrator keeps a read-through snapshot
(`Orchestrator.provider_availability`), refreshed on every transition and once
per cycle, so the scheduler, pool sizing, pre-launch check, claim admission and
route options never touch the database for it.

**Event on change:** `provider.state_changed` on the bus, once per change of
effective state, payload `provider`, `vendor`, `from_state`, `to_state`,
`reason_code`, `reason`, `since`, `until`, `generation`, `actor`,
`override` (bool). Evidence that does not change the state emits nothing —
evidence is not news.

**Retired by this design:** `Orchestrator._provider_cooldowns` and
`SchedulerState.provider_cooldowns` (never written; replaced by the snapshot
above, and `explain.py`'s `rate_limited` reason by `provider_hold`, D18), and
the dead `pause_retry:` config block (superseded by `provider_failover:`; `.2`
removes the dataclass, its validator entry and its `TuningNote`s, tolerating the
key in existing YAML with a deprecation warning).

## 2. Pin semantics

### D8 — a task records provider intent in its own column

`tasks.provider_intent TEXT NOT NULL DEFAULT 'class_only'`, constrained by
`ck_tasks_provider_intent` to three values (mirrored on `archived_tasks`):

| Intent | Meaning | `profile_id` is… | When its provider is unavailable |
|---|---|---|---|
| `pinned` | A human chose this provider on purpose. | a requirement | **Hold.** Never moved automatically. |
| `preferred` | Somebody named a provider, as a preference. *This is what an explicit `profile_id` means by default.* | a preference | Fail over per D12. |
| `class_only` | The task cares about its class. Nobody chose the provider; routing placed it. | a materialised placement, free to change | Fail over per D12. |

`profile_id` and `intelligence_class` keep their meaning and their write paths
(`update_task_routing`'s guard — not running, not claimed — applies to intent
too). The column answers the one question `profile_id` cannot: *did anyone mean
the provider?*

`preferred` and `class_only` fail over identically. They differ in one place:
`task_route_options` today narrows its catalog to the task's own profile
whenever `profile_id` is set, *"because choosing another profile would silently
change the provider"*. That narrowing now applies to `preferred` and `pinned`
only. A `class_only` row is routed on its class, so when routing is asked about
it again its old placement constrains nothing. There is no load balancing here
(§9) — only the absence of a constraint nobody asked for.

Rejected: encoding intent in `task_metadata`. Intent is read by the re-route
sweep, route options and the pre-launch check on every cycle; it filters SQL and
must be indexable. Rejected: reviving `task_assignment_routes`; it has no reader
or writer in `src/`, is one row per task keyed to a playbook run, and cannot hold
a human's choice.

A reader that finds `pinned`/`preferred` with `profile_id IS NULL` treats it as
`class_only`. There is deliberately no cross-column CHECK: profile deletion and
retirement paths already null or repoint `profile_id`, and a constraint would
turn an operator's profile cleanup into a constraint violation.

### D9 — who sets which intent

The command argument is `provider_intent`; the CLI sugar is `--pin` (= `pinned`)
and `--provider-intent <value>`. When the argument is omitted the default
follows **how the profile was chosen**:

> a `profile_id` the caller supplied ⇒ `preferred`; a `profile_id` a resolver
> supplied, or none ⇒ `class_only`.

| Surface | Result |
|---|---|
| `aq task create --profile P` | `preferred` |
| `aq task create --profile P --pin` | `pinned` |
| `aq task create --intelligence-class C` (profile chosen by class match) | `class_only` |
| `aq task create` with nothing (project default left implicit, `profile_id` NULL) | `class_only` |
| Worker-filed task inheriting the filing worker's profile | `class_only` — the rung the filer happened to run on is incidental |
| Worker-filed task with an explicit `--profile P` | `preferred` |
| `aq task route --profile-id P` by a human or supervisor | `preferred`; `--pin` → `pinned` |
| `task_route` from the `default-assignment-routing` playbook (steps 3 and 4) | passes `provider_intent: class_only` explicitly. `task_route` **never downgrades**: a row already `pinned`/`preferred` keeps its intent when the routed profile is on the same provider. |
| `aq task edit --profile-id P` | `preferred`; `--pin` → `pinned` |
| `aq task edit --profile-id null` | `class_only` (clearing the profile clears the intent) |
| `aq task edit --provider-intent X` | sets it alone; `pinned`/`preferred` require a `profile_id` |
| `aq-graph` node with `profile:` | `preferred` |
| `aq-graph` node with `profile:` and `pin: true` | `pinned` |
| `aq-graph` node with only `intelligence_class:` | `class_only` |
| `--profile-id` fill-in on `create_task_graph` for nodes that left it empty | `preferred` (the caller supplied it) |
| Playbook `agent_task` step naming a profile | `preferred`; `pin_provider: true` → `pinned` |
| Project default | never written to the row, so `class_only`. `projects.default_profile_id` expresses the project's *first choice of provider*, not a requirement (D13). |

The graph grammar keeps refusing `provider`, `harness` and `model`
(`src/task_graph/parser.py`, `unsupported_routing`): the profile already names
the provider, and a second spelling is a second thing to disagree. `pin` is the
only new node key (also accepted in `defaults`).

**Who may pin.** `pinned` is a human's statement, so it is accepted from human
principals (CLI operator, dashboard, `human:discord:*`), from a project
supervisor session (it acts on a human's instruction and is already elevated),
from a **vault** formula (`formula_cook` — vault files are operator-owned, the
same trust as a profile) and from a reviewed playbook. A task-scoped worker
token that passes `--pin`, or an inline `--graph` with `pin: true`, is refused
with `provider_intent.pin_not_permitted`; the refusal names the field. Every
write records `provider_intent_by` (principal) and `provider_intent_at` in the
`task_metadata` key `provider_intent_audit`.

**Why an explicit profile is `preferred`, not `pinned`.** The operator asked for
work to move when a provider runs dry. `--profile` has been the only way to pick
a rung for the life of the project, so it carries no reliable provider signal;
making it a pin would hold most of the queue during the next outage and recreate
the incident. A hold is the exception someone opts into with one flag, and the
dashboard route picker gets a *Pin to this provider* checkbox, off by default.
A human's choice stays distinguishable from a default in both directions: by the
intent, and by `provider_intent_by`.

**Pins bite on the push path too.** A class-backed Claude profile is a *generic*
worker route (`_generic_worker_profile`, `src/agents/routing.py`), so today a
task on `standard-high-claude` may be taken by any worker of that class.
`explicit_route` (`src/assignment_routing.py`) fills
`EffectiveAssignmentRoute.provider` with the profile's vendor **when the intent
is `pinned`**, which arms the already-wired, currently-inert `required_provider`
check in `routing_mismatch` and `_check_agent_routing`. `preferred` and
`class_only` leave it `None`, as now.

### D10 — migration of existing rows

One revision (after the current head), idempotent:

```sql
-- add column with server_default 'class_only' (inspector-guarded), then:
UPDATE tasks          SET provider_intent = 'preferred'
 WHERE profile_id IS NOT NULL AND provider_intent = 'class_only';
UPDATE archived_tasks SET provider_intent = 'preferred'
 WHERE profile_id IS NOT NULL AND provider_intent = 'class_only';
```

* **Existing explicit routes become `preferred`, never `pinned`.** No existing
  row can prove a human meant the provider: the routing playbook writes
  `profile_id` on every task it routes, so a non-NULL `profile_id` is mostly
  router output.
* **Why not `class_only` for router-written rows**, which is what they arguably
  are? The two fail over identically and differ only in whether the existing
  `profile_id` narrows the routing catalog (D8) — which it does for every row
  today. Migrating to `preferred` keeps today's behaviour exactly, so the
  upgrade changes nothing on a healthy box.
* **The Astra case survives the migration without a pin**: an `astra-high` task
  becomes `preferred`, and holds anyway because no other provider has an
  `astra-high` rung (D12). The class protects it, not the intent.
* NULL `profile_id` rows keep the default `class_only`.
* Follows the house rules: inspector guard before `add_column`, named CHECK,
  bare `server_default`, written by hand rather than autogenerated from a
  worktree, and `alembic` `get_heads()` checked after basing because sibling
  branches collide on revision ids.

The same revision creates `provider_availability`,
`provider_availability_transitions` and `task_reroutes` (D17) and adds
`tasks.rerouted_from` / `archived_tasks.rerouted_from`. `.2` and `.3` land
separately, so in practice this is two revisions chained in that order.

## 3. Failover policy

### D11 — mechanism in code, policy in a playbook

Three things are **mechanism**. They live in the daemon, need no playbook, and
are what makes an unavailable provider safe even on an install where nothing
else in this section is active:

1. **Launch suppression.** Nothing starts against an unavailable provider:
   `idle_workers` (`src/scheduler.py`) drops workers whose profile resolves to
   it, `AgentReconciler` grows none for it, `_measure_pools` sizes its pools to
   zero, the pre-launch check in `src/orchestrator/execution.py` refuses, and a
   claim from one of its sessions answers `drain_requested`.
2. **The derived hold.** A queued task whose effective profile is on an
   unavailable provider is *held*. It keeps its status — `READY` stays `READY` —
   and `aq task explain`, `task show` and the API report a computed
   `provider_hold` (D18). No status means no state-machine churn, no
   `resume_after` to forget, and release is automatic the moment the provider is
   launchable again.
3. **The catalog filter.** `build_route_options` annotates every row with
   `provider_key`, `provider_state` and `launchable`; automatic selection
   excludes non-launchable rows and reports them as `unavailable_options`,
   beside the existing `disabled_options`.

Moving a task is **policy**, so it is a command driven by a playbook, like
assignment routing and the CI sentinel:

* Command `provider_reroute` (contracted, `SideEffectClass.COMPOSITE`): plans
  and applies one sweep under D12–D16 and returns what it did. `dry_run: true`
  plans only — the same code path the dashboard preview and
  `aq provider reroute --dry-run` use.
* System default playbook `src/prompts/default_playbooks/provider-failover.md`,
  triggers `provider.state_changed` and `timer.5m`, **command steps only, no
  LLM**. Rule one calls `provider_reroute`; outcomes `rerouted`, `held`, `idle`
  (nothing unavailable) and `disabled` all end the run successfully. Rule two
  sends the notifications in D19. It is added to `src/playbooks/required.py`
  with a reviewed bundle under `tests/fixtures/playbooks/v2/provider-failover/`.
* A project that wants other rules keeps a project-scope copy that passes
  different arguments. No code changes.

If the playbook is not active, tasks hold with a visible reason and nothing
moves. That is the safe failure, and `aq doctor` reports it (D21).

### D12 — the fallback rule: same class, next provider, or hold

For a task on rung `(class, harness)` whose provider is unavailable:

```text
policy = provider_failover.classes.get(class, provider_failover.default_policy)

pinned                         -> HOLD   (provider_pinned)
policy == "hold"               -> HOLD   (class_policy_hold)
policy == "same_class":
    for provider' in order, skipping the task's own and any it already left:
        rung' = equivalent rung (class, provider')      # enabled, active, class has a slice
        if rung' exists and provider' is `available`  -> RE-ROUTE to rung'
    nothing qualified          -> HOLD   (no_equivalent_rung | no_available_target)
```

* **`order`** is `provider_failover.order`; empty means the project default's
  provider first, then `WORKER_PROVIDERS` order.
* **The class never changes.** v1 has exactly two policies, `same_class` and
  `hold`. Cross-class substitution (`astra-high → deep-high`) is a non-goal: it
  is precisely the silent model swap the operator ruled out, and a human who
  wants it can say so per task with `aq provider reroute --task … --to-profile …
  --force`.
* **Single-provider classes hold by construction**, not by a list somebody has
  to maintain. `astra-high` has no slice for `anthropic`, so no Claude rung
  exists, so `no_equivalent_rung`. The same is true of any class an operator
  adds with one vendor's slice. An explicit `classes: {deep-high: hold}` entry
  exists for the opposite case — a class that *could* move and that the operator
  does not consider interchangeable across vendors.
* **Role profiles hold.** `supervisor`, `triage`, `reviewer`, `final-reviewer`,
  `playbook-compiler` and `spec-ingest` are not rungs — `worker_route` returns
  `None` — so they have no equivalent. A role whose harness is on an unavailable
  provider holds with `no_equivalent_rung`, and the state-change notification
  names the affected roles, because a project whose supervisor cannot launch
  wants a human sooner than one with a slow queue. Launch-time harness
  substitution for roles is listed in §9.

### D13 — what happens to each kind of work

| Work | `class_only` / `preferred` | `pinned` |
|---|---|---|
| **Queued** (`DEFINED`/`READY`/`BLOCKED`, unassigned) on an unavailable provider | Re-routed by the sweep under D12 and the capacity rules (D14–D15). Until its turn comes it is held with `awaiting_failover_capacity`. | Held. Released automatically on recovery. |
| **Unrouted** (`profile_id` NULL, or missing a class) | `_effective_default_profile_id` becomes availability-aware: when the project default's provider is unavailable it resolves to the default's equivalent rung on the next `available` provider. **Derived per call, never persisted** — `projects.default_profile_id` is not rewritten, so recovery needs no undo. The same resolver feeds pool demand, the routing catalog's `prefer_provider` and the claim path's default-profile widening (which today reads the raw column and must switch). | n/a |
| **PAUSED by a provider failure** | New pauses carry the cause (D17). Once the provider has tripped there is nothing left to wait out, so the sweep returns the task `PAUSED → READY` at once instead of at `resume_after`, and the queued rule above applies: re-route. | The same `PAUSED → READY`, after which it is an ordinary held task. Launch suppression is what keeps it from starting, so no special paused-and-pinned state exists. |
| **PAUSED before this shipped** (the seven tasks of 2026-09-20) | No recorded cause, so never touched automatically. `aq provider reroute --provider codex --include-paused` handles them by hand, listing what it will move first. | same |
| **Mid-retry / in-flight** (`.4`) | A failure *attributed to the provider* — a `startup_dialog` signal, a `RATE_LIMIT` exit, or any startup death while the provider is already unavailable — does **not** consume `retry_count` and does **not** pause into the dead provider. If the provider has tripped, the task returns to `READY` and the sweep decides. If this was the first signal and the trip awaits corroboration, the task pauses for `launch.suspect_backoff_seconds` (30) with context `provider_suspect`, which is what lets the second launch confirm or clear it. Unattributed startup deaths behave exactly as today. | Same accounting; the task then holds. |
| **A session that dies mid-task on a usage limit** (`.4`) | Before the workspace is released the daemon (1) commits uncommitted changes on the task branch as `aq-wip: provider failover checkpoint` through the async `GitManager` API, (2) pushes the branch, (3) writes a **hand-off note** as a task comment and `task_metadata['provider_failover_handoff']`: previous profile, provider and model, session id and `aq session logs <id>` pointer, exit verdict, branch and head SHA, whether a WIP commit was made, and the subtask checklist state. The next worker's `aq prime` shows it. If the push fails the task **holds in place** with the workspace kept — nothing is discarded to make a re-route possible. | Same preservation; then holds. |
| **A session parked mid-task on a usage-limit screen** (`.8`) | Claude Code and Codex usually do not exit on a mid-task usage limit: they print the limit line and sit at the prompt, so nothing above sees them until the stall ladder does, which would nudge a CLI that cannot answer and restart it ~23 minutes in. Instead, once the session is stalled (`sessions.lease_ttl_seconds`), before each ladder rung the reconciler peeks the pane and matches its last 15 non-blank lines against a **strict** set of the CLIs' own blocking messages (`src/sessions/usage_limit_screen.py` — the CLI's `⎿`/`■` gutter at the margin, then `You've hit your … limit`, `You’ve hit your usage limit`, `You're out of usage credits`, …; never `RATE_LIMIT_PATTERNS`, whose bare `429` ordinary output hits). A match stops the process and applies `RATE_LIMIT` with reason `usage-limit screen on a stalled session: <line>`, which is `exit_rate_limit` evidence and then exactly the row above. No restart is spent; the rung counter resets. `mode: enforce` only; `observe`/`off` keep the ladder. A failed stop leaves the session to the ladder. | Same; then holds. |
| **Pool sessions** | Target size 0 for every pool on the provider; `aq pool status` shows `provider_unavailable` with the state and `until` — distinct from `placement_starved`. Idle sessions get `drain_requested` on their next claim. **Busy sessions are left alone**: a session still making turns is recovery evidence (D4), not something to kill. Startup deaths attributed to provider evidence do **not** arm the `(project, profile)` key quarantine or count toward `sessions.max_restarts`. Demand follows the tasks: re-routed work raises demand on the target rung, inside that rung's own bounds. | Pinned tasks add no demand anywhere while held. |
| **Playbook `agent_task` steps** | The created task follows every rule above — a step that names a profile creates a `preferred` task (D9), so it fails over like any other. The run's wait is untouched: it ends when the step's `timeout_seconds` does (see §9 for why that is the only way it ends today), and the run overlay shows the child's `provider_hold` as the reason it is waiting. | `pin_provider: true` on the step. The child holds; the run waits out the outage or times out, which is the author's choice to make. |
| **Headless `llm` steps and other direct-path callers** | See D13a. | n/a |

**As built (`bold-rapids.4`).** The decision is one pure function,
`decide(availability, failure)` in `src/providers/inflight.py`, read *after*
the failure's evidence is recorded: `tripped` when the provider is now in the
unavailable half (so a death that trips it is attributed to it), `suspect`
when the failure carries the provider's own signal (a typed startup dialog, a
`RATE_LIMIT` exit, a pre-launch refusal) but the trip awaits corroboration,
`unattributed` otherwise and always outside `mode: enforce`. The launch path
passes a `ProviderFailure` (kind, provider, harness, profile, dialog, signal,
detail) into `_fail_session_launch`; the exit path is
`SessionReconciler._apply_provider_failover`, and the orchestrator half
(`provider_failover_checkpoint` / `provider_failover_hold`) is
`src/orchestrator/provider_failover.py`. Details the table leaves open:

* **Which exits count.** A `RATE_LIMIT` exit always; a `rapid_crash` or
  `productive_death` only while the provider is unavailable. A task session's
  `provider_suspect` pause and its `provider_pause` record are written by
  `transition_task_with_meta` in one transaction; a pool session's go through
  `terminate_pool_session(resume_after=…, task_meta=…)`, same transaction as
  the claim release. `_apply_transition` deletes `provider_pause` whenever a
  task leaves `PAUSED`, by any route, so a stale record can never label a
  later, unrelated pause as the provider's.
* **The pool key quarantine** is no longer armed by an attributed exit either
  (it was already not armed by an attributed startup death): the provider's
  own state sizes its pools to zero once it trips. `observe` mode keeps the
  old 900 s key quarantine and the 15-minute pause.
* **The checkpoint** is `acommit_all(…, no_verify=True)` then
  `stranded_work.preserve_unpushed_work` (never forced; `aq/<task>-wip` when a
  diverged remote branch holds the name), recording `unmerged_branch` /
  `unmerged_commit` like a failing close does. It touches only the workspace
  locked by the task, never a pool claim still `preparing` (`not_started`),
  and plan files -- left out of every task commit -- do not count as work at
  risk. It runs in the orchestrator cycle, so each is bounded by
  `CHECKPOINT_BUDGET_SECONDS` (90 s); an overrun is `unknown`, which holds. A
  hierarchy/train branch is left to its integration owner (`checkpoint:
  integration_managed`), and a tripped task there takes a short provider pause
  instead of READY, as the launch path does when the integration release is
  unconfirmed.
* **Order on the exit path:** checkpoint while the session row is still live
  (a daemon that dies mid-push re-runs the failover next tick, where a row
  already marked non-live would have let the orphan sweep BLOCK the task),
  then the session row, then the claim's resources are released *before* the
  task becomes claimable, then a status-guarded transition (`from_statuses`:
  a task an operator closed or paused meanwhile is left alone), then the
  hand-off note -- only for an outcome that was actually written.
* **"Holds in place with the workspace kept"** is an operator pause through
  the existing manual-pause machinery (`needs_attention:
  provider_failover_push_failed`): the dead session is confirmed stopped, a
  local Git checkpoint (`task_checkpoint.capture_checkpoint`) is taken before
  the slot is released, and whichever slot the task lands in after
  `aq task resume` restores it. The sweep never touches an operator hold. The
  checkpoint also covers `no_remote`, `dirty` (work the WIP commit could not
  take) and `unknown`, because none of them proves the work is safe.
* **The hand-off note** is `task_metadata['provider_failover_handoff']` plus a
  system comment (`system:provider-failover`), and `aq prime` renders it in
  the task-context section on its own, so a later re-route comment cannot push
  it out of the five recent comments.
* **A move to another provider drops `session_resume_key`**
  (`ProviderRerouteService._move`): the carried conversation id belongs to the
  old CLI, and a harness without a transcript reader would take it unchecked.

### D13a — the direct path: tracked, fail-fast, optional fallback

The direct path has one credential (`llm.provider`, `llm.api_key`,
`llm.base_url`), which is why a profile's harness is deliberately ignored there.
So:

* It is tracked as provider key **`llm`** by the same reducer, from `llm_call`
  evidence: 401/403 → `unauthenticated`; a quota-shaped 429 → `exhausted` with
  `until` from `Retry-After` when present; `launch.generic_failures_to_trip`
  consecutive transport/5xx errors → `failing`. An ordinary rate-limit 429 is
  `degraded` at most. Today the adapters simply raise and the playbook executor
  records only the exception's class name, so an auth failure and a rate limit
  are indistinguishable; `.2` adds one `classify_llm_error(exc)` beside the
  adapters, and that plus the breaker is all the new direct-path code there is.
* While `llm` is unavailable, calls **fail fast** with the existing typed outcome
  `provider_error` (detail `provider_unavailable`), without calling the vendor
  or burning a step timeout. Playbook authors already branch on that outcome.
  After `until`, the next call is the canary.
* **No automatic cross-vendor failover unless configured.** `llm.fallback` is an
  optional second full credential block (`provider`, `api_key`, `base_url`,
  `model`, `default_class`). When the primary is unavailable, calls resolve
  their class against the fallback's provider slice and use it; a class with no
  slice there returns `provider_error`. Rejected: borrowing a session harness's
  login for the direct path — AQ never holds those credentials.
  As built (`LLMFallbackConfig`, `resolve_fallback_call` in `src/llm/spec.py`):
  the block is self-contained — a call naming no class uses
  `fallback.default_class`, else `fallback.model`, and only `max_tokens` is
  shared with the primary. An explicit model id is honoured only when the
  fallback is the same vendor (a second key); otherwise it is `provider_error`
  too. The gate is read on every call, so a tool loop moves between the two
  credentials turn by turn. Fallback calls are **not** `llm` evidence: they say
  nothing about the primary credential, and the primary's recovery is still
  its own canary after `until`. A fallback that repeats the primary's provider,
  `api_key` and `base_url` is a validation error — it is the same credential.
* Knock-on effect, stated so nobody is surprised: `default-assignment-routing`
  uses an `llm` step to choose a class for *undecided* tasks. With `llm` down and
  no fallback, undecided tasks stay unrouted (`awaiting_intelligence_route`) and
  the run fails every two minutes as it does today for any provider error. Tasks
  whose class is already set route deterministically and are unaffected.

`task_route_options` gains one outcome, **`held`**: the task has options in
principle but every one is on an unavailable provider. The routing playbook ends
the rule quietly on it, instead of failing a run every two minutes per task for
the length of an outage.

## 4. Capacity protection

### D14 — failover raises no ceiling

A re-routed task is an ordinary queued task on the target rung. It is claimed
under that rung's `max_active`, `swarm.global_max_active`,
`project.max_concurrent_agents` and workspace capacity exactly like native work.
The sweep changes **no** bound, no `enabled` flag, no agent row. If the target
pool is `enabled: false`, it is not a target.

There is no knob called a "Claude fleet cap". The cap *is* those numbers: each
Claude rung's `max_active`, the enabled worker rows behind it, and
`swarm.global_max_active` over the lot. Leaving all three alone is how failover
respects it.

* **Priority is priority.** The claim frontier orders by `priority, created_at`
  (lower number first) and a re-routed task keeps both. It is neither boosted
  nor demoted: an urgent Codex fix outranks routine Claude work, and routine
  Codex work queues behind urgent Claude work.
* **Credit weights are untouched.** `credit_weight` is a per-*project* token
  share; a re-route changes neither the project nor how its tokens are ledgered.
* **The claim query is not changed.** Its three `idx_tasks_claim_frontier*`
  indexes make it an ordered scan that stops at the first row; a provider join
  would undo that. Availability is enforced on the *claiming session's* provider
  (one snapshot lookup), never per candidate task.
* **Only an `available` provider is a target** (`reroute.allow_degraded_target:
  false`). Claude at 85 % of its five-hour window is `degraded`, so failover
  traffic stops arriving before it can push Claude over the edge, and the
  remaining tasks hold. This is the main protection for the Claude fleet's
  *usage*; the pool bounds protect its *concurrency*.

### D15 — re-routes trickle; they do not dump

The sweep does not move the whole dead queue. Per target rung it keeps at most

```text
target_backlog = max(1, ceil(reroute.target_backlog_factor × max_active(rung)))   # default factor 1.0
```

re-routed-and-not-yet-started tasks queued, topping up in `priority, created_at`
order. The rest stay where they are, held with `awaiting_failover_capacity`
(the reason says how many are ahead).

Why a trickle rather than one bulk move:

* a short outage moves one pool-width of work, not the queue — and everything
  not yet moved **simply runs where it was** when the provider returns, which is
  most of the return path for free (D16);
* the blast radius of a false positive is bounded by the same number;
* the most urgent work moves first without any new priority machinery;
* it needs nothing from the claim path.

Further limits, all per sweep or per task:

| Knob | Default | Purpose |
|---|---|---|
| `reroute.max_per_sweep` | 10 | A sweep is small and frequent, never one large write. |
| `reroute.max_priority_value` | unset | When set, only tasks with `priority ≤` this move automatically; the rest hold. For an install that wants failover for urgent work only. |
| `reroute.task_cooldown_seconds` | 1800 | A task moved automatically is not moved automatically again inside this window. |
| `reroute.max_auto_per_task` | 2 | After two automatic moves a task holds for a human: `reroute_limit_reached`. Two providers flapping in turn cannot ping-pong a task. |

**Every provider unavailable.** There is no target, so the sweep moves nothing;
everything holds with `all_providers_unavailable`; launches are suppressed; pool
workers' claims answer `not_admissible` with reason `provider_unavailable` and a
wait hint of the earliest `until`, which the pool loop already handles. One
notification says so (D19), and it becomes a human escalation when no provider
has an `until` at all (every one needs a login) or the earliest `until` is more
than `notify.escalate_all_down_after_seconds` away. The `llm` key does not count
toward "every provider" — the direct path being down strands routing of
undecided tasks, not execution.

## 5. Return path

### D16 — new work returns at once; moved work stays moved

When a provider leaves the unavailable half:

* **New and held work routes normally, immediately.** Holds are derived, so they
  vanish with the state; pools size back up; routing offers the rows again. On
  probation the canary limit (D4) paces the first launches.
* **Tasks already re-routed and still queued do not move back.** Moving them
  again buys nothing — the target is healthy and they have a queue position — and
  it is half of a ping-pong. Because of the trickle there is at most one
  pool-width of them per rung. `rerouted_from` stays on the task as the record.
* **Running tasks finish where they are.**
* **An operator can undo.** `aq provider reroute-undo --batch <id>` or
  `--task <id>` restores `profile_id` to `rerouted_from` for tasks that are not
  running or claimed (the `update_task_routing` guard), writes a `task_reroutes`
  row with `reason_code = operator_undo`, and clears the marker. Undo is refused
  while the original provider is still unavailable unless `--force`.
* **Intent never changes on a re-route.** A `preferred` Codex task moved to
  Claude is still `preferred`; if Claude later fails and Codex is back, D12 may
  move it home, subject to `max_auto_per_task`.

## 6. Visibility

### D17 — what is recorded on the task

| Where | What |
|---|---|
| `tasks.provider_intent` | D8. |
| `tasks.rerouted_from` (TEXT, nullable; mirrored on `archived_tasks`) | The profile the task was on before its first re-route that has not been undone. NULL means "where it was put". Partial index `idx_tasks_rerouted` on `(profile_id) WHERE rerouted_from IS NOT NULL` — it is what the trickle counts (D15). |
| `task_reroutes` (append-only) | `id`, `task_id` (FK, cascade; registered in `src/database/queries/task_references.py`), `project_id`, `from_profile_id`, `to_profile_id`, `from_provider`, `to_provider`, `intelligence_class`, `reason_code` (`provider_unavailable` \| `operator_forced` \| `operator_undo`), `provider_state`, `provider_generation`, `batch_id`, `actor`, `at`, `undone_at`. The full history; `rerouted_from` is only its cheap current projection. |
| `task_metadata['provider_pause']` | `{provider, state, generation, context}`, written **in the same transaction** as a provider-caused `PAUSED` transition and deleted on resume — the precedent is the `manual_pause` snapshot. It is how the sweep tells a provider pause from every other automatic pause, which today are distinguishable only by a transition `context` string that is not a column. |
| `task_metadata['provider_failover_handoff']` | D13's hand-off note. |
| `task_metadata['provider_intent_audit']` | `{by, at, previous}` for the last intent write. |
| Task comment | One per re-route and one per undo, system-authored: *"Re-routed from `standard-high-codex` to `standard-high-claude`: provider codex unauthenticated since 12:04 (batch `prb-…`)."* A plain comment, not `kind="progress"` — a re-route is not progress. |

`task show`, `GET /api/tasks/{id}` and the list projection gain `provider_intent`,
`rerouted_from`, `reroute` (the newest `task_reroutes` row plus `undoable`) and
the computed `provider_hold`. Model attribution is unchanged: what actually ran
is still `task_session_attempts.model`, never inferred from a profile.

`delete_profile` (`src/database/queries/profile_queries.py`) already nulls
`tasks.profile_id`; it must reset `provider_intent` to `class_only` and clear
`rerouted_from` where it named the deleted profile, in the same statement.

### D18 — a hold always says why

`aq task explain` gains one reason code, **`provider_hold`**, with `ref` = the
provider key, and the result gains a `provider_hold` object:

```json
{"provider": "codex", "vendor": "openai", "state": "unauthenticated",
 "since": 1789958640.0, "until": null, "kind": "provider_pinned",
 "ahead": null, "remediation": "run `codex login` on the host"}
```

`kind` is one of `provider_pinned`, `class_policy_hold`, `no_equivalent_rung`,
`no_available_target`, `awaiting_failover_capacity` (with `ahead`),
`reroute_limit_reached`, `all_providers_unavailable`, `failover_inactive` (mode
is not `enforce`, re-routing is off, or the playbook is not active).

It is produced as a **persistent reason from the task row and the availability
snapshot**, beside the gate and dependency reasons — not inside
`build_capacity_reasons`. Capacity reasons come from the push scheduler's last
tick and are stripped for pool-routed tasks (`_PUSH_ONLY_REASON_CODES`), which is
where most work lives. The `rate_limited` code goes away with
`provider_cooldowns`.

### D19 — events and notifications

**Bus events** (schemas in `src/event_schemas.py`; each is also written with
`db.log_event`, following `_emit_escalation`):

| Event | When | Payload beyond ids |
|---|---|---|
| `provider.state_changed` | effective state changes (D7) | see D7 |
| `task.rerouted` | per task moved, forced or undone | base task triple, `from_profile_id`, `to_profile_id`, `from_provider`, `to_provider`, `reason_code`, `batch_id`, `actor` |
| `provider.reroute_batch` | once per sweep that moved or newly held anything | `batch_id`, `provider`, `generation`, `moved`, `held` by kind, `targets` |
| `notify.provider_state` | with every change **between halves** | typed `NotifyEvent`, category `system`; this is what the dashboard toast and banner consume over the WebSocket |

A **batch** is one outage: `batch_id` is derived from `(provider, generation)`,
so every trickle top-up during the same outage appends to the same batch.

**Notifications are one idempotent command**, `provider_availability_notify`
(contracted for playbooks; excluded from MCP/CLI/API, like
`task_recovery_notify`), keyed by `(provider, generation)` so an event, its
replay and the five-minute timer never notify twice:

| Audience | Channel | When | Content |
|---|---|---|---|
| Global supervisor and the human | `message_send` to `session:supervisor-global` and `user:dashboard`, from `system/playbook:provider-failover` | a provider changes **half** (either direction) | state, reason, since, expected recovery, remediation, moved/held counts by kind, affected role profiles, and the exact commands: `aq provider status`, `aq provider reroute --dry-run`, `aq provider set-state` |
| Project supervisors | `message_send` to `session:supervisor-<pid>` | first sweep of a batch that moved or held a task **in that project** | that project's moved and held tasks by id. One per batch per project — never one per task, never one per top-up. |
| Discord, passively | the hourly digest | any half change or batch in the window | `collect_digest_activity` gains a producer over `provider_availability_transitions` and `task_reroutes`, emitting `WorkFact`s in the already-declared, producer-less `system` category with stable keys `provider:<key>:<generation>` / `reroute:<batch_id>`. A system fact makes a window eligible on its own: an outage in a quiet hour is exactly what the digest is for. |
| Discord, actively | a durable escalation (the `escalation_create` row, written by the daemon), `source_kind = "provider_availability"`, `source_identity = "<provider>:<generation>"`, `incident_key = "provider:<provider>:<generation>"` | **only when a human must act**: `unauthenticated`; `failing` for longer than `notify.escalate_failing_after_seconds`; or every provider unavailable per D15. Never for `exhausted` with a known `until` — there is nothing to decide. | severity `high` (`critical` when everything is down); `decision_requested` is the remediation. Filed under the project with the most affected tasks (ties by id), because escalations are project-scoped; with no affected task, none is filed. Resolved by the same command when the provider leaves that state. |

**As built (`bold-rapids.6`).** The escalation half lives in
`ProviderAvailabilityService.reconcile_escalations`
(`src/providers/availability_service.py`), run by
`provider_availability_notify` for its provider and by the service's own tick
— at once after any transition, otherwise once a minute, and not at all while
nothing is unavailable and nothing is open. Four details the table leaves open:

* **The incident key carries the generation.** `uq_escalations_incident` is
  unique per project for all time, so a bare `provider:<provider>` would refuse
  the provider's second outage in the same project forever. One outage is still
  one thread: an open incident is found by `source_kind` wherever it was filed
  and kept while the condition holds, even as the state moves inside the
  unavailable half (`unauthenticated` → `failing` is the same outage). A
  generation whose incident a human or the supervisor already closed is never
  filed again.
* **Affected tasks** are the queued (`READY`, unassigned) tasks routed to the
  provider — `affected()`, the same count the state-change message reports.
  `llm` strands no queued task, so it is never filed.
* **Every provider down** escalates each unavailable session provider whose
  own state would not (so `exhausted`-with-`until` does page, but only under
  D15's threshold), all at `critical`; open incidents' severity follows the
  fleet (`critical` while everything is down, `high` again after). `disabled`
  never escalates — an operator chose it.
* **Resolution** is `resolved`, not `stale`/`cancelled`: the recorded outcome is
  the provider's new state, carried as `terminal_outcome` and
  `terminal_evidence`. It goes through `resolve_escalation_on_recovery`, the
  second narrow path from an open state straight to `resolved` (the first is
  the Discord cutover's), which compares the revision *and* the source kind, so
  it can close only its own producer's incidents and loses to a concurrent
  human reply rather than overwriting it.

The digest half is `provider_fact` in `src/database/queries/digest_queries.py`:
a `system` fact of kind `provider` per half change, with an empty project and
task — a *fleet* fact, visible whatever `discord.digest.project_ids` selects
(an outage affects every project and is nobody's private work) and still
subject to the category filter. Its highlight wording is remembered against
its own key, so a provider failing the same way next week is still news.
`provider_failover.notify.digest: false` (or `mode: off`) leaves them out. The
`reroute:<batch_id>` facts arrive with `task_reroutes` in `bold-rapids.3`.

AQ's Discord surface is deliberately one digest plus one thread per human
decision, and this keeps to it. If the dead provider is the one the supervisor
itself runs on, the message sits undelivered and the existing
`SupervisorDeliveryWatchdog` (`src/escalations/supervisor.py`) raises its own
escalation after 15 minutes — a backstop this design relies on rather than
duplicates.

**Flap damping.** More than `notify.flap_threshold` half changes for one
provider inside `recovery.flap_window_seconds` produce one *"codex is flapping
(4 changes in 1 h)"* notice, after which per-change messages stop until the
provider has held one half for a full window. Events and the transitions table
are never damped — only messages.

### D20 — operator surfaces

**CLI** — a new `provider` tool category, so the commands land in `aq provider`
rather than the `system` fallback group (`provider_usage_probe` moves with them,
keeping `aq system provider-usage-probe` as an alias):

```text
aq provider status [<provider>] [--verbose] [--json]
aq provider history <provider> [--limit N]
aq provider recheck <provider>
aq provider set-state <provider> disabled|available|auto [--for D | --until TS | --no-expiry] --reason TEXT
aq provider reroute [--provider P] [--task ID ...] [--to-profile X] [--include-paused] [--dry-run] [--force]
aq provider reroute-undo (--batch B | --task ID ...) [--force]
```

`status` prints, per provider: key and vendor, effective state, reason, since,
expected recovery, override and its expiry, held and re-routed counts, last
launch success, and the newest account-wide usage reading. `--verbose` adds the
evidence ring and the last ten transitions. `--force` on `reroute` lets a human
or supervisor move a **pinned** task, or target a degraded provider; it records
`reason_code = operator_forced` and the actor, and leaves the intent as it was.
All of these are operator/supervisor commands; a task-scoped worker token gets
`out of scope`. A worker learns what it needs from its claim response.

**API** — `GET /api/providers/availability`, `POST
/api/providers/{provider}/state`, `POST /api/providers/{provider}/recheck`,
`POST /api/providers/reroute`, `POST /api/providers/reroute/undo`; task models
gain the D17 fields. `openapi.json` and both generated clients are regenerated
(`./scripts/regenerate-api-client.sh --offline`, then
`./scripts/regenerate-ts-client.sh --from-file`). `GET /api/providers/usage` is
unchanged; the dashboard joins the two on provider key.

**Dashboard** (`.5`):

* **Provider cards** (`dashboard/src/pages/metrics/ProviderUsage.tsx`) gain a
  header per provider: a state pill (green / amber / red / grey for `disabled`),
  the reason, *since*, a countdown to expected recovery, an override badge with
  its expiry, held and re-routed counts, and actions — *Disable for…*,
  *Recheck*, *Clear override*. The rule the cards already follow applies: a state
  is server-derived and never recomputed in the browser.
* **A banner** on every page while any provider is unavailable: *"Codex
  unavailable — logged out since 12:04 · 5 tasks moved to Claude · 4 held"*,
  linking to the card.
* **Task detail**: an intent chip (*Pinned* / *Preferred* / *Class only*),
  editable where routing is editable; *"re-routed from `standard-high-codex` ·
  undo"*; and the hold, rendered like `TaskAttention` with the `kind` in words.
* **The route picker** gets *Pin to this provider*, unchecked by default.
* **The Tasks tab** gets a *held by provider* filter.

### D21 — doctor

Namespace `providers.`, all **report-only**. Every useful fix here either
disrupts live sessions or papers over a stalled loop, which is the reasoning
`providers.claude_usage` already records for having none.

| Check | Severity |
|---|---|
| `providers.availability` | `OK` when every provider is `available`; `WARN` for any `degraded` or unavailable one, with reason, since, `until` and remediation; `ERROR` when every session provider is unavailable. `disabled` by override reports `INFO` — an intentional state never fails CI. |
| `providers.recovery_stuck` | `WARN` when a provider is past `until + reset_grace_seconds` by more than two orchestrator sweeps without entering probation, or is `unauthenticated` with no auth probe in 3 × `auth_probe_interval_seconds`. The recovery loop has stopped. |
| `providers.failover_playbook` | `WARN` when `mode: enforce` and `reroute.enabled`, but the `provider-failover` playbook is not active: tasks will hold and never move. |
| `providers.held_tasks` | `WARN` when any task has been held longer than `doctor.held_warn_seconds`, grouped by `kind`. |

`harness.binaries` and `providers.claude_usage` stay as they are.

## 7. Config surface

### D22 — one new section, hot-reloadable

```yaml
provider_failover:
  mode: enforce                  # off | observe | enforce
  order: []                      # failover target preference; [] = project default's provider, then WORKER_PROVIDERS order
  default_policy: same_class     # same_class | hold
  classes: {}                    # per-class override, e.g. {deep-high: hold}

  usage:
    exhausted_percent: 99        # account-wide window at/above this => exhausted
    degraded_percent: 85         # at/above this => degraded, and not a failover target
    hysteresis_percent: 2
  launch:
    strong_failures_to_trip: 2   # startup_dialog signals with no success between
    generic_failures_to_trip: 5  # unlabelled startup deaths, attributable only
    window_seconds: 600
    suspect_backoff_seconds: 30
  rate_limit:
    exits_to_trip: 2             # RATE_LIMIT exits from distinct sessions in the window
    cooldown_seconds: 900        # used only when the provider gave no resets_at
  auth_probe:
    interval_seconds: 600        # while launchable; 0 disables the background probe
    timeout_seconds: 10
  recovery:
    reset_grace_seconds: 60
    auth_probe_interval_seconds: 120
    failing_backoff_seconds: 300
    backoff_max_seconds: 3600
    flap_window_seconds: 3600
  override:
    default_ttl_seconds: 14400   # 4 h
    max_ttl_seconds: 604800      # 7 d
  reroute:
    enabled: true
    max_per_sweep: 10
    target_backlog_factor: 1.0   # x the target rung's max_active
    allow_degraded_target: false
    max_priority_value: null     # null = every priority may move
    task_cooldown_seconds: 1800
    max_auto_per_task: 2
  notify:
    supervisor: true
    digest: true
    escalate_unauthenticated: true
    escalate_failing_after_seconds: 1800
    escalate_all_down_after_seconds: 1800
    flap_threshold: 3
  doctor:
    held_warn_seconds: 14400
  evidence:
    keep: 20

llm:
  fallback: null                 # optional second credential block (D13a); restart-required like the rest of llm:
```

* **`mode`** — `off` does nothing. `observe` runs evidence, state, events,
  surfaces and `provider_reroute` in plan-only form, but suppresses no launch
  and moves no task; it is how an operator watches the detector on their own box
  before trusting it, and how `.2` can land ahead of `.3`. `enforce` is the
  default: availability tracking is a safety feature (it ends the retry storm),
  it changes nothing on a box whose providers are healthy, and the operator
  asked for re-routing in so many words.
* `src/config.py`: a `ProviderFailoverConfig` dataclass tree with `validate()`
  bounds in the house style — percentages in `(0, 100]` with `degraded_percent <
  exhausted_percent`; every count `≥ 1`; every duration `≥ 0`;
  `default_ttl_seconds ≤ max_ttl_seconds`; `order` and `classes` keys checked
  against known providers and classes as **warnings**, because both are vault
  content that may load later. Added to the hot-reload set beside `providers` and
  `swarm`; one row in `docs/reference/configuration.md`.
* **No `TuningNote`s.** `tests/test_config_tuning.py` demands a note for exactly
  the keys `recommended_tuning` emits, and none of these depends on cores or RAM,
  so none is emitted.
* Removed: `pause_retry:` (D7).

## 8. Test plan

### D23 — a fake provider that can be exhausted on command

Everything below runs with no LLM and no real CLI.

**The fake provider kit** (`tests/fixtures/provider_failover/`, reused by unit,
integration and end-to-end tiers):

* Two harness files, `prova.md` and `provb.md`, each with a `login-required`
  (`signal: auth`) and a `usage-limit` (`signal: usage`) quarantine dialog.
* Intelligence classes `std-high` with slices for both harness ids and
  **`solo-high` with a slice for `prova` only** — the Astra analogue. The
  id-keyed slice fallback added on 2026-09-20 is what makes a vendorless fake
  harness resolvable.
* Hand-authored pool profiles `std-high-prova`, `std-high-provb`,
  `solo-high-prova`. `WORKER_PROVIDERS` is a fixed tuple, so the kit authors
  profiles rather than deriving rungs; `worker_route` recognises them as
  equivalents, which also tests that operator-authored profiles fail over.
* `FakeProvider` (`src/sessions/fake.py`) gains `script_startup_dialog(harness,
  dialog)` — raising `SessionDiedDuringStartup` with a structured `detail` — and
  `script_rate_limit_exit(name, after_s)`, leaving a pane capture that matches
  `RATE_LIMIT_PATTERNS`. For the end-to-end tier a test-only
  `sessions.fake.script_file` points at a JSON file mapping each harness to a
  mode — `ok | login_required | usage_limit | rate_limit_midtask | crash` —
  re-read on every start, so a shell script can exhaust and restore a provider
  mid-run.
* A fake login injected through `provider_logins(*additional)` whose
  `status_command` reads the same file, and a helper that writes
  `ProviderUsageSnapshot(provider="prova", used_percent=100, resets_at=now+120)`
  through `record_provider_usage`.

**Pure reducer** (`tests/test_provider_availability.py`, fake clock, table-driven):
every row of D3 and D4; one flaky launch ⇒ `degraded` only; one stray `429` ⇒
`degraded` only; corroboration by probe; `cannot_tell` is never evidence; stale
snapshots never trip; a model-scoped window never trips; attribution (one project
and no other provider ⇒ `degraded`); probation admits one launch, re-trips on one
failure with `level + 1`, completes on one success; backoff doubling and its cap;
level decay; override precedence, expiry, `auto` resetting counters; percent
hysteresis; `generation` increments only on effective change.

**Collectors and mechanism** (`tests/test_provider_evidence.py`,
`test_provider_suppression.py`): the dialog `signal` field and the name-map
fallback; `_fail_session_launch` receives structured fields; only startup deaths
are `launch_failure`; **the 2026-09-20 replay** — `login-required` on every
`prova` launch marks it `unauthenticated` within two launches and the count of
`startup_exit` session rows then stops growing; attributed startup deaths arm no
key quarantine and bump no `sessions.restarts`; pools size to zero and idle
sessions drain while busy ones are left alone; the push scheduler skips the
provider; state survives a daemon restart; the auth probe never blocks the loop.

**Intent and migration** (`tests/test_provider_intent.py`, on PostgreSQL): every
row of D9; pin refusal for a worker token and for an inline graph; a vault
formula pin honoured; `task_route` never downgrades; the migration is idempotent,
yields `preferred` for every non-NULL `profile_id` and **never `pinned`**; named
constraints; `archived_tasks` mirrored; `delete_profile` resets intent.

**Re-route engine** (`tests/test_provider_reroute.py`): with `prova` unavailable
— a `preferred` `std-high-prova` task moves to `std-high-provb`; a `pinned` one
holds with `provider_pinned`; a `solo-high` task holds with
`no_equivalent_rung`; `classes: {std-high: hold}` holds; a degraded target is
refused; the trickle keeps at most `target_backlog` moved-and-queued per rung and
tops up in priority order; `max_per_sweep`, `task_cooldown_seconds`,
`max_auto_per_task`; **property test: after any sequence of sweeps, running
sessions per profile ≤ `max_active` and fleet-wide ≤ `global_max_active`**;
provider-paused tasks move and resume, legacy pauses are untouched without
`--include-paused`; the availability-aware project default is derived and never
persisted; undo restores the route and refuses a running task; force moves a pin
and records the actor; every provider down ⇒ nothing moves, claims answer
`not_admissible`; recovery releases holds, leaves moved tasks, routes new work
home. A guard asserts the claim SQL references no provider table, and the existing
claim-frontier perf test must still pass unchanged.

**In-flight** (`tests/test_provider_inflight.py`, `.4`): a startup death on a
usage dialog leaves `retry_count` unchanged and resumes on the other provider; a
mid-task `RATE_LIMIT` exit produces the WIP commit, the push and the hand-off
note, and the next worker sees it in `aq prime`; a failed push holds in place
with the workspace kept; pool session drain; all providers down.

**Surfaces**: command contracts for `provider_status`, `provider_set_state`,
`provider_recheck`, `provider_reroute`, `provider_reroute_undo`,
`provider_availability_notify`; the reviewed `provider-failover` bundle
(`scripts/rebuild-reviewed-playbook-artifacts.py`, then the manifest digests);
notification idempotency across event + replay + timer, flap damping, escalation
create and auto-resolve; digest facts; the four doctor checks; config validation
bounds; the API contract test after regenerating `openapi.json`; dashboard unit
tests for the card header, banner, intent chip and undo; direct-path `llm_call`
classification, fail-fast, and `llm.fallback`.

**End to end** (`.5`; a `provider-failover` scenario in `scripts/e2e-smoke.sh`,
tier 1): queue three `preferred`, one `pinned`, one `solo-high` and two
`class_only` tasks; set `prova` to `login_required`; assert state within two
launches, no further launches, moves inside `provb`'s `max_active: 1`, holds with
their kinds, one supervisor message, one escalation, `aq provider status`; restore
`prova` and `aq provider recheck` ⇒ probation ⇒ `available` after one launch; held
tasks run on `prova`, moved-and-queued tasks stay on `provb`, `reroute-undo`
returns one; then both providers down ⇒ holds, `not_admissible`, a critical
escalation, no moves. The transcript is attached to `.5`. Every wall-clock
assertion takes `perf_strict`.

## 9. Non-goals and known limits

* **Cross-class substitution.** Never automatic (D12).
* **Model-scoped exhaustion.** A Claude `week`/`Opus` window at 100 % marks the
  provider `degraded` and is not acted on; per-model availability would need a
  structured scope-to-class mapping nobody has (D3).
* **Role profiles** (`supervisor`, `triage`, `reviewer`, …) hold; they do not
  switch harness. A `fallback_harnesses` list on a role profile, applied at
  launch and recorded on the attempt, is the natural follow-up.
* **Per-project provider order** and per-project fallback maps. One global
  policy; a project-scope copy of the playbook is the escape hatch.
* **Predictive failover** — moving work because a window *will* run out. `degraded`
  stops new failover traffic arriving; it does not move native work.
* **Several accounts per provider.** One login per harness. `provider_key()` is
  the seam (D0).
* **Load balancing.** `class_only` placement prefers the project default's
  provider and merely avoids unavailable ones; it does not spread load.
* **`agent_task` waits.** `ChildTaskCompleted` has no producer in `src/` today, so
  an `agent_task` wait ends only at its step deadline whatever this design does to
  the child. No shipped playbook uses the step; recorded because D13 depends on
  it for nothing, and nobody should conclude otherwise.

## 10. Phasing

| Task | Ships | Decisions |
|---|---|---|
| `bold-rapids.2` | Tables, reducer, collectors, snapshot, launch suppression, the derived hold and its explain reason, `provider_status` / `set_state` / `recheck`, API read, the four doctor checks, `provider.state_changed`, `provider_availability_notify` for state changes, config, removal of `provider_cooldowns` and `pause_retry:`. Can run in `observe` first. | D0–D7, D11 (mechanism), D18, D19 (state half), D21, D22 |
| `bold-rapids.6` | D19's Discord half, split out of `.2`: the provider outage escalation (file, keep severity current, resolve on recovery) and the digest's provider facts. No schema change. | D19 (Discord half) |
| `bold-rapids.3` | `provider_intent` and its migration, every D9 surface, `provider_reroute` / `reroute-undo`, the playbook and its reviewed bundle, catalog filter and the `held` outcome, the availability-aware project default, `task_reroutes`, batch notifications. | D8–D10, D11 (policy), D12–D17, D19 (batch half) |
| `bold-rapids.4` | Structured launch-failure fields, provider-attributed failure accounting, `provider_pause`, WIP checkpoint and hand-off note, pool drain, no key quarantine for attributed deaths. | D2 (collector inputs), D13 (in-flight rows) |
| `bold-rapids.8` | The stall ladder's usage-limit screen: a live CLI parked on its limit is taken out as a `RATE_LIMIT` exit instead of being nudged. | D13 (parked-session row) |
| `bold-rapids.5` | Dashboard, operator runbook, concept and reference pages, supervisor guidance on when to pin, end-to-end scenario and transcript. | D20, D23 (end to end) |

### The epic's acceptance criteria, mapped

| Criterion | Where it is decided |
|---|---|
| The 2026-09-20 incident marks the provider unavailable "within the specified number of failures" and stops further launches | D3 — **two launches**; D11 mechanism 1 |
| State, reason, since and expected recovery visible via CLI, API and doctor | D7, D20, D21 |
| A `preferred` `standard-high-codex` task moves to `standard-high-claude`; a pinned one holds with a visible reason; an `astra-high` task holds; nothing exceeds pool maxima | D12, D13, D14, D18 |
| Every automatic re-route recorded and reversible; routing options exclude the unavailable provider | D17, D16, D11 mechanism 3 |
| A task whose session dies on a usage-limit dialog resumes on another provider with its branch intact and retry budget unchanged | D13 |
| The pin migration | D10 — `preferred`, never `pinned` |
| The Astra / single-provider case | D12 — holds by construction; D10 — survives the migration |
