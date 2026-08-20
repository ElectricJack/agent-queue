---
tags: [analysis, design, extensibility, platform, ecosystem]
date: 2026-08-20
status: opinionated design — written to be argued with, then turned into specs
related: "[[ecosystem-positioning]], [[../specs/design/aq-surface]], [[../specs/design/work-graph]], [[../specs/design/trust-and-ops]], [[../specs/plugin-system]], [[../specs/design/guiding-design-principles]]"
---

# Extensibility Architecture — agent-queue as the Thing Others Plug Into

**The question.** In eighteen months there will be many more tools in this space — the
Agent Flywheel alone is thirty-nine of them, written by one person, in nine months. What
makes agent-queue the thing those tools *compose with* rather than the thing they route
around? This document answers that with a specific architecture, grounded in what is
actually in the tree today, and it protects the one thing
[[ecosystem-positioning]] established is the product: **the readiness predicate — "what
should run next, and why isn't it running?" — evaluated across dependencies, gates,
workspace locks, caps and budget at once, and then acted on without a human.**

---

## 1. The core insight

agent-queue should not become a plugin host. It should become a **ledger of typed facts
about work, with a closed evaluator and an open perimeter.** The readiness predicate is
only valuable because every fact it joins — edges, gates, locks, caps, budget, session
liveness — lives in one schema with one meaning, and because every verdict it produces can
be narrated (`aq task explain`). Both properties die the moment the *evaluation* is
pluggable, and both properties are completely indifferent to *who supplies the facts* and
*who observes the verdicts*. So the extensibility model is: **facts in, events out,
decisions stay home.** External tools participate by contributing typed facts through the
command surface (create tasks, declare dependencies, resolve gates, report costs, close
work) and by observing a durable, replayable event stream — never by replacing the
scheduler, the blocked-state predicate, or the completion protocol. The system already
has three-quarters of the machinery for this model and calls it by other names: the
CommandHandler is the fact-intake, the event schema registry is the observation contract,
the vault is the type-definition language, and gates — specifically the `event` gate type
— are the rendezvous point where an external tool's judgment becomes a scheduling fact
without the external tool ever touching the scheduler. The work is not to invent an
extension system; it is to finish, harden, and *publish as contracts* the seams that
exist, and to say no to everything else.

Three corollaries that drive everything below:

1. **Extension points are typed fact channels, not behavior hooks.** A Rust CI watcher
   doesn't hook `pre_schedule`; it resolves a `ci-run` gate. A cost tracker doesn't wrap
   the runtime; it writes token-ledger facts. A safety scanner doesn't intercept
   dispatch; it raises a `human` gate. Every one of those leaves the readiness predicate
   able to explain itself, because the external tool's contribution *is a row with a
   type*, not a mutation of control flow.
2. **The vault defines new *instances* of core-implemented types; code defines new
   *types* of behavior.** A harness markdown file can integrate a brand-new CLI agent
   because "launch an argv, watch a pane, dismiss these dialogs" is behavior core already
   implements. That's the generalizable pattern — and also its hard limit (§3.4).
3. **Out-of-process is the default tier, in-process Python is the privileged exception.**
   Today it's backwards: the polished path is a Python `Plugin` subclass, and the
   out-of-process story is "you can call MCP." Flip it. Most of the flywheel is Rust and
   Go; none of it will ever subclass `src.plugins.base.Plugin`, and it shouldn't have to.

---

## 2. The extension-point taxonomy

Four rings, from closed to open. The rule for placement: **if swapping a component could
make two parts of the system disagree about whether a task is runnable, or make a verdict
unexplainable, it is closed.** If it only changes where facts come from or where they go,
it is open.

### 2.1 Ring 0 — Closed. Not pluggable, ever.

| Component | Why pluggability destroys a guarantee |
|---|---|
| **The blocked-state predicate** (`src/database/queries/blocked_state.py`) | This is the product. A pluggable predicate means two installs disagree about what "ready" means, `explain` can't narrate third-party logic, and the test-enforced invariant that the projection matches the definition becomes unenforceable. |
| **The scheduler's admission decision** (`src/scheduler.py` + the planned write-time cap invariant, ecosystem-positioning §5.1) | Admission is becoming an invariant precisely because a second writer (the supervisor) is coming. An invariant cannot live in another process, and it certainly cannot live in someone else's process. Ordering *policy inputs* are open (§2.3); the admission check is not. |
| **The completion protocol** — `task close --outcome/--failure-class/--work-outcome`, the exit classifier, drain-ack | "Process exit carries no information" is the day-one differentiator. If a plugin can redefine what "done" means, retry policy, reflection, and the whole correctness posture are gone. |
| **The task state machine + dependency taxonomy** (`src/state_machine.py`, the 8 CHECK-constrained edge types) | The `br` convergence (ecosystem-positioning §4.6) says the taxonomy is commodity — which is exactly why it must be fixed: it's the shared vocabulary. Extensible edge types would be a private dialect per install. Escape hatch: `task_metadata`, which is deliberately schemaless. |
| **Workspace acquisition order + merge slot lease protocol** | All-or-nothing acquisition in canonical lock order is a deadlock-freedom proof. A plugin participating in lock acquisition is a distributed transaction with no coordinator. |
| **The schema and its single Alembic chain** | One schema is what makes the cross-domain join cheap (ecosystem-positioning §1). Plugins get `plugin_data` and `task_metadata`, never tables in the core chain. |
| **The event envelope + schema registry semantics** (`src/event_schemas.py`) | The registry is the observation contract (§4). Third parties may *add* namespaced event types; nobody may change the meaning or required fields of an existing one. |
| **CommandHandler as the single entry point** | Every surface being a projection of one handler is why behavior is identical across CLI/MCP/REST/Discord. A second entry path is where "every install behaves differently" starts. |

Naming what's closed is the identity statement. A system where the scheduler, the
blocked-state predicate, and the completion protocol are swappable is not a platform; it
is a bag of parts, and nobody builds on a bag of parts because a bag of parts guarantees
nothing.

### 2.2 Ring 1 — ABC-pluggable (in-process, code, small and finished)

These exist and are shaped correctly. Leave them alone:

- **`Runtime`** (`src/runtimes/base.py`) — start/wait/stop/is_alive + `Capability`
  ClassVar. Already has three implementations plus ACPX fanning out to 14+ ACP agents.
  Correct. Note that ACPX plus harness markdown (§2.3) means "add a new agent platform"
  almost never requires a new Runtime subclass — that's the sign the seam is at the right
  altitude.
- **`SessionProvider`** (`src/sessions/provider.py`) — the best interface in the
  codebase: `Cap` gating instead of name-switching, typed errors that encode hard-won
  operational lessons (`PartialListError`: unknown is not dead; `NotSubmitted`: never
  assume delivery). This is the template every future ABC should copy.
- **`DatabaseBackend`** protocol, **ChatProvider**, **messaging adapters** — fine as-is.
- **`DoctorRegistry`** — plugin checks are namespaced `plugin.<name>.*` and can never
  shadow core checks. Correct precedence model; keep it.
- **Plugin service protocols** (`src/plugins/services.py`, e.g. memory) — the right way
  for a plugin to *offer* a capability core consumes, with `None` as a first-class
  answer.

What should be *added* to this ring — exactly one thing:

- **`TranscriptReader`** (per-harness, planned with session-runtime S3). It belongs here
  because parsing `~/.claude/projects/*.jsonl` vs Codex vs Gemini formats is genuinely
  polymorphic behavior over local files, needs process-local performance, and produces
  facts (token splits, activity) the core writes. It should be selected by the harness
  markdown (`transcript_paths` already lives there), implemented in Python.

What should *not* become an ABC, though each is tempting:

- **Schedulers** — closed (Ring 0). What's open is *policy input*: `touches:` hints,
  priority, labels, and (future) vault-tunable weights. The deficit/ordering function can
  read knobs; it cannot be replaced.
- **Task sources** (Jira/GitHub/Linear sync) — these are just clients. `create_task` +
  labels + `task_metadata` over the wire contract (§3) is the whole integration. An ABC
  would drag other people's domain models into the daemon for zero gain.
- **Verifiers** — not a component type at all; a verifier is anything that resolves a
  gate (§3.3). Making it an ABC would put third-party judgment inside the process whose
  job is to survive third-party failure.
- **Cost writers** — `token_ledger` is core-owned (one join, one answer). External cost
  *sources* report through a command; they don't own the table.
- **Memory backends** — already a plugin service protocol; the FTS5-first decision
  (ecosystem-positioning §3.1) makes the default in-tree anyway. Don't widen it.

### 2.3 Ring 2 — Vault-defined (markdown; config, not code)

This is the actual extensibility model for most people, generalized. The harness registry
(`src/sessions/harness_registry.py` + `vault/harnesses/<name>.md`) is the proof: a new
CLI coding agent is integrated by writing one markdown file — argv, prompt mode, dialog
dismissal rules, transcript globs, hook file templates — read live by the vault watcher,
project-scope shadowing system-scope, parse failures keeping the last good version. No
release, no Python, no fork. Workspace kinds, MCP server definitions, profiles, and
playbooks follow the same pattern.

**The generalization rule: a vault file may (a) declare data, (b) parameterize behavior
core implements, and (c) *select* among behaviors core implements. It may never introduce
behavior.** Markdown-as-plugin ends exactly where a Turing-complete decision is needed —
at that point you are either an LLM decision inside a playbook node (structure guides,
intelligence decides) or you are code in Ring 1/3.

What else should become vault-defined, in priority order:

1. **Event subscriptions** — `vault/subscriptions/<name>.md` (system) and
   `vault/projects/<pid>/subscriptions/<name>.md`: event-type patterns, delivery mode
   (webhook URL | pull-only), filter (project, severity), token binding. This is the
   registration half of the outbound story (§4). It's a perfect vault citizen: pure
   declaration, human-auditable ("what is watching my system?" is answerable with `ls`),
   and hot-reloadable.
2. **Gate definitions for the `event` gate type** — a named gate template: which event
   type resolves it, timeout, on-timeout behavior. Lets an operator wire "block until the
   nightly-benchmark tool emits `bench.passed`" without code.
3. **Doctor checks, command-shaped** — a vault check that runs an allowlisted `aq`
   command (or an operator-authored script path) and maps exit code to OK/WARN/FAIL.
   This is how a Go binary contributes to `aq doctor` without being a Python plugin.
   Namespaced `vault.<name>.*`, same shadowing rules as everything else.
4. **Credential profiles** (already recommended, ecosystem-positioning §4.4) and
   **scheduling policy knobs** (weights for the deficit function, anti-affinity strength)
   — parameters, not policies.

What should **not** be vault-defined: anything in Ring 0; retry policy semantics;
event schemas (they're a code-owned contract with a test invariant — a vault-editable
schema is a self-modifying contract); and command definitions (same reason).

### 2.4 Ring 3 — Wire-extensible (out-of-process, any language; the default tier)

The headline: **a first-class participant is anything that can speak two HTTP endpoints.**

- **Inbound:** `POST /api/execute` — the versioned JSON envelope from
  [[../specs/design/aq-surface]] §4.1, with a scoped bearer token (§3.2 below).
- **Outbound:** `GET /api/events?after_id=…` — durable, cursor-paged, replayable event
  feed (§4), plus optional webhook push.

Everything a Rust binary, a Go daemon, or a hosted service needs is those two, plus a
discovery endpoint (`/api/schema`) and a place to register (`vault/subscriptions/`).
MCP remains available as a *transport convenience* for LLM-agent clients (it already
exists in both directions), but MCP is not the contract — the command envelope and the
event schemas are, and MCP is one projection of them, exactly as the CLI is.

This ring is where the flywheel-shaped world plugs in, and it's worth being concrete
about what each category of tool becomes (this is the "agent-queue as substrate" story):

| External tool shape | How it participates | What it never touches |
|---|---|---|
| CI / test watcher | Resolves `ci-run` gates; emits namespaced events | Scheduler |
| Safety/destructive-command scanner | Raises `human` gates on tasks; annotates `task_metadata` | Dispatch path, harness hooks |
| Cost/usage tracker | Reads `token.recorded` events; or writes cost facts via command | `token_ledger` internals |
| Issue tracker sync | Creates/closes tasks with labels + metadata; subscribes to `task.closed` | Task state machine |
| Dashboard / TUI / mobile client | Events feed + read commands | Everything |
| Another orchestrator (supervisor-of-supervisors) | Creates task graphs, watches events, resolves its own `event` gates | Admission, completion |
| A new coding agent | A harness markdown file (Ring 2) — no wire code at all | — |

Note what this table implies: **agent-queue's substrate story is "we are the system of
record for work state, and we act on it; you are everything else."** Other people's
agents build *on* it by treating tasks, gates, and events as the API — the same way tools
build on a database or a message bus, except this one schedules.

---

## 3. The out-of-process contract, concretely

### 3.1 The command half (inbound)

Already 90% designed in [[../specs/design/aq-surface]]; this section adds the
integration-specific deltas.

- **Envelope:** the §4.1 versioned JSON envelope, unchanged. `schema_version` integer,
  additive payload evolution, typed error codes.
- **Command subset:** integrations get a **stable command tier** (§5) — a designated
  subset of the ~140 commands whose names and argument shapes are frozen-additive. The
  agent surface (task show/set/close/heartbeat/ask, message *, gate resolve, create_task,
  get_schema, record-cost) is the seed. Everything outside the tier is explicitly
  "internal — may change without notice."
- **Idempotency:** integrations retry (because §4 delivery is at-least-once), so
  mutating stable-tier commands accept an optional `idempotency_key`; the handler stores
  `(key → result)` in a bounded table and replays the stored result on duplicates. This
  is the one genuinely new mechanism the inbound path needs; without it, "at-least-once
  out, retry in" mints duplicate tasks.

### 3.2 Identity and scope

Ride the Wave 3 auth work (aq-surface S2/S3), don't invent a second system. The
`api_session_tokens` table and scope model already planned for per-session tokens
generalize with one addition: a **token kind**. Session tokens scope to
`(session_id, task_id, project_id)` and narrow to the agent surface. **Integration
tokens** scope to `(integration_name, project_id | *)` and narrow to the stable tier plus
the event feed, minted by the operator (`aq integration token <name>`), hash-stored,
revocable, TTL-optional. The subscription markdown (Ring 2) names which token it binds
to. Same middleware, same 401/403 semantics, same "tokens only ever narrow" principle.

### 3.3 Gates are the participation primitive

This is the single most load-bearing decision in the document, so it gets its own
subsection. The gate table already models typed blocking waits — `human`, `timer`,
`pr-merged`, `ci-run`, `event`, `task` — and the blocked predicate already reads it.
When WG-3 (gates sweep + commands, Wave 3) lands, the external-tool story falls out
almost for free:

- An external verifier is: subscribe to `task.gated` events (or poll `gate list
  --pending`), do arbitrary work in any language on any host, then
  `POST /api/execute {"command": "resolve_gate", "args": {"gate_id": …, "resolution":
  "approve", "note": …}}` with an integration token.
- The scheduler never knows the verifier exists. The readiness predicate sees a gate row
  flip. `aq task explain` says "was waiting on gate ci-run#42, resolved by
  integration:bench-runner at 03:12" — **the external tool's participation is natively
  narratable** because it is a fact, not a hook.
- Failure isolation is automatic: a dead verifier is a gate that times out (gate
  templates in Ring 2 declare the timeout and on-timeout behavior — escalate to `human`
  or fail the gate), not a wedged daemon.

Contrast with the alternative everyone reaches for — a `pre_dispatch` /
`on_task_complete` hook API. Hooks put third-party code on the critical path, make every
install behave differently, can't be explained by `explain`, and turn "why isn't it
running" into "because some webhook hasn't returned." Gates invert all four properties.
**agent-queue should never ship a synchronous extension hook on the scheduling path.**

### 3.4 What about tools that want to *be* the runtime?

Already answered by existing seams, in order of preference: (1) a harness markdown file
if the tool is a CLI agent (Ring 2 — zero code); (2) ACP if it speaks ACP (ACPXRuntime
fans out already); (3) an MCP server registration if it's a tool-provider for agents
(`src/profiles/mcp_registry.py` — also markdown); (4) a Python `Runtime` subclass only
if it is none of those. The taxonomy means "we support your agent" is almost always a
config statement, which is precisely the property that made the flywheel prolific —
composition through files — grafted onto a system that can also schedule and explain.

---

## 4. The outbound story — durable events, replay, backpressure

This is the weakest area today, and the honest inventory is: the EventBus
(`src/event_bus.py`) is in-memory, fire-and-forget, schema-validated at emit; the
WebSocket (`src/api/websocket.py`) forwards `notify.*`/`message.*` live with a
1000-deep drop-oldest queue and **no cursor, no resume, no durability**; the `events`
table (`src/database/tables.py:269`, autoincrement `id`, ad-hoc `log_event` writers)
is a durable audit log that most bus events never reach. A tool that disconnects for
ten seconds silently loses events. That is disqualifying for a platform, and fixing it
is cheap because the substrate already exists.

**The design: one durable spine, three delivery modes, consumer-paced.**

1. **Durable spine.** The EventBus gains a persistence sink: every emitted event whose
   type is in the schema registry is also written to the `events` table (async, batched,
   off the emit hot path — a bounded queue flushed by one writer task). The
   autoincrement `id` becomes the global cursor. Retention is config
   (`events.retention_days`, default 30) with a cascade-step sweep. The existing
   test-enforced invariant "every emitted type has a schema" now also means "every
   emitted type is durable" — one rule, no per-event opt-in to forget.
2. **Pull (the primary mode).** `GET /api/events?after_id=<cursor>&types=task.*,gate.*
   &project=<pid>&limit=500` — strictly ordered by `id`, consumer stores its own cursor,
   backpressure is inherent (you fetch when you're ready), replay is inherent (rewind
   your cursor). This is the mode every serious integration should use; it is ~40 lines
   on top of `get_recent_events`, which already supports the filters.
3. **Push, WebSocket/SSE with resume.** The existing WS grows `?after_id=` — on connect,
   page history from the table, then splice into the live feed (dedupe on id at the
   splice point). The dashboard and `aq session logs -f` (session-runtime S3's SSE plan)
   should use this same cursor pattern rather than inventing a second one.
4. **Push, webhooks (convenience tier).** For tools that can't hold a connection: the
   subscription markdown declares a URL; a delivery worker per subscription reads the
   spine from that subscription's stored cursor (a `subscription_cursors` row — the
   *daemon* keeps the cursor here, unlike pull), POSTs batches with
   `{delivery_id, events: […]}`, advances the cursor on 2xx, retries with exponential
   backoff on failure, and after N failures parks the subscription and raises a doctor
   finding (`subscriptions.parked`) rather than dropping events. **At-least-once,
   ordered per subscription, never silently lossy.** Consumers dedupe on event `id`.

Two contract statements that keep this honest:

- **Delivery guarantee:** at-least-once within retention, strictly ordered per
  subscription, replayable from any retained cursor. Never exactly-once (don't promise
  it, don't build for it — dedupe on id is the consumer's one obligation).
- **The feed is facts, not commands.** Events describe what happened; nothing about
  consuming an event authorizes an action. (The inverse rule — no event consumer inside
  core may treat payloads as instructions — already falls out of the schema registry.)

This also quietly future-proofs multi-host: a second machine's daemon consuming the
spine over HTTP with a cursor is the same contract as a Rust dashboard doing it. Nothing
here needs Postgres LISTEN/NOTIFY or a broker today, and nothing forecloses adding one
as an *optimization* behind the same endpoints later (principle #9: the interface is
"give me events after cursor X"; how they move is implementation).

---

## 5. Stability contracts and versioning

What third parties may depend on, in order of hardness:

| Contract | Guarantee | Change policy |
|---|---|---|
| **JSON envelope** (`schema_version`) | Frozen | New integer only on breaking envelope change; payloads evolve additively without a bump. Already specified — adopt as-is. |
| **Event types + required fields** (schema registry) | Additive-only | Required fields never removed, renamed, or re-typed; new fields land as `optional`; event types are never repurposed; removal requires a deprecation window ≥ 2 minor versions with a `deprecated` marker in the catalog. |
| **Stable command tier** (names + arg shapes) | Additive-only | New optional args fine; renames ship as aliases for the window; removal per the same deprecation policy. Commands outside the tier: no promises, stated loudly. |
| **Enums** (`aq schema` / `get_schema`) | Additive-only | New enum values may appear (consumers must tolerate unknown values — put this sentence in the docs in bold); existing values never change meaning. |
| **The cursor** (`events.id`) | Monotonic per install | Never reused; retention bounds replay depth; a restore-from-backup that rewinds cursors must bump an `install_epoch` field in `/api/schema` so consumers detect it. |
| Internal Python APIs, DB schema, vault file internals, tool definitions | **None** | Explicitly unstable. The DB is a projection/implementation detail; anyone querying SQLite directly is off-contract by definition. |

**Mechanism, not vibes:** the schema registry and the stable tier get committed baseline
snapshots (JSON files in `docs/contracts/`), and a test diffs the live registry against
the baseline — removals and type-changes fail CI, additions update the baseline in the
same PR. This is the same enforcement pattern as the existing "every emitted type has a
schema" invariant test, pointed at compatibility instead of coverage. `/api/schema`
serves the whole catalog (commands in tier, event types, enums, envelope version,
`install_epoch`, daemon version) so a participant's capability handshake is one GET
at startup: check `min_aq_api`, enumerate what exists, degrade or refuse loudly.

Versioning identity: one daemon version (SemVer). "Breaking" is defined *only* against
the table above — internal refactors, schema migrations, even command-surface changes
outside the tier are minor. This keeps the promise small enough to actually keep.

---

## 6. Migration path from today's tree

Ordered by (value ÷ cost), with honest cost labels. Nothing here should be scheduled
ahead of the ecosystem-positioning §8 verdict — turn the dark flags on, fill the empty
mixins, get a real session running — because a platform whose core doesn't run attracts
no participants. But several stages below are *the same work* as Wave 3, done with the
contract in mind, which is the point.

**Stage 0 — Declare (cheap; documentation + tests, no runtime changes).**
Write `docs/contracts/`: the stable command tier list, the event catalog snapshot, the
enum catalog, the change policy from §5. Add the baseline-diff test. Add `/api/schema`
(it's a projection of data that already exists in `src/event_schemas.py`,
`src/tools/definitions.py`, and the enums). This stage costs days and is what makes
everything after it a promise rather than an accident.

**Stage 1 — The durable spine (cheap-to-moderate; the single highest-value runtime
change in this document).**
EventBus persistence sink → `events` table (batched writer task); `GET /api/events`
cursor pull; `?after_id=` resume on the existing WebSocket; retention sweep. Table,
filters, and autoincrement cursor already exist. Session-runtime S3's transcript/SSE
work should be built *on* this cursor pattern, not beside it — that's a coordination
note for Wave 3, not new scope.

**Stage 2 — Identity for non-sessions (moderate; rides Wave 3 auth, aq-surface S2/S3).**
Add the integration token kind to the planned token store + middleware; `aq integration`
CLI verbs; scope enforcement = stable tier + events feed. Deliberately do this *inside*
the S2/S3 implementation rather than after it — retrofitting a second principal type
onto a session-only token store is the expensive version of the same work.

**Stage 3 — Gates as the participation primitive (mostly already scheduled).**
WG-3 (gate sweep + create/resolve commands) is planned Wave 3 work and is currently the
gap between this document and reality — gates are schema plus one read clause today.
The extensibility deltas on top of WG-3 are small: `resolve_gate` in the stable tier,
resolution attribution (`resolved_by: integration:<name>`) in the gate row and the
explain output, and `task.gated` / `gate.resolved` events in the registry. Plus the
Ring 2 gate templates (vault) with timeout/on-timeout.

**Stage 4 — Webhook delivery + subscription vault kind (moderate; genuinely new).**
`vault/subscriptions/*.md` parsing (clone the harness-registry pattern —
`src/sessions/harness_registry.py` is the template, ~300 lines), `subscription_cursors`
table (one Alembic migration), the delivery worker (reuse the cascade/timer machinery
for retries), `subscriptions.parked` doctor check. This is the only stage that adds a
new moving part, and it's a worker loop, not a datastore.

**Stage 5 — Idempotency keys + vault doctor checks + policy knobs (cheap, incremental).**
Each is small and independent; do them on demand, driven by the first real external
integration rather than speculatively.

**Deliberately not on the path:** plugin sandboxing (WASM, subprocess plugins) — the
trust answer for untrusted code is "run it out of process behind Ring 3," which needs no
sandbox because it already has a permission boundary: the token. A plugin marketplace.
Remote runtimes. Multi-host schedulers. A stable *Python* API (the wire contract is the
contract; freezing `PluginContext` for third parties would freeze internals we still
need to move — `PluginContext` stays quasi-stable for the handful of real Python plugins,
documented as "internal tier, best-effort").

**One deletion to schedule:** `PluginContext.register_command`'s unqualified-name
convenience (`base.py:330` registers both `foo.bar` *and* `bar`) lets a plugin shadow or
collide with core command names — exactly the "plugin-shaped mush" failure mode. Require
the namespace, reserve un-namespaced names for core, enforce in the loader.

---

## 7. What this costs — honestly

- **The contract freezes real things.** Once `task.closed`'s required fields are a
  published contract with a CI baseline, renaming a field is a deprecation project
  instead of a refactor. The mitigation is keeping the stable tier *small* (a few dozen
  commands, not 140) — but a small tier also means integrators will constantly ask for
  promotions, and each promotion is permanent. Budget for saying no.
- **At-least-once is a tax on every consumer.** Dedupe-on-id is easy but it is
  documentation, support questions, and the occasional double-created task when someone
  skips the idempotency key. Exactly-once would be a tax on *us* forever; this is the
  right trade, but it is a trade.
- **The durable spine writes every schema'd event to SQLite.** On a busy install that's
  a steady write load on the same file the scheduler transacts against. Batching and
  WAL make it fine at this system's scale (hundreds of events/minute, not thousands/sec)
  — but it's a new reason the DB file grows and a new sweep to keep green, and if event
  volume ever 100×es, the spine forces the Postgres conversation earlier than otherwise.
- **Testing surface roughly doubles at the perimeter.** Every stable-tier command now
  needs contract tests (shape, not just behavior); the delivery worker needs the ugly
  tests (retry, park, cursor rewind, restore-with-epoch-bump); `/api/schema` must never
  lie. The baseline-diff mechanism keeps this cheap per-change but it is a permanent
  fixed cost.
- **Support burden shifts from code to protocol.** Today a misbehaving install is your
  Python. Tomorrow it's someone's Go binary hammering `/api/events` with a stale cursor,
  or a webhook target that 200s and drops. The doctor checks (`subscriptions.parked`,
  delivery lag) are the mitigation — findings, not silent decay — but debugging other
  people's consumers becomes a recurring activity the moment there are consumers.
- **Guarantees narrow at the edges by design.** An external verifier means readiness can
  now wait on something outside the transaction boundary. That is *already* true (PR
  gates, humans) and the gate timeout/escalation machinery is the honest answer — but it
  must be said: every Ring 3 participant added to a critical path makes "why isn't it
  running" more often answered by "because something outside is slow." The predicate
  can always *say* that; it cannot make it not so.
- **Opportunity cost.** Stages 1–4 are perhaps 4–6 focused weeks across Wave 3+. Given
  ecosystem-positioning §9 (dark flags, schema-only gates, no real session on the dev
  machine), none of this outranks making the core run. The sequencing discipline that
  matters: do Stage 0 now because it's nearly free and shapes Wave 3; do Stages 2–3 *as
  part of* the already-planned auth and gates work; and treat Stages 4–5 as demand-driven.
  Extensibility built before the first external tool wants in is inventory, and this
  project already has an inventory problem.

---

## 8. Summary — the five load-bearing ideas

1. **Facts in, events out, decisions stay home.** The readiness predicate, admission,
   and the completion protocol are closed; everything around them is an open channel of
   typed facts.
2. **Gates are the extension hook this system will never otherwise ship.** External
   judgment becomes a scheduling fact via `resolve_gate` — narratable, timeout-bounded,
   failure-isolated. No synchronous hooks on the scheduling path, ever.
3. **The vault defines instances, code defines types.** Generalize the harness-markdown
   pattern to subscriptions, gate templates, and doctor checks; stop at the
   Turing-completeness line.
4. **Out-of-process is the default tier.** Two endpoints (`/api/execute`,
   `/api/events?after_id=`) + scoped tokens + `/api/schema` make any language a
   first-class participant; Python plugins become the privileged exception.
5. **Contracts are files with CI teeth.** The schema registry and stable command tier
   graduate from internal artifacts to committed baselines that a diff-test defends —
   the same invariant-test pattern the codebase already trusts, aimed at compatibility.
