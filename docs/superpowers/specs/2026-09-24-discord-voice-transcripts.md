# Discord voice transcripts parsed by the supervisor — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [Discord mention routing](2026-09-24-discord-mention-routing.md) (prerequisite),
[exclusive job queue](2026-09-24-exclusive-job-queue.md) (if transcription runs locally),
[morning report playbook](2026-09-24-morning-report-playbook.md),
[Discord simplification](2026-09-08-discord-simplification-implementation.md),
`docs/guides/escalations.md`

## 1. The ask

> Inbound: voice transcripts dropped in Discord get parsed by the supervisor. Nearly free once
> mention routing works.

**Interpretation (to be confirmed, Q1):** the operator records thoughts away from the desk —
a phone dictation, a voice memo, or Discord's own mobile "voice message" — and drops the
result into the configured channel. The supervisor reads it and turns it into work: new tasks,
edits/comments on existing tasks, notes, or answers to open escalations. "Nearly free" holds
only for the *text* case; audio needs a transcription step that does not exist today.

Like mention routing, this widens Discord's inbound surface beyond the escalation-reply-only
contract of the 2026-09-08 simplification, and a transcript is a *bulk* instruction: one
message may imply a dozen changes. The guardrails below matter more than the parsing.

## 2. What exists today

- **Inbound Discord reads only `message.content`.** `DiscordEscalationIntake.observe`
  (`src/discord/escalation_intake.py:51-68`) never looks at `message.attachments`,
  `message.flags` or `message.reference`; `grep -i attachment src/discord src/escalations`
  finds nothing. Anything not a thread reply to a bound escalation is dropped silently
  (full trace in [mention routing §2.1](2026-09-24-discord-mention-routing.md)).
- **Reply length caps.** Escalation replies are capped at 16,000 chars
  (`MAX_REPLY_CHARS`, `src/escalations/intake.py:40`; mirrored in
  `accept_escalation_reply`, `src/database/queries/escalation_queries.py:335`). Discord itself
  caps a message at 2,000 chars for non-Nitro accounts; the desktop client offers to turn a
  longer paste into a `message.txt` **attachment** (mobile behaviour unverified). So even the
  "text only" case needs `.txt` attachment ingestion for any memo longer than ~300 words.
- **Discord library.** `discord.py>=2.5.2,<2.6` (`pyproject.toml:21`). `discord.Attachment`
  exposes `filename`, `size`, `content_type`, `read()`, and (believed since 2.3)
  `is_voice_message()` / `duration` for native voice messages — **verify against 2.5**.
  The bot requests the privileged `message_content` intent (`src/discord/bot.py:31`), which
  also governs attachment visibility on messages that do not mention the bot.
- **No speech-to-text anywhere.** No whisper/STT dependency; `src/llm/` (`LLMClient.complete`
  / `run_tools`, `src/llm/client.py:271-284`) has anthropic/google/openai providers and no
  audio call (`grep -i audio src/llm/providers/openai.py` is empty). The `openai` and
  `google` extras exist (`pyproject.toml:119-145`), so an API transcription path would add
  code, not a new package; a local model (faster-whisper, whisper.cpp) would add a package
  and CPU/GPU load.
- **A propose → human-confirm → commit pipeline already exists.**
  `task_batch_propose` (`src/commands/proposal_commands.py:119-158`) validates a batch of
  tasks + edges, persists it and emits `proposal.ready`; the system playbook
  `src/prompts/default_playbooks/default-pipeline.md` (rules `proposal-ready-gate`,
  `proposal-commit`) opens a human gate "Approve task batch?" and commits on a `gate.resolved`
  with resolution `approve`/`approved` (`APPROVAL_RESOLUTIONS`, `proposal_commands.py:33`);
  `task_batch_commit` is atomic and replay-safe (`proposal_commands.py:295+`).
  `task_batch_propose` requires a `project_id`.
- **Approving from Discord is plausible through escalations.** `escalation_apply_reply`
  supports `action_kind="gate_resolve"` for an escalation whose `source_kind` is `gate` and
  whose gate is in the same project (`src/commands/escalation_commands.py:411-440`,
  `490-499`), resolving with the human reply's text as the resolution. Whether a gate
  resolved this way (`resolved_by` = the supervisor principal) satisfies
  `_proposal_approval_error`'s "human gate" check is **unverified**.
- **Other sinks.** `aq-notes` plugin (`src/plugins/internal/notes.py`: `write_note`,
  `append_note`, `promote_note`), task comments (`task_comment_commands.py`), subtasks,
  and the external `aq-memory` plugin.
- **A non-Discord inbound exists.** `aq-inbox` (`src/plugins/internal/inbox/`) polls Gmail,
  authenticates SPF/DKIM + an allowlist, and emits `email.received.allowlisted` for playbooks —
  emailing a transcript is already a (project-scoped) route with better sender authentication
  than a chat message.

## 3. Gaps

1. No inbound route at all until [mention routing](2026-09-24-discord-mention-routing.md) lands.
2. No attachment ingestion (text or audio), size limits, content-type allowlist, or storage.
3. No transcription capability, provider choice, cost accounting or resource gating for it.
4. No "transcript" shape for the supervisor: nothing tells it to split one memo into
   per-project proposals, notes and comments, or how to report back what it understood.
5. No Discord-side confirmation UX for a proposal (the gate lives in the dashboard; the
   escalation route to approve it is unproven).
6. No retention/privacy policy for raw audio or transcript text.

## 4. Implementation options

### Option A — Text transcripts only (operator transcribes on the phone)

*Sketch.* Extend the mention intake to accept the message text **plus** `text/plain`
attachments (`.txt`, `.md`) up to a byte cap (e.g. 64 KB, then truncate with a note), read
via `Attachment.read()`, decoded as UTF-8 with replacement. The body goes to the supervisor
with `body_kind="discord_transcript"` and the Discord message ID as identity. Supervisor
instructions (prime / global supervisor role) describe how to handle a transcript.
*Touches.* The mention intake module, supervisor instructions, config caps.
*Pros.* Truly near-free after mention routing; no new dependency, no audio privacy question;
phone OS dictation is good enough for most memos. *Cons.* Operator does the transcription;
Discord native voice messages are unsupported. *Size:* S (after mention routing).

### Option B — Audio attachments + server-side transcription

*Sketch.* Accept `audio/*` attachments and native voice messages up to a duration/size cap;
download to a temp path; transcribe via (B1) a provider API — OpenAI transcription or Gemini
multimodal — added to `src/llm/` as `LLMClient.transcribe` with the provider chosen by an
intelligence-class-like config; or (B2) a local model run as a gated job (resource slot via
`src/resources/`, or the [exclusive job queue](2026-09-24-exclusive-job-queue.md)). Post the
transcript back in the thread so the operator can see what was heard, then continue as A.
*Touches.* `src/llm/` (new capability), config (`transcription:` section, cost cap), intake,
temp-file handling, possibly `pyproject.toml` (B2), doctor check for the provider.
*Pros.* Zero-friction capture. *Cons.* New cost and a third-party data flow for raw voice
(B1), or heavy CPU/GPU on a box that already gates test runs (B2); audio is a new
prompt-injection carrier; more failure modes (format, silence, language). *Size:* M (B1) /
L (B2).

### Option C — Transcript → triage proposal playbook, human confirms

*Sketch.* Independent of how text arrives (A or B). A playbook (or supervisor procedure)
consumes the transcript and produces, per project, a `task_batch_propose` (tasks + edges)
plus a list of non-task items (notes to append, comments on named tasks, questions). The
supervisor posts a compact summary into the Discord thread ("3 tasks for agent-queue, 1 note,
1 question — reply `approve` or edit in the dashboard"). Approval: the existing dashboard gate,
or a Discord reply applied through `escalation_apply_reply(gate_resolve)` if Q5 proves it
sound. Notes/comments could be applied immediately (low risk) or held with the batch.
*Touches.* A new project/system playbook or supervisor skill, maybe an LLM direct-path node
(`src/llm/`), the Discord reply path from mention routing, possibly the escalation
gate-resolve binding.
*Pros.* Reuses the only reviewed human-approval path for bulk task creation; an operator can
correct misheard items before anything exists; replay-safe commit. *Cons.* Round trip before
anything happens; the proposal model is per project so multi-project memos fan out into
several gates. *Size:* M.

### Option D — Route transcripts by email instead

Point the operator at `aq-inbox` (email the memo to the project inbox) and add a playbook on
`email.received.allowlisted`. *Pros.* No Discord surface change; DKIM-authenticated sender.
*Cons.* Project-scoped (one inbox per project), Gmail-only, not what was asked. *Size:* S–M.

## 5. Initial take

Provisional: **A + C first, B1 behind an opt-in flag later.** Text and `.txt` attachments
through the mention route, with the supervisor always **proposing** (via `task_batch_propose`)
rather than creating tasks from a transcript; low-risk sinks (a note, a comment on a named
task) may apply directly if Q3 says so. Audio is a separate decision because it adds cost and
a new place raw voice goes; phone dictation covers the need meanwhile. B2 (local model) only
if the operator rules out sending audio to a provider.

Guardrails (proposed):
- Same identity, allowlist (fail closed), channel and rate limits as mention routing; the
  transcript is an operator instruction, **not** human evidence for any escalation action.
- Attachment allowlist by `content_type` *and* extension; hard caps on bytes (text) and
  duration/bytes (audio); refuse archives, images and anything executable; never follow URLs
  inside a transcript.
- Nothing destructive from a transcript: no delete/archive/cancel/config changes; those need
  the dashboard or an explicit follow-up.
- Echo back what was understood (and, for audio, the transcript) before or with the proposal,
  so mis-hearing is visible.
- Raw audio is deleted after transcription unless retention is configured; transcript text
  lives in the supervisor `messages` row like any chat turn.

## 6. Open questions

1. **What does the operator actually drop?** Pasted phone dictation, `.txt` exports (Otter,
   Apple voice memo transcripts), or Discord native voice messages? Decides whether B is in
   scope at all.
2. **Confirmation policy.** Always propose-then-confirm, or allow direct task creation below
   a threshold (e.g. one task, one project)? Decides whether C is mandatory.
3. **Which outputs are in scope?** Tasks only, or also notes, task comments, subtask edits,
   memory facts and escalation answers? Each extra sink needs its own "safe to apply without
   confirmation" ruling.
4. **Project resolution.** How does the supervisor pick the project(s) for a memo that names
   none or several — ask back, default project, or per-project proposals?
5. **Discord approval.** Is approving a proposal by a Discord reply acceptable, and does the
   `escalation_apply_reply(gate_resolve)` path satisfy `_proposal_approval_error` (a
   supervisor-principal resolver of a `human` gate)? If not, approval stays dashboard-only.
6. **Transcription provider and privacy (if B).** API (which provider, which data-retention
   terms) vs local model; per-month cost cap; language support; who may enable it.
7. **Size limits.** Text cap, audio duration cap, and what happens above them (truncate,
   refuse with a message, or summarise first).
8. **Retention.** Keep audio? Keep transcripts beyond the message history? Should transcripts
   be excluded from reflection/memory extraction by default?
9. **Prompt injection.** A transcript may quote other people or pasted text. Does the
   supervisor treat quoted content as data, and should the proposal summary flag
   instructions it chose to ignore?
10. **Idempotency on edits.** If the operator edits a transcript message, is that a new
    instruction, an amendment, or ignored (mention routing currently ignores edits)?

## 7. Dependencies and sequencing

1. [Mention routing](2026-09-24-discord-mention-routing.md) — hard prerequisite (identity,
   intake, thread replies, outbox).
2. Option A (text + `.txt`) — small follow-on.
3. Option C (proposal round trip) — depends on answering Q2/Q5; may need a small change to
   the escalation gate-resolve binding.
4. Option B — independent track; B2 depends on resource gating or the
   [exclusive job queue](2026-09-24-exclusive-job-queue.md).
- [Tailscale link fix](2026-09-24-tailscale-dashboard-link.md) matters for "edit in the
  dashboard" links in the proposal summary.

## 8. Non-goals

- Live voice-channel listening or real-time speech (Discord voice gateway).
- Speaker identification or transcribing people other than the operator.
- Replacing the dashboard's proposal/gate UI.
- A general file-upload surface in Discord.
