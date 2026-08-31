# Task Session History Implementation Plan

**Goal:** Make every task execution attempt inspectable from its task, accurately show why work stopped, and prevent reused workspaces from displaying the wrong transcript.

**Architecture:** Preserve task/session associations as immutable attempt records at assignment and release. Expose a scoped read-only task history endpoint, reuse SessionDetail with an optional attempt ID, and resolve transcripts by actual harness conversation identity. Existing session records are backfilled conservatively; absent exit times or transcripts stay unknown.

**Tech Stack:** Python, SQLAlchemy/Alembic, FastAPI, React/TypeScript, pytest/Vitest.
**Spec:** User-approved in conversation: Sessions section with agent/model/times/outcome/exit reason, links to exact transcripts, live tmux only for current running session, retained history across retries/reassignment.

## Global constraints
- Work only in this isolated worktree. Never restart daemon, mutate live task/gate state, send terminal input or messages.
- Tests use disposable databases/transcripts and unique TMUX_TMPDIR; strip AQ_ and database environment variables.
- Preserve all user work. No historical transcript guessing by newest cwd.
- Existing user authorization covers main integration/push after verification; daemon deployment remains separately gated.
- Backend and UI work have disjoint file ownership. Root owns transcript readers/watcher, session logs/stream, schema generation, integration.

## Task 1: Durable attempts and task history (backend)
- Add task_session_attempts table + migration and CRUD, assignment/release integration including pool claims (atomic with claim transaction), conservative legacy backfill.
- API GET /api/tasks/{task_id}/sessions => {task_id, sessions: TaskSessionAttempt[]}; enforce project scope before history is returned.
- TaskSessionAttempt fields: id, session_id, task_id, agent_id, agent_name, model, intelligence_class, harness, provider, state, work_dir, started_at, ended_at nullable, end_reason nullable, outcome nullable, session_key nullable.
- DB get_task_session_attempt(attempt_id) => mapping or None. Include existing SessionRecord metadata snapshot and stable links; do not recycle associations.
- Capture ended_at/end_reason on terminal transitions and outcome on task close/release. Legacy data must not fabricate exit times or outcomes.
- Include needs_attention in task get/show and Explain reasons for operational blocks.
- Tests: retry produces distinct attempts; pool A then B retains A; wrong project denied; legacy rows imported idempotently; terminal metadata; explain operational reason. RED then GREEN.

## Task 2: Exact transcript identity (root)
- Reader resolve_session(row) => Path|None. Explicit real key is exact-only and independent of newest scan cap.
- Legacy/unpinned Codex rows (blank key or AQ UUID) discover only an unambiguous transcript with matching cwd and launch metadata; persist actual UUID. No fallback to an unrelated newer conversation.
- Launcher must not set AQ UUID as Codex conversation key.
- Apply same resolution to watcher, session logs, SSE. Attempt query validates association and overlays saved key/workdir/time range; stopped attempt never peeks a current tmux session.
- Tests: reused cwd old/new, old explicit key outside scan cap, ambiguous discovery, missing historical transcript, historical attempt isolation, learned UUID for resume.

## Task 3: Task history UI (frontend)
- TaskSessionAttempt interface matches Task1. Hook GET /api/tasks/{taskId}/sessions.
- Shared Sessions component in task pane and full detail: agent/model/class/start/end/status/reason/outcome, count and clickable attempts; loading/error/empty states.
- Link /sessions/{session_id}?attempt={id}; state.from returns to original task/page.
- SessionDetail fetches GET /api/tasks/{task_id}/sessions as needed via taskId query? Prefer query taskId in link as well; existing useSession remains authoritative current session.
- useTranscriptStream passes attempt_id when present. Historical ended attempt shows read-only transcript, no Attach/Nudge/Kill/live pane; active matching attempt retains shared InteractiveTerminal.
- Tests: both task surfaces, links and back navigation, multiple attempts, unavailable transcript, stopped attempt vs newer live session.

## Verification and review
- Run focused backend and dashboard tests, full dashboard, production build/typecheck, lint on changed sources and diff check.
- Run read-only independent review of migration/scoping/identity/lifecycle correctness and fix actionable findings.
- Regenerate OpenAPI and client, integrate/push completed changes without restarting live daemon.
