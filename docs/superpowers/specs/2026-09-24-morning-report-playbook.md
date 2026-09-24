# Morning report playbook — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md) (the hourly sibling; this spec reuses its brief → supervisor → submit → outbox primitive),
[agent sleep/wake](2026-09-24-agent-sleep-wake.md), [wake context compaction](2026-09-24-wake-context-compaction.md),
[adversarial review recipe](2026-09-24-adversarial-review-recipe.md) (a possible source of "risky change" signals),
[Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md), [mobile dashboard](2026-09-24-mobile-dashboard.md) (where the full report is read);
existing: [Discord simplification §8](2026-09-08-discord-simplification-implementation.md),
[recent work and model attribution](2026-09-07-recent-work-and-model-attribution-design.md),
[delivery branch cleanup](2026-09-21-delivery-branch-cleanup-design.md), [CI main sentinel](2026-09-05-ci-main-sentinel-design.md).

## 1. The ask

Operator brief: a playbook fires **daily**, feeds the **overnight change list** (merged commits,
deliveries, completed tasks, failures) to the supervisor, which reports **what changed and what
you should test**. It uses a strong model through the agent (not a cheap model inline in the
playbook) and is **playbook-defined**, so any Agent Q operator can configure it — part of the
out-of-the-box positioning.

## 2. What exists today

**Daily triggers exist.** `src/timer_service.py` emits `cron.HH:MM` "once per local day at a
wall-clock time". Details that matter here:
- Local time is the **daemon host's** zone (`TimerService._now_local()` →
  `datetime.now().astimezone()`); there is no install/operator timezone setting in
  `src/config.py` (grep for `timezone`/`quiet_hours` finds nothing relevant).
- Fires "at-or-after the target": if not yet fired today and `now >= target`, it fires. A daemon
  started at 23:00 therefore fires a `cron.07:00` playbook **at 23:00** that day.
- Last-fired date is persisted to a state file (`_load_state`/`_save_state`); a lost file "causes
  at worst one extra same-day fire". The report needs its own durable dedup regardless.
- Timer/cron events carry `project_id: null` (`GLOBAL_EVENT_PREFIXES = ("timer.", "cron.")`,
  `src/playbooks/services.py`); a project-scoped playbook fires once globally, not per project.

**Shipped playbook patterns.** System defaults live in `src/prompts/default_playbooks/` with a
reviewed bundle per id in `src/prompts/reviewed_playbooks/<id>/` (`artifact.json`,
`manifest.md` with `capabilities_granted` and frozen `profiles_referenced` fingerprints).
`src/playbooks/required.py` distinguishes `REQUIRED_SYSTEM_PLAYBOOK_IDS` (readiness
requirement), `DEFAULT_SYSTEM_PLAYBOOK_IDS` (activated once on first start, never re-enabled
after an operator disables it — currently `supervisor-failure-triage`, `default-pipeline`)
and project-scoped reviewed bundles the operator imports by hand (e.g. `ci-main-sentinel`,
`scope: project:agent-queue`, `triggers: [timer.15m]`). Every shipped playbook is a thin rule:
one or a few contracted commands that own all logic (`provider-usage-probe.md`: "The playbook
holds no state and makes no decision").

**How a playbook can reach a strong model** (full analysis in the
[narrative spec §2](2026-09-24-supervisor-narrative-updates.md)):
- `CommandStep` → a notify command → a durable `messages` row to `supervisor-global` →
  `MessageDeliveryEngine` wakes the on_demand session (`src/messages/delivery.py`). Precedent:
  `provider_availability_notify` (`src/providers/availability_service.py:1369`).
- `LlmStep` `transport: "cli"` — one-shot, **tool-free**, schema-validated, through the logged-in
  CLI (`src/playbooks/executors/llm.py:299`, `src/llm/cli.py`).
- `AgentTaskStep` — creates a task for a **worker** profile; the `supervisor` profile is refused
  (`src/profiles/task_execution.py`).
- `wait` on `event` (e.g. `message.replied`, which carries `body`; `src/event_schemas.py:702`).

**Change-list sources (durable evidence).**
- `task_completion_records` (`src/database/tables.py:974`): per close, `outcome`,
  `work_outcome`, `summary`, **`changes`** (defaults to summary), **`verification`**,
  **`tests`**, `commands`, `branch`, `commits`, `pr_url`, `deliverables`, `completed_at`
  (indexed). Written by `task_close` (`src/commands/session_commands.py:~1188`). This is the
  richest "what changed / how was it checked" source we have.
- `development_deliveries` (`tables.py:4283`): per landing, `state` ∈
  prepared/publishing/**delivered**/parked/**adopted**/cancelled, `target_ref`, `manifest`
  (member `task_id`s), `evidence` (head sha, `branch_cleanup`). Only for projects on the
  development-integration path; PR-mode projects expose `pr_url` on completions. Whether a PR's
  *merge* is recorded durably is **unverified**.
- Failures: `task.failed` → `supervisor-failure-triage` incidents
  (`supervisor_recovery_incident` task metadata; `src/database/queries/task_recovery_queries.py`),
  escalations (`list_escalations`, `src/database/queries/escalation_queries.py:189`),
  `task_session_attempts.end_reason/outcome`.
- Fleet: `provider_availability_transitions`, `task_reroutes` (already read as digest facts in
  `collect_digest_activity`, `src/database/queries/digest_queries.py:166`).
- Overnight digests: `digest_windows` rows with `payload.reported_keys`.
- Recent-work read: `list_recent_task_activity` (`src/database/queries/activity_queries.py:63`),
  command `task_recent_activity` — models per attempt; broad "touched" definition.
- CI: `ci_baseline_status` (`src/commands/ci_commands.py`) — per project, via `gh`.
- **Git:** the only log command is `git_log` (`src/plugins/internal/git.py:1116`), which returns
  `git log --oneline -N` (`GitManager.aget_recent_commits`, `src/git/manager.py:5316`) on the
  project checkout — count-based, no `--since`, no range, no diffstat. The supervisor profile
  has `Bash` but `needs_workspace: false`; whether it can reliably reach a checkout to run git
  itself is **unverified** (the design's read-only project dir is not in the current profile).

**Outbound Discord** is daemon-owned only (escalations, digest, `ReviewNotifier`), all through
`DiscordEscalationTransport` (`post_root`, `ensure_thread`, `post_thread_message`,
`find_marker`). The supervisor cannot post.

## 3. Gaps

1. No "change list since X" read spanning completions, deliveries, failures, escalations and
   git; no git range/diffstat command.
2. No durable report record or watermark (what the last report covered).
3. No supervisor → Discord path (shared with the narrative spec).
4. No timezone setting; `cron.*` is host-local and fires late after a late start.
5. No notion of "user-visible change" or "needs manual testing" anywhere; completion records
   carry `verification`/`tests` as free text.
6. No place for a long report: Discord caps a message at 2,000 chars, the digest targets 1,200.

## 4. Implementation options

All options share a deterministic front half, a command (working name
`morning_report_brief`) that:
- computes the window from a **durable watermark** (end of the last *sent* report), not from the
  clock: `since = last_report.until` (bounded by `max_lookback_hours`, e.g. 72 so a Monday
  report covers the weekend), `until = now`;
- reserves a report row keyed `(destination, report_kind='morning', local_date)` — the same
  reserve-before-send idea as `digest_windows` — so a double cron fire, a restart or a replayed
  event yields one report per local day;
- builds the change list per project: completions (with `summary`/`changes`/`verification`/
  `tests`/`pr_url`/`commits`), deliveries landed on the default branch, failures and open
  incidents/escalations, CI verdict where available, provider outages; plus git commits on the
  default branch in the range (new range-capable read, e.g. `git log --first-parent
  <since_sha>..<head> --stat` — the `since_sha` can be stored on the report row, making the
  watermark exact rather than time-based);
- persists the brief on the row, and ends silently if nothing changed (`suppression_reason`
  `no_changes`, still durable — "nothing happened overnight" might itself be worth one line; Q7).

### Option A — Supervisor session authors, daemon delivers (hub model)

Playbook `morning-report` (`triggers: [cron.07:00]`): (1) `morning_report_brief` → `brief`;
`none` ends. (2) `morning_report_request(report_id)` writes one message to `supervisor-global`
(`body_kind="morning_report"`, idempotent on report id) with the brief and the output contract.
The supervisor may dig (`aq task explain`, comments, `session_logs`, vault specs), writes the
full report into the vault (`vault/projects/<pid>/notes/morning-<date>.md` or a fleet path)
and calls `morning_report_submit(report_id, summary, checklist[], vault_path)`. A daemon outbox
(pump modelled on `DigestScheduleService.pump`) posts a root message (summary ≤~1,500 chars) +
optional thread with the checklist, with marker reconciliation and escalation priority. If no
submit arrives by `deadline` (e.g. 45 min), the deterministic change list is posted instead.

- Touches: new commands + contracts, new outbox table or generalised `digest_windows` (Q10),
  git range read, supervisor profile grants, reviewed bundle, config.
- Pros: exactly the brief ("through the agent", "supervisor is the hub"); the supervisor can
  investigate and ties the report to what the operator told it; the report lands in the vault
  (dashboard- and memory-visible).
- Cons: one deep-high resumed wake per day (cheap at daily cadence, but adds to context — see
  compaction); tool-using, so less predictable output; needs the new outbox.
- Size: L (M if the narrative spec's request/submit/outbox lands first).

### Option B — One-shot CLI `LlmStep` authors (no session)

Same brief; step 2 is `LlmStep transport: cli` with a deep-class profile, `output_schema`
`{summary, sections[{project, changed[], test[{what, why, how, refs[]}], risks[]}]}`, a hard
`AiBudget`; step 3 `morning_report_submit`.

- Pros: predictable, schema-validated, budgeted; no session; strong model on subscription.
- Cons: tool-free — cannot open a diff or a transcript beyond what the brief carries, so the
  brief must include diffstats/excerpts (prompt size); not "the supervisor"; nothing enters the
  hub's memory.
- Size: M on top of the shared front half.

### Option C — A worker task writes the report (`AgentTaskStep`)

Playbook creates a task for a read-only worker profile (new `reporter` rung, `lifecycle: task`)
with a `readonly-dir`/`project-repo` workspace, so it can run `git diff`/`git log`, read the
actual code and write a vault report; the playbook waits on the child (`ChildTaskReconciler`)
then submits.

- Pros: deepest "what to test" (real diffs, can even run a smoke command); uses the existing,
  well-guarded task path and budgets.
- Cons: consumes a worker slot and a workspace lock every morning; per-project fan-out means N
  tasks; heavy for a status report; not the supervisor.
- Size: M–L.

### Option D — Supervisor's own harness scheduler

The supervisor already sets up a recurring patrol with its harness scheduler
(`SUPERVISOR_PATROL_PROMPT`, `src/sessions/spec.py:144`, "CronCreate … every ~15 minutes").
A morning job could live there.

- Pros: zero daemon code.
- Cons: not playbook-defined, not durable ("The patrol only lives as long as this session"),
  harness-specific, no dedup, no fallback, still no way to post. **Rejected**; listed because it
  is the path of least resistance someone will try.

## 5. Initial take

Provisionally **Option A**, built on the same request/submit/outbox primitive as the hourly
narrative, with **Option B** selectable as the author (a playbook variant) for installs that do
not want a daily supervisor wake. Reasons: the brief explicitly wants the supervisor as hub; a
daily cadence makes session cost a non-issue compared with hourly; the deterministic front half
(watermark, reserve, change list, fallback) is where correctness lives and is identical for A
and B. Option C is worth revisiting only if "what to test" proves too shallow without real
diffs — and the git range read in the front half may be enough.

**"What to test" — provisional definition.** A manual-QA checklist, each item:
`{what to try (user-facing action), why (which change), where (surface: dashboard page / CLI
command / API / Discord), refs (task id, commit, PR, spec), confidence (agent-verified vs
unverified)}`. Derivation, deterministic first, model second:
1. Keep only completions with code (`no_code` false; `work_outcome` not no-op) that landed
   (delivered/adopted delivery or merged PR) — work still on a branch is "pending", not "test".
2. Classify touched paths into surfaces with a per-project **surface map** (vault file, e.g.
   `vault/projects/<pid>/surfaces.md`: `dashboard/** → dashboard`, `src/cli/** → CLI`,
   `src/api/** → API`, `migrations/** → upgrade`). Internal-only changes go to "changed",
   not "test".
3. Use the completion's `verification`/`tests` to mark what an agent already verified; the
   checklist emphasises what nobody verified by hand.
4. The model turns (1–3) into plain-language steps and flags risk (migrations, config changes,
   auth, anything the [adversarial review](2026-09-24-adversarial-review-recipe.md) flagged).

**Fleet vs per-project.** One fleet report (one Discord post, per-project sections, projects
with no changes omitted), honouring `discord.digest.project_ids` as the destination's
visibility. Per-project reports are possible by copying the playbook with a project scope and a
`project_id` input, since cron events are global.

**Timezone.** Add an explicit report timezone (`reports.timezone`, IANA name, default = host
local) used for the report's `local_date` key and the Discord label. Leave `cron.HH:MM`
host-local for now (Q3). Add a `late_start_cutoff` so a cron that fires far after its target
(the 23:00 case) either skips or relabels ("daily report" instead of "morning report").

**Configurability.** Playbook-defined: the trigger (`cron.07:00`) *is* the schedule; the
command inputs carry `max_lookback_hours`, `deadline_minutes`, project selection and author
choice. Ship as a reviewed system bundle; default-on vs opt-in is Q1.

## 6. Open questions

1. **Default-on or opt-in?** Add to `DEFAULT_SYSTEM_PLAYBOOK_IDS` (every install gets it on first
   start; disabling sticks) or ship as a bundle the operator activates? Decides the
   out-of-the-box story vs surprise supervisor wakes on installs without Discord.
2. **How do operators change the time?** Editing a reviewed playbook means re-import and a new
   artifact sha (`aq playbook v2-import` + `activate --artifact-sha256`). Is that acceptable, or
   do we need playbook-level parameters/overrides (e.g. an activation-time input
   `schedule: "07:00"`) so a time change is not a re-review? This likely affects every
   configurable shipped playbook, not just this one.
3. **Timezone source.** Install-level `reports.timezone`, per-operator (who is "you"?), or make
   `TimerService` cron tz-aware (`cron.07:00@Europe/London`)? The last changes a shared
   primitive and its persisted state format.
4. **Watermark: time or commit?** Time-based `since = last.until` is uniform across sources;
   commit-based (`since_sha` per project default branch) is exact for git but undefined for
   non-git facts. Probably both on the row — confirm.
5. **"Merged" for PR-mode projects.** Is a PR merge durably recorded anywhere (unverified), or
   must the brief ask `gh` at report time? Decides whether PR-mode installs get a truthful
   "landed" list.
6. **Git access.** Does the change list come from a new daemon-side range read (proposed), or
   do we give the supervisor a read-only checkout so it runs git itself? Affects Option A's depth
   and the supervisor's workspace model (`needs_workspace: false` today).
7. **Nothing changed** — post "quiet night, nothing landed" or stay silent? §8's digest stays
   silent; a morning report arguably should confirm the system is alive.
8. **Failures section depth.** Just counts + links, or the supervisor's triage verdicts for each
   overnight incident? The latter duplicates escalations; the former may bury a real problem.
9. **Where does the full report live?** Vault note (supervisor writes only to the vault today),
   a dashboard view, or both? Discord gets a summary + link; decides whether a dashboard page is
   in scope (see [mobile dashboard](2026-09-24-mobile-dashboard.md)).
10. **Generalise the outbox?** Add a `kind` to `digest_windows` (its unique key
    `destination, config_generation, window_start, window_end` would need `kind`), or a new
    `supervisor_reports` table shared by the hourly narrative and this report? One pump vs two.
11. **Relation to the hourly narrative.** Should the morning report suppress/absorb overnight
    hourly narratives (quiet hours in the narrative spec), making the morning report the
    catch-up for the night?
12. **Surface map ownership.** Hand-authored vault file per project, inferred by the model each
    time, or seeded by project onboarding (`2026-09-03-project-onboarding-design.md`)?
13. **Should the checklist become work?** E.g. an optional human gate/subtask list "QA the
    overnight changes" the operator can tick off in the dashboard (subtasks exist:
    `task_subtasks`). Useful, but turns a report into queue state.

## 7. Dependencies and sequencing

1. **Shared with the [hourly narrative](2026-09-24-supervisor-narrative-updates.md):** the
   request/submit command shape, message `body_kind`, sanitisation of supervisor text, and the
   outbox/pump (Q10). Build that primitive once; whichever feature lands second is S–M.
2. **[Agent sleep/wake](2026-09-24-agent-sleep-wake.md):** not required (a daily wake is fine),
   but the report request should use the same wake API.
3. **[Wake context compaction](2026-09-24-wake-context-compaction.md):** the report brief is
   large; it should be `archive_after_inject` or otherwise not pinned in the resumed context.
4. **Suggested order:** (a) change-list read + git range read, exposed first as a dry-run
   command (`aq report morning --dry-run`) so operators can judge the raw list; (b) report row +
   watermark + deterministic delivery (useful on its own); (c) supervisor request/submit
   (Option A) and CLI variant (Option B); (d) surface map + checklist; (e) timezone.
5. **[Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md)** for a link that opens
   from a phone; **[mobile dashboard](2026-09-24-mobile-dashboard.md)** if the full report gets a
   dashboard page.

## 8. Non-goals

- Running tests or QA automatically as part of the report (that is CI and the sentinel's job).
- A general-purpose report scheduler / BI layer; one daily report kind, playbook-triggered.
- Changing `cron.*` semantics for existing playbooks (any tz extension must be additive).
- Letting the supervisor post to Discord directly.
- Replacing escalations: a failure needing a human decision still escalates immediately; the
  report only references it.
