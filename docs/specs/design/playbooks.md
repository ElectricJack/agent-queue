---
tags: [design, playbooks, automation, workflows]
---

# Playbooks — Agent Workflow Graphs

**Status:** Active
**Supersedes:** `rule-system.md`, `hooks.md` (migration complete, deprecated spec files removed)
**Source files:** TBD
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #3 structure guides intelligence, #7 events not coupling)
**Related:** [[vault]], [[agent-coordination]], [[specs/event-bus]], [[specs/supervisor]], [[specs/plugin-system]]

---

## 1. Problem Statement

The current automation system (rules + hooks) has served as a solid foundation but
is hitting scaling limits as the system grows more sophisticated:

**Rules are isolated.** Each rule generates independent hooks that fire without
awareness of each other. When `task.completed` triggers both `post-action-reflection`
and `spec-drift-detector`, they race independently — even though the spec drift check
is pointless if the reflection already flagged quality problems. There's no way to
express "do X, then conditionally do Y based on X's result."

**Event cascades are invisible.** A hook can create tasks, emit events, and trigger
other hooks. The resulting cascade is only observable at runtime through logs. There is
no static artifact that shows "when this event fires, here's everything that will
happen in what order." Debugging requires mentally simulating the runtime.

**No multi-step reasoning flows.** The system's growing ambitions — analyzing GitHub
issues, self-monitoring logs, generating work queues for human review — require
multi-step reasoning processes where each step builds on prior context. The current
model of "one prompt, one LLM call, done" can't express "classify this issue, then
assess feasibility based on the classification, then draft an approach if feasible."

**No human-in-the-loop.** There's no way for an automation to pause, present findings
to a human, wait for approval or edits, and then resume. Work that requires human
judgment must be fully manual or fully autonomous — there's no middle ground.

**Hooks are opaque execution artifacts.** Users author rules in natural language, which
get compiled into hooks with rendered prompts. But the hooks themselves are black boxes
to users — there's no visual representation of the automation's structure, flow, or
current state.

---

## 2. Vision

Playbooks are **directed graphs of LLM decision points** authored in natural language
markdown. Each node is a focused prompt. Each edge is a condition the LLM evaluates.
The graph encodes *process knowledge* — what to think about, in what order, with what
context — while the LLM provides *judgment* at each step.

Agent Queue is an agent orchestration system. The goal is not to replace LLM reasoning
with deterministic logic, but to give LLM agents **structure to operate within**. Instead
of "here's a prompt, go figure it out," a playbook says "here's where you are in a
process, here's what you need to decide right now, and here are the possible next steps."

This creates a framework for building layers of intelligence: systems that pull issues
from GitHub and assess them, systems that analyze their own logs for insights, systems
that generate and prioritize work for human review. Each of these is a playbook — a
multi-step reasoning process with defined flow, accumulated context, and human
checkpoints.

---

## 3. Core Concepts

### Playbook

A playbook is a directed graph defined in a single markdown file. It describes a
multi-step process that executes in response to events. The markdown is the source of
truth — an LLM compiles it into a JSON workflow graph that the runtime executes.

- One markdown file = one playbook
- Authored in natural English, no structural constraints on the markdown format
- An LLM parses the markdown into a well-defined JSON schema (the "compilation" step)
- The JSON is saved to disk and loaded into memory at runtime
- When the markdown changes, the JSON is recompiled

### Node

A node is a single step in a playbook — a focused LLM decision point. At each node,
the runtime invokes an LLM with:
- The node's prompt/action description
- Accumulated context from prior nodes in this execution
- The original event data that triggered the playbook

The LLM's response is used to:
1. Execute the action described (using available tools)
2. Produce context for downstream nodes
3. Determine which transition to follow

### Transition (Edge)

A transition connects two nodes. It has a condition that the LLM evaluates to determine
the next step. Conditions are natural language descriptions ("if findings exist," "if
the error is transient") that the LLM interprets given the current context.

### Context

Context is the accumulated state of a playbook execution as it flows through nodes.
Each node receives context from prior nodes and can produce additional context for
downstream nodes. This is how a downstream `draft_approach` node knows what an
upstream `classify` node decided — without re-analyzing the original input from scratch.

Context is the playbook's working memory for a single execution run.

### Trigger

The event (or events) that start a playbook execution. Triggers connect the playbook
to the EventBus. A playbook can have multiple triggers (e.g., both `task.completed`
and `task.failed`), with the entry node branching based on which trigger fired.

Periodic triggers (timers) are modeled as synthetic events — e.g., `timer.30m` for
elapsed-time intervals, or `cron.08:00` for daily wall-clock times — so that all
playbooks share the same event-driven execution model.

---

## 4. Authoring Model

### Markdown as Source of Truth

Playbooks are authored as natural language markdown files with minimal frontmatter.
There are **no structural constraints** on the markdown body — the author writes in
plain English, describing the process as they would explain it to a colleague. An LLM
handles the parsing.

```markdown
---
id: code-quality-gate
triggers:
  - git.commit
scope: system
---

# Code Quality Gate

When a commit is made, run vibecop on the changed files. If issues
are found, create tasks to fix them grouped by severity.

On receiving a commit event, run vibecop_check on the files that
changed in the commit. Use the commit's diff to scope the scan —
don't lint the entire repo, just what changed.

If no findings, we're done.

If findings exist, group them by severity:
  - For errors: create one task per file with errors, priority high.
    Include the vibecop output and the file path in the task.
  - For warnings: batch all warnings into a single task, priority
    medium.
  - For info-level findings: log them to project memory but don't
    create tasks.

If the commit was made by an agent that's still running a task,
attach the fix tasks as follow-ups to that agent's current task
rather than creating standalone tasks.
```

### Frontmatter

The only structured portion. Kept minimal:

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique identifier for the playbook |
| `triggers` | yes | List of event types that start this playbook |
| `scope` | yes | `system`, `project`, or `agent-type:{type}` |
| `enabled` | no | Default `true`. Set `false` to disable without deleting |
| `cooldown` | no | Minimum seconds between executions. Default varies by trigger |
| `version` | no | Auto-incremented on each compilation |

### Referencing Resources

Playbooks often need to reference files that live outside the playbook itself —
bundled prompt templates, vault entries, logs, per-task artifacts, a project's
workspace. Absolute filesystem paths are not portable (they break whenever the
daemon runs on a different machine or with a different `data_dir`), and a
hardcoded `~/.agent-queue/...` path hides the fact that the vault root is
configurable.

Instead, use the `aq://` URI scheme. The daemon resolves these URIs against
its own config and database, so the same playbook works unchanged wherever it
runs. All authorities are **read-only** in v1.

| URI | Resolves to |
|---|---|
| `aq://prompts/<path>` | Bundled `src/prompts/<path>` (ships with the daemon) |
| `aq://vault/<path>` | `{vault_root}/<path>` |
| `aq://logs/<path>` | `{data_dir}/logs/<path>` |
| `aq://tasks/<path>` | `{data_dir}/tasks/<path>` |
| `aq://attachments/<path>` | `{data_dir}/attachments/<path>` |
| `aq://workspace/<project_id>/<path>` | Project's primary workspace |
| `aq://workspace-id/<workspace_id>/<path>` | A specific workspace by DB id |

**Which tools understand the scheme.** `read_file` accepts an `aq://` URI in
place of `path`. `read_prompt` and `render_prompt` accept a `uri` parameter as
an alternative to `(project_id, name)`; when `uri` is set, the prompt is
loaded from the resolved path and (for `render_prompt`) `{{variable}}`
placeholders are substituted server-side.

**Example — a playbook step that creates a task whose description is a
rendered bundled prompt:**

```markdown
For each target project, call `create_task` with:

- `project_id`: the project's id
- `title`: "Consolidate memory: <project_name>"
- `description`: the `rendered` field of
  `render_prompt(uri="aq://prompts/consolidation_task.md", variables={...})`
```

**Safety rules.** The resolver rejects `..` segments and absolute path
segments inside the URI, and rejects unknown authorities. The authority
whitelist is the permission model — `aq://` paths skip the workspace-path
validation that applies to plain `read_file` calls, so adding a new
authority requires a deliberate code change.

### LLM Compilation

When a playbook markdown is saved or modified, an LLM reads the natural language
content and compiles it into a JSON workflow graph conforming to the schema defined in
Section 5. This is a one-time operation per edit, not per execution.

The compilation step:
1. Detect that a playbook `.md` file was created or changed (file watcher on vault)
2. Read the markdown content
3. Invoke an LLM with the markdown + the JSON schema definition
4. Validate the output against the schema
5. Write the compiled JSON to `~/.agent-queue/compiled/` (outside the vault — runtime
   artifacts don't belong alongside authored content)
6. Load the updated graph into the runtime

**Recompilation is autonomous.** No human reviews the JSON. The JSON schema is the
contract — if the output is valid JSON conforming to the schema, it's accepted. If
compilation fails (invalid output), the previous compiled version remains active and
an error is surfaced.

**Compilation is deterministic enough.** The same markdown should produce functionally
equivalent JSON across compilations, even if the exact wording of prompts varies
slightly. The schema enforces structural equivalence; the node prompts are allowed
to vary in phrasing since they're interpreted by an LLM at execution time anyway.

---

## 5. Compiled Format (JSON Schema)

The compiled JSON is the runtime artifact. The runtime never reads the markdown
directly — it only executes the JSON graph.

```json
{
  "id": "code-quality-gate",
  "version": 1,
  "source_hash": "a1b2c3d4",
  "triggers": ["git.commit"],
  "scope": "system",
  "cooldown_seconds": 60,
  "max_tokens": 50000,

  "nodes": {
    "scan": {
      "entry": true,
      "prompt": "Run vibecop_check on the files changed in this commit. Use the diff to scope the scan to only changed files, not the entire repo.",
      "transitions": [
        {"when": "no findings", "goto": "done"},
        {"when": "findings exist", "goto": "triage"}
      ]
    },
    "triage": {
      "prompt": "Group the scan findings by severity (error, warning, info).",
      "transitions": [
        {"when": "has errors", "goto": "create_error_tasks"},
        {"when": "warnings only", "goto": "create_warning_task"},
        {"when": "info only", "goto": "log_to_memory"}
      ]
    },
    "create_error_tasks": {
      "prompt": "Create one high-priority task per file that has errors. Include the vibecop output and file path. If the commit was made by an agent still running a task, attach as follow-ups to that agent's task.",
      "goto": "create_warning_task"
    },
    "create_warning_task": {
      "prompt": "Batch all warnings into a single medium-priority task.",
      "goto": "log_to_memory"
    },
    "log_to_memory": {
      "prompt": "Record any info-level findings in project memory for reference.",
      "goto": "done"
    },
    "done": {
      "terminal": true
    }
  }
}
```

Note: the `context_schema`, `context_receives`, and `context_produces` fields from
earlier drafts have been removed. Context flows through conversation history, not
explicit data routing (see Section 6 for details).

### Schema Details

**Top-level fields:**

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique playbook identifier |
| `version` | yes | Auto-incremented on recompilation |
| `source_hash` | yes | Hash of source markdown for change detection |
| `triggers` | yes | Event types that start this playbook |
| `scope` | yes | `system`, `project`, or `agent-type:{type}` |
| `cooldown_seconds` | no | Minimum seconds between executions |
| `max_tokens` | no | Token budget for entire run. Run fails if exceeded |
| `llm_config` | no | Override default model/provider for this playbook |

**Node fields:**

| Field | Required | Description |
|---|---|---|
| `entry` | no | If `true`, this is the starting node. Exactly one per playbook |
| `prompt` | yes (non-terminal) | Focused instruction for the LLM at this step |
| `transitions` | conditional | List of `{when, goto}` pairs. Evaluated by a separate transition call |
| `goto` | conditional | Unconditional next node (mutually exclusive with `transitions`) |
| `terminal` | no | If `true`, execution ends here |
| `wait_for_human` | no | If `true`, pause execution and surface for human review |
| `timeout_seconds` | no | Max time for this node's LLM call before failing |
| `llm_config` | no | Override model/provider for this specific node |
| `summarize_before` | no | If `true`, summarize conversation history before this node to manage context size |

**Transition fields:**

| Field | Description |
|---|---|
| `when` | Natural language condition OR structured function-call expression |
| `goto` | Target node ID |
| `otherwise` | If `true`, this is the default/fallback transition |

---

## 6. Execution Model

### Playbook Executor

The playbook executor replaces the current hook engine's single-shot LLM invocation
with a graph walker that steps through nodes, using the Supervisor's existing
conversation model.

### Context via Conversation History

Context flows through **conversation history**, not explicit data structures. The
Supervisor is already stateless with caller-managed history — the executor maintains
a `messages` list across nodes, and each node's prompt and response become
conversation turns that downstream nodes naturally see.

This eliminates the need for `context_receives` / `context_produces` declarations.
The LLM at each node has the full conversation thread. The node prompts naturally
reference prior work ("Based on the scan results above..." or "Given the findings
you just grouped...") because the prior results are in the conversation.

**How it works concretely:**

```python
class PlaybookRunner:
    def __init__(self, graph: dict, event: dict, supervisor: Supervisor):
        self.graph = graph
        self.supervisor = supervisor
        self.messages: list[dict] = []  # Conversation history
        self.run_id = generate_id()
        self.tokens_used = 0

    async def run(self):
        # Seed conversation with event context
        self.messages.append({
            "role": "user",
            "content": f"Event received: {json.dumps(event)}\n\n"
                       f"You are executing playbook '{self.graph['id']}'. "
                       f"I will guide you through each step."
        })

        node = self._entry_node()
        while not node.get("terminal"):
            # Execute node
            response = await self.supervisor.chat(
                text=node["prompt"],
                history=self.messages,
            )
            self.messages.append({"role": "user", "content": node["prompt"]})
            self.messages.append({"role": "assistant", "content": response})
            self.tokens_used += estimate_tokens(node["prompt"], response)

            # Determine next node
            node = await self._evaluate_transition(node, response)
```

The Supervisor's internal tool-use loop handles tool calls within each node (running
vibecop, creating tasks, etc.). Tool call/result messages are part of the conversation
history managed inside `supervisor.chat()`. The executor only sees the final response.

**Design choice: executor history vs. Supervisor history.** The executor's `messages`
list contains only the node prompts and final responses — not the raw tool
call/result messages from inside each `supervisor.chat()` call. This is intentional:
tool details would bloat the conversation and hit token limits quickly. The LLM's
final response for each node should summarize the relevant results in natural
language. If a downstream node needs specific tool output (e.g., exact vibecop
findings), the node prompt should instruct the LLM to include those details in its
response. If this proves insufficient, the Supervisor can be extended to return its
full internal message list for inclusion in the executor history.

### Context Size Management

As playbooks grow longer, conversation history can exceed token limits. Two mechanisms
address this:

**Automatic summarization.** If a node has `"summarize_before": true`, the executor
summarizes the conversation history into a condensed form before invoking that node.
This is a lightweight LLM call that produces a ~500-token summary of key decisions
and outputs so far, replacing the full history.

**Natural brevity.** For most playbooks (3-7 nodes), the conversation stays well
within context limits. Summarization is only needed for unusually long or deep
playbooks. The compilation step can add `summarize_before` markers automatically
when the node graph exceeds a depth threshold.

### Transition Evaluation

Transitions are evaluated in a **separate LLM call** after each node completes. This
clean separation means the node prompt focuses purely on its action, and the
transition logic is evaluated independently.

**Two transition types:**

**Natural language transitions** — The executor makes a focused LLM call with the
node's response and the list of conditions:

```python
async def _evaluate_transition(self, node, response):
    if "goto" in node:
        return self.graph["nodes"][node["goto"]]

    # Separate, cheap LLM call for transition evaluation
    transition_prompt = (
        f"Based on the result above, which condition is met?\n"
        + "\n".join(f"- {t['when']}" for t in node["transitions"])
        + "\n\nRespond with ONLY the matching condition text."
    )
    decision = await self.supervisor.chat(
        text=transition_prompt,
        history=self.messages,  # Has full context
    )
    return self._match_transition(decision, node["transitions"])
```

This call can use a cheaper/faster model via per-node `llm_config` override since
it's a classification task, not a reasoning task.

**Structured transitions (function-call)** — When conditions are deterministic (e.g.,
"findings count > 0"), the compilation step can express them as structured expressions
rather than natural language. The executor evaluates these without an LLM call:

```json
{
  "transitions": [
    {"when": {"function": "has_tool_output", "contains": "no findings"}, "goto": "done"},
    {"otherwise": true, "goto": "triage"}
  ]
}
```

The compiler decides which form to use based on the markdown's language — simple
conditionals become structured, nuanced judgments stay as natural language.

### Execution Flow (Complete)

1. Event fires on EventBus, matched to playbook trigger(s)
2. Executor creates a new **PlaybookRun** (persisted to DB, see Section 6a)
3. Create a Supervisor instance with appropriate tool access
4. Seed conversation with event context
5. Enter the `entry` node
6. At each node:
   a. If `summarize_before`: compress conversation history
   b. Send node prompt via `supervisor.chat()` with accumulated history
   c. Supervisor executes tools internally, returns final response
   d. Check token budget — fail if `max_tokens` exceeded
   e. If `wait_for_human`: persist run state, pause (see Section 9)
   f. If `transitions`: evaluate via separate call or structured check
   g. If `goto`: follow unconditionally
   h. If `terminal`: end execution
   i. Update PlaybookRun record with node result
7. Mark run as completed, record final state

### Customizable Agent Configuration

Different playbooks and nodes may need different agent configurations. The current
single-Supervisor model is insufficient — a transition evaluation doesn't need the
same model or tools as a complex reasoning step.

**Per-playbook `llm_config`** overrides the default chat provider for all nodes in
the playbook. Useful for playbooks that need a specific model (e.g., a cheaper model
for high-frequency periodic playbooks).

**Per-node `llm_config`** overrides further for individual nodes. Transition
evaluation calls can use a fast/cheap model. Complex reasoning nodes can use the
most capable model. This is the mechanism for cost control at the node level.

The Supervisor instance is created per-playbook-run, but the executor can swap the
underlying chat provider between nodes based on `llm_config`.

### Run Persistence

Each playbook execution creates a `PlaybookRun` record in the database:

| Field | Type | Description |
|---|---|---|
| `run_id` | str | Unique run identifier |
| `playbook_id` | str | Source playbook |
| `playbook_version` | int | Compiled version at time of run |
| `trigger_event` | JSON | The event that started this run |
| `status` | str | `running`, `paused`, `completed`, `failed`, `timed_out` |
| `current_node` | str | Current/last node (for paused/failed runs) |
| `conversation_history` | JSON | Serialized message list (for resume) |
| `node_trace` | JSON | List of `{node_id, started_at, completed_at, status}` |
| `tokens_used` | int | Cumulative token count |
| `started_at` | float | Unix timestamp |
| `completed_at` | float | Unix timestamp (null if in progress) |
| `error` | str | Error message if failed |

For paused runs (human-in-the-loop), the full conversation history is persisted so
the run can resume exactly where it left off, even across process restarts.

The `node_trace` provides the data needed for dashboard visualization — which path
the run took through the graph, how long each node took, where failures occurred.

### Concurrency

- Multiple playbook instances can run concurrently (e.g., two `git.commit` events
  arrive in quick succession → two instances of `code-quality-gate`)
- Within a single instance, nodes execute sequentially (graph walk, not parallel)
- Global concurrency limits carry over from current hook engine (`max_concurrent_hooks`
  becomes `max_concurrent_playbook_runs`)
- Cooldown per playbook (not per node) prevents rapid re-triggering

### Token Budget

Playbooks can declare a `max_tokens` budget in the compiled JSON (derived from
frontmatter or set by the compiler). The executor tracks cumulative token usage
across all nodes and transition calls.

If the budget is exceeded mid-run:
- The current node completes (don't cut off mid-response)
- The run is marked as `failed` with reason `token_budget_exceeded`
- The partial context trace is preserved for debugging
- A notification is sent

A global budget cap (`max_daily_playbook_tokens` in config) prevents runaway costs
across all playbooks. This complements the existing per-task budget system.

### Error Handling

If a node's LLM call fails or times out:
- The run is marked as failed at that node
- Conversation history is preserved in the run record for debugging
- No automatic retry at the node level (the playbook author can design retry logic
  as explicit nodes/transitions if desired)
- Failed runs are surfaced in Discord notifications and the dashboard

---

## 7. Event System

Playbooks are driven by events. The current EventBus is the right foundation, but
the event catalog needs expansion.

### Current Events (Usable Today)

| Event | Source | Notes |
|---|---|---|
| `task.started` | Orchestrator | Task assigned to agent |
| `task.completed` | Orchestrator | Task finished successfully |
| `task.failed` | Orchestrator | Task errored |
| `task.paused` | Orchestrator | Task paused with resume timer |
| `task.waiting_input` | Orchestrator | Agent asked a question |
| `note.created` | Orchestrator | Note saved |
| `file.changed` | FileWatcher | Watched file modified |
| `folder.changed` | FileWatcher | Watched folder contents changed |
| `plugin.*` | PluginRegistry | Plugin lifecycle events |
| `config.reloaded` | ConfigWatcher | Config file changed |

### New Events Needed

| Event | Source | Payload | Enables |
|---|---|---|---|
| `git.commit` | GitManager | `commit_hash`, `branch`, `changed_files`, `message`, `author`, `project_id`, `agent_id` | Code quality gates, changelog generation, spec drift detection scoped to actual changes |
| `git.push` | GitManager | `branch`, `remote`, `commit_range`, `project_id` | Deployment triggers, PR creation flows |
| `git.pr.created` | GitManager | `pr_url`, `branch`, `title`, `project_id` | Review workflows, notification chains |
| `git.pr.merged` | External/webhook | `pr_url`, `branch`, `project_id` | Post-merge cleanup, deployment |
| `github.issue.opened` | External/webhook | `issue_url`, `title`, `body`, `labels` | Issue triage, work generation |
| `github.issue.commented` | External/webhook | `issue_url`, `comment_body`, `author` | Conversation tracking, auto-responses |
| `playbook.run.completed` | PlaybookExecutor | `playbook_id`, `run_id`, `final_context` | Cross-playbook composition |
| `playbook.run.failed` | PlaybookExecutor | `playbook_id`, `run_id`, `failed_at_node`, `error` | Meta-monitoring |
| `human.review.completed` | Dashboard/Discord | `playbook_id`, `run_id`, `node_id`, `decision`, `edits` | Resume paused playbooks |

### Periodic Triggers as Synthetic Events

Periodic triggers are modeled as synthetic events so that all playbooks share a
uniform event-driven model. Two families are supported:

- **`timer.{N}m` / `timer.{N}h`** — elapsed-time interval from daemon start (e.g.
  `timer.30m`, `timer.4h`, `timer.24h`). Not persisted; daemon restart resets
  elapsed time and fires all intervals on the first tick.
- **`cron.HH:MM`** — daily wall-clock time in the system's local timezone (e.g.
  `cron.07:00`, `cron.17:30`). Fires once per local day at-or-after the target.
  Persisted to `{data_dir}/timer_state.json` so daemon restarts do not cause a
  same-day re-fire.

Both families share the payload shape `{"tick_time": "...", "interval": "..."}`.
For timer events, `interval` is the spec after the `timer.` prefix (e.g. `"30m"`);
for cron events it is the `HH:MM` target.

This eliminates the split between "event-driven" and "periodic" execution models.
Everything is event-driven.

### Timer Service

The timer service is a lightweight component in the orchestrator loop that emits
synthetic `timer.*` and `cron.*` events.

**Behavior — `timer.*` (periodic intervals):**
1. On startup and whenever a playbook is compiled, the timer service scans all
   compiled playbooks and collects the set of unique timer intervals from triggers
2. Only intervals with at least one active subscriber are tracked
3. Each tick (~5s), the service checks if any interval has elapsed and emits the
   corresponding `timer.{interval}` event
4. Arbitrary intervals are supported (minimum 1 minute, no maximum)
5. If a playbook is disabled or removed, its interval is dropped (unless another
   playbook also uses it)

**Behavior — `cron.*` (daily wall-clock triggers):**
1. Same discovery/rebuild flow: scans playbook triggers, keeps only those
   subscribed. `cron.HH:MM` targets are parsed into `(hour, minute)` in local time.
2. On each tick, for every cron target whose last-fired date is not today, the
   service fires if the current local wall-clock time is at-or-past the target.
3. Each cron trigger fires at most once per local day. Date-based dedup is
   authoritative — the "already fired today" check means missed minutes don't
   cause double-fires, and DST fall-back's repeated 01:30 fires only once.
4. `_cron_last_fired_date` is persisted to `{data_dir}/timer_state.json` on
   each fire. A daemon restart at 08:15 after already firing `cron.08:00` at
   08:00 will not re-fire; but a daemon started at 08:15 having *never* fired
   today will fire at 08:15 (missed-but-same-day catches up).
5. DST spring-forward skips the non-existent hour (e.g. `cron.02:30` does not
   fire on the day 02:30 doesn't exist). Not suitable for load-bearing
   scheduling; playbooks are the target use case.

Timer/cron events carry `project_id: null` — they are inherently system-scoped.
A project-scoped playbook can still trigger on them: the executor injects the
playbook's own `project_id` before scope matching, so the run is scoped to that
project. Each (playbook, trigger) pair fires once per tick — the same cron
tick fires each subscribing playbook once, scoped to whichever project owns it.

### Event-to-Scope Matching

When an event fires, the executor must match it to the correct playbooks. This
requires knowing the event's scope:

**Events with `project_id`:** Task lifecycle events (`task.*`), git events (`git.*`),
file/folder events, note events. These match:
- System-scoped playbooks (always)
- Project-scoped playbooks for the matching project
- Agent-type playbooks matching the originating agent's type

**Events without `project_id`:** Config events, plugin events,
`playbook.run.completed` from system-scoped playbooks. These match:
- System-scoped playbooks only
- Project-scoped playbooks are skipped (no project to match)

**Timer/cron events** are a special case: emitted by the system-level scheduler
without a `project_id`, but project-scoped subscribers still fire with their
own `project_id` auto-injected by the executor (see above).

**Enforcement:** All events emitted by the orchestrator and git manager MUST include
`project_id` when the operation is project-scoped. The EventBus does not enforce this
— it is a contract between emitters and the playbook executor. Events missing
`project_id` are treated as system-scoped.

---

## 8. Scoping

Playbooks can be scoped to control where and when they apply.

### Scope Levels

**`system`** — Applies globally. One instance of the playbook runs regardless of
which project or agent triggered the event. System-scoped playbooks handle
cross-cutting concerns: health monitoring, dependency audits, infrastructure checks.

**`project`** — Applies to a specific project. The playbook only fires for events
within that project. Multiple projects can each have their own version of the same
playbook with project-specific logic.

**`agent-type:{type}`** — Applies to a specific agent type (e.g., `agent-type:coding`).
These playbooks run autonomously in response to events, scoped to the agent type.
For example, a coding agent's `reflection.md` playbook triggers on `task.completed`
and extracts insights into the coding agent's memory.

**Important distinction:** Agent-type playbooks are **not** the same as agent
behavioral rules. Behavioral rules ("always run tests before committing") live in
the agent's `profile.md` under `## Rules` and are injected into the agent's prompt
as trust-based guidance. Agent-type playbooks are event-driven workflows that run
alongside agents, not instructions injected into them. See
[[profiles|profiles as markdown]] for the profile model.

### Scope Resolution

When an event fires, the executor collects all matching playbooks:
1. All `system`-scoped playbooks with matching triggers
2. All `project`-scoped playbooks for the event's project with matching triggers
3. All `agent-type`-scoped playbooks matching the originating agent's type

Multiple playbooks can fire for the same event. They run as independent instances
with no implicit coordination. If coordination is needed, it should be explicit —
either consolidate into one playbook or use `playbook.run.completed` events for
sequencing.

### Storage

Playbooks live inside the [[vault|vault]].
Compiled JSON lives outside the vault in `~/.agent-queue/compiled/`.

```
~/.agent-queue/vault/
  system/
    playbooks/                     # System-scoped playbooks
      task-outcome.md
      system-health.md
  orchestrator/
    playbooks/                     # Orchestrator-specific playbooks
      task-assignment.md
  agent-types/
    coding/
      playbooks/                   # Agent-type-scoped playbooks
        coding-standards.md
  projects/
    {project_id}/
      playbooks/                   # Project-scoped playbooks
        code-quality-gate.md

~/.agent-queue/compiled/           # Runtime artifacts (outside vault)
  system/
    task-outcome.compiled.json
    system-health.compiled.json
  orchestrator/
    task-assignment.compiled.json
  agent-types/
    coding/
      coding-standards.compiled.json
  projects/
    mech-fighters/
      code-quality-gate.compiled.json
```

This separation keeps the vault clean for human editing in Obsidian while
giving the runtime its own space for generated artifacts.

---

## 9. Human-in-the-Loop

Some playbooks need human judgment at specific points. A node with `wait_for_human: true`
pauses execution and surfaces the accumulated context for human review.

### Pause and Resume

1. Executor reaches a `wait_for_human` node
2. The run's state (current node, accumulated context, event data) is persisted to DB
3. A notification is sent (Discord, dashboard) with the context and a prompt for
   the human to review
4. The playbook instance is parked — it consumes no resources while waiting
5. The human reviews, optionally edits the context, and submits a decision
6. A `human.review.completed` event fires with the human's input
7. The executor resumes the run from the paused node, with the human's input
   added to context

### Use Cases

- **Issue triage:** LLM classifies and assesses an issue, then pauses for human
  approval before creating tasks
- **Deployment gates:** Automated checks pass, playbook pauses for human sign-off
  before triggering deploy
- **Work queue review:** System generates proposed tasks from log analysis, human
  reviews and edits before they enter the queue

### Timeout

Paused playbooks have a configurable timeout (default: 24 hours). If no human
response arrives, the playbook either:
- Transitions to a designated timeout node (if defined)
- Fails with a timeout error (default)

---

## 10. Composability

Playbooks are building blocks. Complex automation is built by composing smaller
playbooks rather than writing monolithic ones.

### Cross-Playbook Communication

Playbooks communicate exclusively through events. There is no direct invocation of
one playbook from another.

- When a playbook completes, a `playbook.run.completed` event fires with the
  playbook's final context
- Another playbook can trigger on `playbook.run.completed` with a **payload filter**
  on the source playbook ID (see "Event Payload Filtering" below)
- This keeps playbooks decoupled — you can add, remove, or modify downstream
  playbooks without touching upstream ones

### Event Payload Filtering

The current EventBus matches on event type only. For composition to work, triggers
need **payload filters** — conditions on event data fields:

```yaml
triggers:
  - type: playbook.run.completed
    filter:
      playbook_id: code-quality-gate
```

The compiled JSON equivalent:

```json
{
  "triggers": [
    {
      "event_type": "playbook.run.completed",
      "filter": {"playbook_id": "code-quality-gate"}
    }
  ]
}
```

This requires extending the EventBus to support filtered subscriptions. The filter
is a simple dict of `{field: expected_value}` pairs — all must match for the event
to trigger the playbook. This is a **prerequisite refactor** to the existing EventBus
(see Section 17: Prerequisite Refactors).

For simple triggers without filters, the string shorthand remains valid:

```yaml
triggers:
  - task.completed    # Shorthand: matches all task.completed events
```

### Example: Composition Chain

```
git.commit
  → code-quality-gate playbook runs
    → playbook.run.completed {playbook_id: "code-quality-gate", context: {...}}
      → post-commit-summary playbook triggers (filtered on playbook_id match)
        → Summarizes what happened and posts to Discord
```

### Sub-Playbook Invocation (Deferred)

A possible extension: a node could explicitly invoke another playbook as a
sub-routine, passing context in and receiving results back. This would be modeled as:

```json
{
  "invoke_playbook": "code-quality-gate",
  "input_context": {"files": "..."},
  "output_context_key": "quality_results",
  "goto_on_complete": "next_step",
  "goto_on_failure": "handle_error"
}
```

This is deferred to avoid premature complexity. Event-based composition is sufficient
for the initial implementation.

---

## 11. Memory Integration

Playbooks interact with the [[memory-scoping|vault memory system]] in two
directions: reading memories for context, and writing insights as output.

### Reading: Memory via Tools

Memory search is **tool-driven, not automatic**. The Supervisor has access to
`memory_search`, `memory_recall`, and `memory_get` as tools. When a node's prompt
references prior knowledge ("check if this is a recurring failure"), the LLM decides
whether to call a memory tool as part of executing the node — just like it decides
whether to call any other tool. It can use `memory_recall` for exact lookups
("what's the test command?") or `memory_search` for semantic queries ("what do we
know about this error pattern?"), or `memory_get` to let the system decide.

This avoids wasteful pre-searches on nodes that don't need memory, and lets the LLM
craft targeted queries rather than the executor guessing what to search for. The
scoping follows the memory hierarchy automatically — `search_memory` uses the
playbook's scope to determine which collections to query.

### Writing: Insight Extraction

Playbook nodes can write to memory via MCP tools (`memory_save`, `memory_store`).
The LLM at any node can call these tools when it identifies something worth
remembering. For example, the `task-outcome` playbook's reflection node might save a
pattern it discovered about recurring failures.

Insights written by playbooks are tagged with the source playbook and run ID for
traceability:

```markdown
---
tags: [insight, auto-generated, recurring-error]
source_playbook: task-outcome
source_run: run-abc123
created: 2026-04-07
---

# SQLAlchemy async session requires expire_on_commit=False

Discovered after three consecutive task failures involving stale
object references in async SQLAlchemy sessions...
```

### Playbook-Driven Self-Improvement

Two categories of playbooks directly drive the [[self-improvement|self-improvement loop]]:

**Reflection playbooks** (agent-type scoped) run after task completion and extract
insights from the agent's work into its type memory. These live at
`vault/agent-types/{type}/playbooks/reflection.md`.

**Analysis playbooks** (system/orchestrator scoped) run periodically to review logs,
identify patterns, and generate operational insights. These live at
`vault/system/playbooks/` or `vault/orchestrator/playbooks/`.

Both follow the standard playbook model — they're just playbooks whose purpose is
knowledge extraction rather than task execution.

---

## 12. Default Playbooks

The current six default rules map to two playbooks, demonstrating the consolidation
benefit:

### Task Outcome (`task-outcome.md`)

Consolidates: `post-action-reflection`, `spec-drift-detector`, `error-recovery-monitor`

```markdown
---
id: task-outcome
triggers:
  - task.completed
  - task.failed
scope: system
---

# Task Outcome

When a task completes or fails, evaluate it and take follow-up action.

If the task completed:
  First, review the output against the acceptance criteria. Note
  whether it passed or had issues.

  Then, check if the completed task modified files that have
  corresponding specs. If the code diverged from a spec, create
  a task to update the spec. Skip this if the reflection step
  already flagged quality problems — spec sync is pointless if
  the work needs to be redone.

  Update project memory with insights from both checks.

If the task failed:
  Check whether this is a recurring failure by looking at recent
  task history.

  If the error is transient (rate limit, timeout, network), retry
  the task.

  If it's a code issue, create a fix task. If post-action-reflection
  previously flagged this area as problematic, include that context
  in the fix task.
```

### System Health (Split into Three Playbooks)

The original design combined all periodic checks into one multi-trigger playbook.
However, a playbook with triggers `[timer.30m, timer.4h, timer.24h]` would fire
on EVERY interval, requiring the entry node to branch on which timer triggered it.
This adds complexity and means the playbook runs three times when all timers align.

**Better approach:** split into three focused playbooks, each with its own interval.
They can share insights through events and memory.

**`system-health-check.md`** (was `periodic-project-review`):

```markdown
---
id: system-health-check
triggers:
  - timer.30m
scope: system
---

# System Health Check

Every 30 minutes, check for stuck tasks (ASSIGNED or IN_PROGRESS
too long), orphaned hooks, rule-hook sync issues, and BLOCKED
tasks with no resolution path. Post a summary if anything is wrong.
```

**`codebase-inspector.md`** (was `proactive-codebase-inspector`):

```markdown
---
id: codebase-inspector
triggers:
  - timer.4h
scope: system
---

# Codebase Inspector

Inspect a random section of the codebase for quality issues,
security risks, and documentation gaps. Follow weighted selection:
source (40%), specs (20%), tests (15%), config (10%), recent
changes (15%). Check inspection history to avoid re-inspecting
the same files. Only report concrete, actionable findings.

If the system health check recently flagged a related issue,
consolidate into one task rather than creating duplicates.
```

**`dependency-audit.md`** (was `dependency-update-check`):

```markdown
---
id: dependency-audit
triggers:
  - timer.24h
scope: system
---

# Dependency Audit

Run dependency audit (pip-audit + check-outdated-deps). Create
high-priority tasks for critical vulnerabilities. Summarize
non-critical updates as a note.
```

This follows the principle of one playbook per concern. Cross-playbook awareness
happens through memory (the health check writes findings to system memory, the
inspector reads them before creating duplicate tasks).

---

## 13. Migration Path

The transition from rules + hooks to playbooks is incremental, not a big bang. This
migration runs in parallel with the vault migration described in [[vault]].

### Phase 1: Vault Structure + Playbook Runtime

- Create the vault directory structure (`~/.agent-queue/vault/`)
- Implement the playbook executor, compiler, and file watcher on the vault
- Playbooks and hooks coexist — both systems listen to events
- Default playbooks ship alongside (not replacing) default rules
- Validate that playbook execution produces equivalent results

### Phase 2: Default Rule Migration

- Replace default rules with default playbooks in the vault
- Migrate user-created rules that map cleanly to single-node playbooks
  (most active rules are effectively one-node playbooks)
- Move rule files from `memory/*/rules/` to `vault/` playbook locations
- Rules that have no trigger (passive rules) migrate to agent-type memory
  in the vault — they're contextual guidance, not workflows

### Phase 3: Hook Engine Deprecation

- Remove the hook engine
- Rules become a legacy concept — existing rule files are auto-converted
  to single-node playbooks
- The RuleManager is replaced by the PlaybookManager
- Hook-related commands (`list_hooks`, `fire_hook`, etc.) are redirected
  to playbook equivalents

### Passive Rules

Passive rules (no trigger, influence supervisor reasoning via semantic search) are
out of scope for playbooks. They serve a different purpose — contextual guidance
rather than workflow automation. In the new architecture, they become memory files
in the appropriate agent-type or project scope within the vault, where they're
surfaced through memory search rather than a separate rule mechanism.

---

## 14. Dashboard Visualization

The compiled JSON graph is directly renderable as a visual diagram. The dashboard
supports:

- **Graph view:** Nodes as boxes, transitions as arrows, conditions as edge labels.
  Color-code by node type (action, decision, human checkpoint, terminal).
- **Live state:** For running playbook instances, highlight the current node.
  Show accumulated context at each completed node.
- **Run history:** Timeline of past runs with the path taken through the graph
  highlighted. Click a node to see the LLM prompt and response for that step.
- **Authoring:** Visual graph editor that generates markdown, closing
  the loop — author visually, edit as text, compile to JSON, render as graph.

---

## 15. Playbook Commands

New commands for authoring, testing, and managing playbooks. These replace the
current hook commands during Phase 3 of migration.

| Command | Description |
|---|---|
| `compile_playbook` | Manually trigger compilation of a playbook markdown |
| `dry_run_playbook` | Simulate execution with a mock event, no side effects |
| `show_playbook_graph` | Render the compiled graph as ASCII or mermaid diagram |
| `list_playbook_runs` | List recent runs for a playbook with status and path taken |
| `inspect_playbook_run` | Show full node trace, conversation, and token usage for a run |
| `resume_playbook` | Resume a paused (human-in-the-loop) playbook run |
| `list_playbooks` | List all playbooks across scopes with status and last run |

These are registered in the command handler via the tool registry, following the
existing pattern for command registration.

---

## 16. Plugin Integration

Plugins currently register hooks and emit events. In the playbook world:

**Plugins as event emitters:** Plugins continue to emit events via the EventBus.
Playbooks subscribe to plugin events like any other event. No change needed.

**Plugins as tool providers:** Plugins expose tools (e.g., vibecop provides
`vibecop_scan`). Playbooks use these tools through the Supervisor. If a plugin is
unloaded while a playbook run is in progress, tool calls to that plugin's tools
will fail. The playbook's error handling (fail the node, preserve context) applies.

**Plugins do NOT register playbooks.** Playbooks are user/system-authored artifacts
in the [[vault|vault]]. A plugin that wants to provide
automation ships a default playbook markdown in its package, which gets installed to
the vault via `install_defaults()` — the same pattern as current default rules.

[[agent-coordination|Coordination playbooks]] are a special case — they use the same
playbook execution model but their purpose is orchestrating multi-agent workflows
rather than single-agent automation.

**Migration:** Existing plugin-created hooks are migrated to playbooks in Phase 2.
Plugin hooks that use `@cron()` decorators become timer-triggered playbooks.

---

## 17. Prerequisite Refactors

These changes to existing code should happen before or in parallel with playbook
implementation. They are independently valuable.

### EventBus Payload Filtering

The EventBus currently matches on event type only. Playbook composition requires
payload filters (Section 10). Extend the EventBus subscription model:

```python
# Current
bus.subscribe("task.completed", handler)

# Extended
bus.subscribe("playbook.run.completed", handler,
              filter={"playbook_id": "code-quality-gate"})
```

Filter is a dict of `{field: expected_value}` — all conditions must match. Events
without matching fields are skipped. This is backward-compatible — existing
subscriptions without filters continue to work.

### Event Schema Registry

Events are stringly-typed with ad-hoc payloads. The "events must carry `project_id`"
contract is enforced by convention, not code. Add a lightweight registry:

```python
EVENT_SCHEMAS = {
    "task.completed": {
        "required": ["task_id", "project_id", "title"],
        "optional": ["agent_id", "agent_type"],
    },
    "git.commit": {
        "required": ["commit_hash", "branch", "changed_files", "project_id"],
        "optional": ["agent_id", "message", "author"],
    },
    # ...
}
```

Validation at emit time in dev mode, warnings in prod. Auto-generates documentation
for playbook authors. Catches missing `project_id` before it causes silent scope
mismatches.

### GitManager Event Emission

Add `git.commit`, `git.push`, and `git.pr.created` events to the existing
GitManager. This is a small, self-contained change — add `bus.emit()` calls after
the corresponding git operations. Valuable immediately for current hooks too.

### Supervisor Configuration Flexibility

The current Supervisor is monolithic — one class, one tool set, one model config.
Per-node `llm_config` requires the Supervisor to support:

- Swappable chat provider (model) per `chat()` call
- Configurable tool sets (transition evaluation nodes don't need shell access)
- Different system prompts per context

Approach: extend `Supervisor.chat()` to accept optional `llm_config` and
`tool_overrides` parameters. The PlaybookRunner passes these based on the current
node's configuration. This avoids a full Supervisor refactor while enabling the
per-node flexibility the playbook system needs.

### Unified Vault File Watcher

Rather than separate file watchers for playbooks, profiles, memories, and READMEs,
implement one vault-wide watcher that dispatches based on path:

```
vault/**/*.md changed →
  */playbooks/*.md   → recompile playbook
  */profile.md       → sync profile to DB
  */memory/**/*.md   → re-index in vector DB
  projects/*/README.md → update orchestrator summary
  */overrides/*.md   → re-index in project collection
```

One watcher, one debounce strategy, one log stream. Simpler to operate and debug.

### Task Records Migration

Move task records from `memory/{project_id}/tasks/` to `tasks/{project_id}/`
(outside the vault). This stops task records from polluting memory search results.
It's a path change plus a re-index — no logic changes. Should happen in Phase 1.

---

## 18. Resolved Design Decisions

These were originally open questions, now resolved:

- **Context model:** Conversation history, not explicit context routing (Section 6)
- **Context size:** `summarize_before` node flag, auto-added by compiler at depth
  thresholds (Section 6)
- **Transition evaluation:** Separate LLM call; structured function-call expressions
  for deterministic cases (Section 6)
- **Run persistence:** PlaybookRun DB table with full conversation history (Section 6)
- **Token budget:** Per-playbook `max_tokens` + global daily cap (Section 6)
- **Event-project matching:** Events must carry `project_id`; events without it are
  system-scoped only (Section 7)
- **Timer service:** Scans compiled playbooks for intervals and daily cron
  targets, emits only subscribed `timer.*` / `cron.*` events (Section 7)
- **Agent-type playbook identity:** Playbooks are event-driven workflows, not
  behavioral rules; rules live in profiles (Section 8)
- **Compiled JSON naming:** Mirrors vault directory structure to avoid collisions
  (Section 8)

---

## 19. Open Questions

1. **Compilation prompt engineering.** The compilation LLM call needs a carefully
   designed prompt that includes the JSON schema and produces reliable output. Should
   this use a dedicated system prompt, or can it reuse the existing Supervisor? What
   model is cost-effective for compilation (it's a structured extraction task)?

2. **Testing and validation.** How do playbook authors test their playbooks without
   triggering real side effects? Dry-run mode with mock events? A simulation
   environment? This is important for iterating on playbook design.

3. **Versioning.** When a playbook is recompiled, in-flight runs use the old version
   (the version is captured in PlaybookRun). But how long do old compiled versions
   persist? Do we keep N versions? Delete after all runs complete?

4. **Observability.** What metrics matter? Tokens per node, total run duration,
   transition paths taken, failure rates by node? Should the dashboard show a
   "playbook health" view aggregating across runs?

5. **Parallel branches.** The current design enforces sequential node execution
   within a playbook instance. If parallel branches are needed (e.g., run two
   independent checks simultaneously), should the schema support fork/join
   semantics? Or is this better handled by two separate playbooks communicating
   via events? Leaning toward the latter for simplicity.

6. **Graph validation.** The compiler should validate the graph for common errors:
   unreachable nodes, missing entry point, cycles without exit conditions,
   transitions referencing nonexistent nodes. Should this be a post-compilation
   check or part of the compilation prompt?
