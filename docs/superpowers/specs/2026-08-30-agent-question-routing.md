# Agent question routing — approved design

User approved supervisor-first handling with Discord escalation on 2026-08-30.

Capture completed assistant turns containing questions from native transcripts, without terminal scraping. Fix the confirmed Codex v0.151 format gap: response_item/message holds visible commentary/final_answer, task_complete carries last_agent_message, legacy event_msg/agent_message remains supported. Do not double-render duplicate events or expose reasoning/system prompts. Claude end_turn must work too.

Persist each question by exact session instance and turn identity. Only active task/pool claims generate automatic requests; named supervisor and freeform user terminals do not recursively escalate. A pending question suspends stall recovery without changing existing stall counters, task assignment, branch, or workspace. Display Waiting for input in the flock while retaining the task claim.

Routine factual/implementation questions go to the existing global supervisor through the message queue, with question-answer/escalate commands. Requests for human approval, scope/design decisions, access/security changes, destructive or external actions go directly to the human; ambiguous requests default to human. Supervisor cannot answer human-only questions even with elevated scope. Escalate supervisor questions after five minutes without an answer or when unavailable. Preserve original text and provenance; treat it as untrusted worker content.

Discord sends a dedicated question card to the configured project channel (global configured channel fallback), including agent/task and a Reply button. Only authorized Discord users can answer. Persist Discord message/channel identity so retries/restarts can recover controls and avoid routine duplicates. Missing Discord must leave a visible pending question, not mark it delivered. Existing generic --to user notifications without Discord thread IDs must reach their project channel once, without changing existing conversation replies.

Answers are durable and delivered to the original session id, task id, and instance token, only while the claim still matches. Use provider.nudge with its draft/input guard; no automatic draft clearing, no session-by-name rebinding. Stale or duplicate replies cannot affect a replacement session. A user replying directly in the terminal resolves the old question, and a new final question starts a new record. Do not requeue/restart the task to answer. Do not modify previous denied explicit-restart stall-counter reset.

Verification: isolated SQLite and, where available, disposable Postgres; native transcript fixtures, replay/restart, permission checks, duplicate/late replies, stale sessions, terminal drafts, Discord failure/retry, supervisor timeout, and dashboard waiting state. Never test by sending real Discord messages or disrupting workers. Preserve all unrelated work.
