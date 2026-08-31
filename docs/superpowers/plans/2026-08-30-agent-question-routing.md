# Agent Question Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Catch worker questions and return supervisor/human answers into the same safe live session.
**Architecture:** Native transcript completion events feed a durable question service. Scoped commands and Discord views answer or escalate; the service fences provider delivery against the original task/session instance and suppresses stall handling while waiting.
**Tech Stack:** Python, SQLAlchemy async Core, FastAPI/Click, discord.py, React/TypeScript, pytest/Vitest.
**Spec:** docs/superpowers/specs/2026-08-30-agent-question-routing.md

## Global Constraints

No production Discord test sends. No worker restart/requeue to answer. No reset of saved stall counters. No privilege expansion for worker tokens. Keep exact task/session provenance and existing manual terminal input protection. Default uncertain approval decisions to human. All work in this isolated worktree; no independent merge/restart. Each task owns its listed files; report overlap before editing.

## Shared interfaces

- Extend TranscriptEntry with `turn_complete: bool = False` at the end; completed assistant text has this flag, uuid is stable, parent_uuid contains turn ID where available. No new entry type.
- New service at `src/sessions/questions.py`: `AgentQuestionService(db, bus, providers, config)`, `async observe(row, entries)`, `async tick(now=None)`, `async answer(question_id, body, *, actor, human)`, `async escalate(question_id, reason)`. Answer returns result dict or {error: str}. Service owns all routing/delivery policy.
- Database getters return dicts: `get_agent_question(id)`, `list_agent_questions(project_id=None, session_id=None, pending_only=True)`; expose question fields `id, session_id, session_name, instance_token, task_id, project_id, agent_id, turn_id, question, requires_human, state, answer, answered_by, created_at, updated_at, discord_channel_id, discord_message_id`. States `supervisor`, `human`, `answered`, `delivered`, `resolved`, `stale`; pending = supervisor/human/answered. Additional private fields allowed for reliable notification and routing.
- `mark_agent_question_notified(question_id, channel_id, message_id)` stores actual successful delivery; a failed Discord call must not set this.
- Event `agent.question` carries a flat question dict (same fields). Emit only human-state unnotified records with bounded retry; Discord acknowledges via DB mark method. Event `agent.question.updated` carries flat updated record for UI.
- Command names `question_list` (optional project_id), `question_answer` (question_id, body), `question_escalate` (question_id, reason). Server scope determines human/supervisor identity; never trust client human/actor fields. Local trusted caller is human; sessions must be elevated AND have a live supervisor-profile row, project must match unless global. Human-only questions reject supervisor answers. Ordinary workers cannot answer/escalate others' questions.
- Core exposes service as `orchestrator.agent_questions`. Generic /api/execute handles registered question commands; CLI uses CLIClient.execute. Discord calls handler.execute with question ID after its authorization check.

### Task 1: Transcript completion compatibility
**Files:** src/sessions/transcripts/base.py, codex.py, claude.py; tests/test_transcript_readers.py or new tests/test_transcript_completions.py. Do not edit watcher.
**Produces:** turn_complete entries under the shared contract.
- [ ] Write failing tests using a Codex response_item visible commentary + final_answer + task_complete fixture and legacy agent_message fixture; run to prove visible text/completion is lost.
```python
entries, _ = await reader.read_new(path, 0)
assert [(e.text, e.turn_complete) for e in entries if e.text] == [("Checking files", False), ("Which test should I run?", True)]
```
- [ ] Implement native format compatibility with stable deduplication across incremental reads; never expose user system/developer frames or reasoning. Final text must emit once with completion even where the terminator is in a later read. Claude text end_turn is completed, tool calls are not.
- [ ] Test fragmented lines, duplicate event formats across separate polls, missing text completion, reused session path behavior, legacy transcript tests.
- [ ] Report exact files/results in task-1-report.md. No main merge or runtime changes.

### Task 2: Durable question service and security
**Files:** new src/sessions/questions.py, src/database/queries/agent_question_queries.py, migration after e7a2b9c41d05, tables.py, both DB adapters, base.py as needed; src/sessions/transcripts/watcher.py, src/sessions/reconciler.py, src/orchestrator/core.py; new src/commands/question_commands.py, handler.py registration, tool definitions; tests/test_agent_questions.py and relevant backend tests. Do not edit Discord/CLI/dashboard or transcript readers.
**Consumes:** Task 1 turn_complete. **Produces:** all service, DB, command interfaces above.
- [ ] Write failing integration tests on real isolated SQLite DB for capture, replay dedup, routing, permission denial, and exact-session answer delivery (fake external provider only).
```python
await service.observe(session, [final_question])
await service.observe(session, [final_question])
rows = await db.list_agent_questions(session_id=session.id)
assert len(rows) == 1
assert rows[0]["state"] == "human"
assert (await service.answer(rows[0]["id"], "Approved", actor="supervisor", human=False)).get("error")
```
- [ ] Implement persistence with transactional unique turn/session identity and compare-and-set answers. Keep terminal questions pending without altering task status/claim; expire when session/task mismatch or new user turn occurs. Route routine-only questions to global supervisor with explicit CLI instructions and no general automatic permission grants. Five-minute timeout escalates to human.
- [ ] Wire watcher before stall evaluation so first recovered question cannot be killed on startup. Watcher sends newly read batch to observe; service must ignore old/replied questions when catching up after restart. Error in question observation must not drop transcript activity. Stall ladder skips only exact pending session/claim, without counter reset. Handle automatic nudge echoes separately from actual human answers if relevant.
- [ ] Deliver answers using original row+instance token and provider.nudge; deferred draft leaves pending, mismatches mark stale, delivered answer is not repeated next tick. Crash recovery and at-most-once logical answer acceptance required; document unavoidable process/I/O crash window.
- [ ] Register scoped commands: explicit server-owned identity check, DB lookup before project check, human-only guard, no arbitrary worker answer. Bound body size and reject blank input. Produce stable JSON for surfaces.
- [ ] Test restart replay, terminal reply resolution, tool-output question exclusion, no-completion commentary, approval vs routine, supervisor down/timeout, stale task/session, concurrent answers, deferred drafts, disabled messaging, and scoped commands. Run baseline affected tests. Report task-2-report.md.

### Task 3: Discord, CLI and flock surfaces
**Files:** new src/discord/agent_questions.py, notification_handler.py registration, bot.py persistent view registration if needed; src/cli/questions.py and registration; src/commands/agent_commands.py flock projection; dashboard AgentFlock types/components; tests/test_discord_agent_questions.py, CLI and dashboard tests. No core service/DB/parser edits.
**Consumes:** question service/DB/command/event interfaces above.
- [ ] Write failing tests for authorized Reply preserving question ID, unauthorized rejection, no requeue, missing-thread --to user project fallback, once-only notification, failed send remains pending, restored buttons after restart; CLI argument dispatch and Waiting for input state.
- [ ] Implement dedicated question card with literal question text, agent/task provenance, session reference and Reply button; validate authorization at button AND modal submission. Suppress mention pings from worker text. Resolve project channel via existing bot methods, not user-controlled channel IDs. Save actual sent IDs; persistent views must recover after process restart.
- [ ] Add `aq question list`, `aq question answer <id> --body TEXT`, `aq question escalate <id> --reason TEXT` through CLIClient.execute. No local privilege fallback when AQ_API_TOKEN present.
- [ ] Add `waiting_question` field to list_agents projection and show Waiting for input with question excerpt in flock. Keep actual worker BUSY state/claim untouched; preserve terminal open behavior. Surface human/queued answer state intelligibly.
- [ ] Fix generic user messages without discord thread: configured project channel fallback for session senders, preserve normal existing Discord conversation routing, persist or reuse delivery identity to prevent duplicate send+delivery events. Do not send unrelated system messages as parked warnings.
- [ ] Run focused tests, typecheck and build with existing shared dependencies. Report task-3-report.md.

### Task 4: Integration review, verification, deployment
**Files:** tests and compatibility fixes only where reviews identify failures; all branch diff.
- [ ] Review each deliverable against approved spec, then run combined backend tests and dashboard tests/build. Exercise actual native fixture through watcher/service with isolated storage and fake platform transport; verify question, human/supervisor answer, same session delivery, no repeated nudge.
- [ ] Validate migration head and empty upgrade on existing schema versus real migration on disposable DB. Never stamp/reset production DB.
- [ ] Review security, duplicate/race windows and restoration behavior. Commit approved source and integrate current main without losing concurrent work. Restart AQ only after migration/code checks and preserve live tmux/dashboard. Verify API health and captured pending question without fabricating human answers.
