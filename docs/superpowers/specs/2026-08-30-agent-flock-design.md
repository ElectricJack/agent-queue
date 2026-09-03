# Global Agent flock

The user's requested model is a global roster of defined, shared workers. The supervisor is one member. Projects own tasks and workspaces, never agent identity. Existing agents already have global IDs; make these records durable instead of introducing a second identity table.

## Identity and execution

Preserve existing IDs, task assignments, locks, and history. Add Agent.role (worker/supervisor), enabled, and optional harness/model/intelligence_class overrides. Keep profile_id as a stable default. Register one supervisor-global agent (Supervisor), linked to the existing n-supervisor--global session, without a fake project or process.

Stop startup deletion and silent profile reassignment of workers. Reuse eligible idle workers globally; project concurrency, budgets, workspaces, and claims remain enforced. Lazily supplied workers are durable. Pools retain task/project-scoped sessions and may reuse idle definitions after safe termination. Ordinary workers never gain global-admin tokens.

Task profiles can specialize capabilities, while explicit worker harness/model/intelligence overrides control execution. Overrides never modify shared profiles. Changes apply on the next session. Populate sessions.agent_id on task/supervisor launches and adoption; record nullable llm_provider/model/intelligence_class launch snapshots. Preserve historical unknowns.

## API

Use existing command infrastructure: list_agents returns the real global roster without an active project; explicit project_id filters current assignments only. get_agent/create_agent/edit_agent manage individual definitions. Global configuration changes require local or global-admin scope.

Row contract: id, name, profile_id, role, enabled, state, provider (LLM vendor), harness, model, intelligence_class, current_task_id/title/current_project_id, exact session_id/session_state/session_provider, settings (name/profile_id/harness/model/intelligence_class/enabled), active_subagent_count (nullable), subagent_count_complete (boolean), aq_subagent_count (integer), native_subagent_count (nullable). project_id and workspace_id may remain nullable compatibility fields. Resolved runtime values prefer launch snapshots; settings hold next-run overrides.

## UI

Collapsible Agent flock above Projects in the left rail; persist collapse preference. Entries show name/state, provider/model, intelligence level, active subagents, and current task or Idle.

Route /agents with ordered repeated agent query parameters. Plain click replaces all views. Shift click appends a distinct agent, maximum four. Existing selection is a no-op; fifth selection preserves four and displays an accessible limit notice. One full window, two columns, or 2x2 for three/four; stack on narrow screens.

Each tile has Terminal (default), Settings, and Close. Close hides only the view and releases its stream; it never stops a worker. Last close shows an empty state. Reuse usePaneStream/LivePaneConsole. Idle/sleeping/no-session agents show an honest state; merely viewing does not launch an agent. Settings edit only this worker and show next-session semantics and save errors.

## Pool instances in the rail

A roster row is a **pool instance** when a pool minted it (`agents.origin = "pool"`, set by `_launch_pool_session` and kept while the row idles between sessions) or when a `lifecycle: pool` session currently owns it (`session_lifecycle = "pool"` on the roster row) — a pool reserves an idle hand-made worker on a compatible profile just as readily as it mints one. Pool instances are listed only beneath their pool entry (joined by the session's project and profile), never beside the fixed workers, and remain openable directly via `/agents?agent=<id>`.

The profile id is not a signal. `pool_status` names every pool profile in every active project even at zero supply, so classifying by profile hid every worker an operator added by hand on a pool-backed profile. Such a worker is a plain durable worker until a pool session takes it, and again once that session ends. `origin` also records `reconciler` for the lazy capacity bootstrap; only `pool` affects the rail.

## Subagents and live updates

Count distinct active direct AQ workers attributable to any session of this parent definition; exclude parent, queued/completed tasks, and non-worker task containers. Record authenticated creator session for elevated supervisors too, separately from worker-only quotas. Native harness counts require explicit child start/finish telemetry. Missing/unsupported telemetry is partial or unknown, never a fabricated zero.

Agent/session/task/message WebSocket events invalidate the flock. Short polling refreshes derived assignment/count state. Terminal SSE stays mounted only for visible Terminal tabs.

## Verification

Migration preserves identities/history. Test global listing with no project, supervisor singleton, stable config across reconciliation/restart, cross-project reuse and no double assignment, scoped settings mutations, launch override precedence, exact session links, active child counts and missing telemetry. UI tests cover metadata/collapse, click/Shift/dedup/cap/close, subscription release, default terminal/settings/save errors, idle states. Build and verify real API/dashboard after a session-preserving restart.

