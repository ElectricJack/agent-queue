---
tags: [discord, migration, operations, runbook]
---

# Discord simplification: migration and rollout runbook

This is the operator procedure for moving an existing install from the old
Discord surface — per-project channels, per-task execution threads, gate and
task buttons, the `question_answer` modal and six slash commands — to the
simplified one: **one shared channel, an hourly activity digest, and one
escalation thread per human decision**.

Read [Discord notifications](discord-commands.md) for what the end state looks
like, [Durable human escalations](escalations.md) for the model underneath it,
and the [replacement capability checklist](discord-replacement-checklist.md) for
the surface-by-surface ledger this cutover works from. Implementation spec: §10
of `docs/superpowers/specs/2026-09-08-discord-simplification-implementation.md`.

Nothing here deletes a Discord channel, post or thread. The migration makes old
bot-owned cards *inert* and moves the work identities into core state; the
history stays exactly where it is.

## 0. There is no runtime legacy mode

The spec allowed a temporary explicit legacy/simplified switch during
development. It was never needed and does not exist: the legacy notification
consumer, the control views and the slash commands were removed in one change,
so a daemon running this code is always in simplified mode. The practical
consequences:

- There is no `discord.mode` setting to flip, and no half-migrated state where
  both consumers run.
- The blast radius you can control at runtime is enablement, not behaviour —
  `discord.digest.enabled` and `discord.escalation.enabled` (see §6).
- A true rollback is a redeploy of a pre-cutover release, not a setting.

## 1. Before you upgrade

Take five minutes and write down what the old install had, because the
migration reads it and you may have to arbitrate:

```bash
aq system get-config --section discord     # current destinations and allowlist
aq question list                           # pending worker questions
aq playbook list-runs --status paused      # runs parked on a human gate
```

Note in particular:

- every channel named by `channels.control`, `channels.notifications` and
  `channels.agent_questions` — these become one-way *migration inventory*, not
  settings;
- whether `per_project_channels` was enabled — it is ignored from now on and
  produces a warning;
- the `authorized_users` list — it is preserved verbatim and is still the
  allowlist for escalation replies.

Pick the one channel the installation will use. Every selected project shares
it and every item names its project.

## 2. Configure the shared destination

```yaml
discord:
  bot_token: "…"          # unchanged
  guild_id: "…"           # unchanged
  channel_id: "123456789012345678"   # numeric ID, not a name
  authorized_users: ["234567890123456789"]
  digest:
    enabled: true
    interval_minutes: 60      # 15–1440
    project_ids: []           # empty = every project this destination can see
    categories: [work, vcs, budget, system]
    catchup_hours: 24         # 1–168
  escalation:
    enabled: true
    mention_user_ids: []
    mention_role_ids: []
    reminder_minutes: 0                        # 0 = disabled
    supervisor_delivery_timeout_minutes: 15    # watchdog
```

`channel_id` is a Discord snowflake (17–20 digits), never a channel name: the
single-channel model binds durable delivery to an ID that survives a rename.
Settings are validated by the config schema and by the dashboard's
Settings → Messaging panel, so a bad interval or a channel *name* is refused at
save time rather than at send time.

Mentions are bounded and explicit. Only the IDs in `mention_user_ids` /
`mention_role_ids` are ever pinged, and only on an escalation root post.
Digests never mention anybody, including text that merely looks like a mention.

## 3. What the cutover pass does at startup

The bot runs one idempotent migration pass in `on_ready`, **before** inbound
routing goes live (`src/discord/cutover.py`). It:

1. resolves the destination — see §4;
2. inventories pending worker questions and open human gates, and the
   historical task threads recorded for them;
3. creates the missing transport-neutral escalation identities, preserving the
   original question/gate source identity and its human-required flag;
4. preserves already-accepted answers as accepted — they are never delivered a
   second time;
5. adopts a compatible bot-owned root that is already in the selected channel
   instead of posting a duplicate human request;
6. edits old bot-owned cards to `view=None` so a pre-cutover button cannot
   still mutate a task, and records the old message/thread mappings;
7. marks historical task threads routing-inert.

Every write has a durable unique key, so a crash or a restart mid-pass simply
repeats the pass safely. Until the pass reports `complete`, the gateway accepts
no inbound message at all.

Read the result:

```bash
aq --json digest status | jq .cutover
```

```json
{
  "status": "complete",
  "channel_id": "123456789012345678",
  "migrated_questions": 2,
  "migrated_gates": 1,
  "accepted_answers_preserved": 4,
  "adopted_roots": 1,
  "retired_task_threads": 7,
  "inert_messages": 12,
  "conflicts": []
}
```

`status: "complete"` is the only state in which replies are accepted.
`needs_configuration` and `failed` both keep inbound routing closed and put the
reason in `conflicts` and in `digest status`'s `warnings`.

## 4. Resolving a channel-selection conflict

The migration never picks a channel for you when the answer is ambiguous.

| Situation | Result | What to do |
|---|---|---|
| `channel_id` is set | Used as-is | Nothing |
| Not set; exactly one legacy destination name, resolving to exactly one channel in the guild | Promoted to that channel's ID **in memory** for this run | Save the resolved `channel_id` so it survives a restart |
| Not set; legacy names disagree | `needs_configuration`, conflict `legacy destinations disagree: …` | Choose one and set `channel_id` |
| Not set; the legacy name matches no channel, or several | `needs_configuration`, conflict names it | Set `channel_id` explicitly |
| Not set; no legacy names either | `needs_configuration`, `discord.channel_id is not configured` | Set `channel_id` |

Two more conflicts are permission-shaped rather than naming-shaped:
`could not clear legacy question view <id>` and `could not clear legacy gate
view <id>` mean the bot cannot edit that old message. The view still has no
callback after a restart, so it cannot mutate anything — but grant the bot
*Manage Messages* on the old channel and restart if you want the card visibly
cleaned up.

Fix the setting, restart the daemon, and check `digest status` again. The pass
re-runs from scratch and re-converges; it does not need a clean slate.

## 5. Verify before you trust it

Nothing in this list sends a message or advances a delivery cursor.

```bash
# Schedule, next evaluation, recent windows, delivery health, cutover report.
aq --json digest status

# Dry-run the current window: would_send, suppression_reason, the exact text.
aq --json digest preview

# The incidents that migrated, plus anything opened since.
aq escalation list --states needs_human,reply_received
```

What to look at:

- `settings_errors` empty and `warnings` empty (or only the ones you chose —
  disabling either feature is reported as a warning on purpose).
- `next_evaluation_at` in the future and `digest.interval_minutes` as intended.
- `delivery_health` — counts of digest windows in an attention state.
- `pending_escalation_deliveries` — outbox rows still `pending`, `sending`,
  `retry` or **`unknown`**. `unknown` is the honest state after an ambiguous
  external send: it means the sender could not confirm whether the post landed,
  and it wants a human to look rather than a blind repost.
- `digest preview`'s `suppression_reason` on a quiet hour. Silence is a
  feature — idle, queued-only, blocked-only and container-only windows are all
  *supposed* to send nothing — and the preview tells you which of those it is,
  so you never have to guess whether the bot is broken.

The dashboard shows the same three things (Settings → Messaging): validated
settings, the suppression-reason preview with the next evaluation, and the
escalation inbox with pending/unknown/failed delivery.

## 6. Safe rollout, and rollback

Roll out in this order; each step is independently reversible.

1. **Escalations only.** `digest.enabled: false`, `escalation.enabled: true`.
   The channel stays quiet except for real human decisions — the highest-value,
   lowest-volume traffic. Watch a few incidents through
   post → thread → reply → supervisor recovery → resolution edit.
2. **Add the digest.** Set `digest.enabled: true`. Confirm with
   `aq digest preview` *before* the first boundary that the text and project
   scope are what you expect.
3. **Narrow or widen scope.** `digest.project_ids` and `digest.categories`
   change what the digest is allowed to read at all, so a filtered-out project
   can never leak into another's summary.

Rollback, in increasing order of severity:

- **Silence the channel.** Set `digest.enabled: false` and/or
  `escalation.enabled: false`. Escalation incidents are still created, the
  owning supervisor is still notified, and the dashboard inbox still works —
  only the external post stops. This is the rollback that is always safe.
- **Repoint the destination.** Changing `channel_id` starts a new digest
  schedule generation at the change time. It does **not** replay old windows or
  unsent historical summaries into the new channel. Escalations already
  delivered keep their durable delivery identity bound to the old
  channel/thread IDs; move an open incident deliberately rather than expecting
  a config change to re-home it.
- **Redeploy a pre-cutover release.** Supported, with one rule: the old direct-
  action views must not come back over conversations that have already migrated.
  The old cards were edited to `view=None` and the old code will not restore a
  view onto a message it no longer has state for, so a downgraded daemon posts
  new legacy cards but cannot re-arm the migrated ones. Escalations created in
  the meantime stay in the database, remain visible in the dashboard, and are
  picked up again when you roll forward.

Changing the interval or the catch-up horizon is not a rollback risk: after a
restart or a prolonged outage the scheduler coalesces missed windows into at
most **one** labelled catch-up digest over `catchup_hours`, never one message
per missed hour.

## 7. Housekeeping is explicit, never automatic

Nothing in the cutover deletes Discord content, and no scheduled job cleans up
old task threads. If you want the old per-project channels tidied, that is a
deliberate operator action:

```bash
aq discord purge-channel --channel-id <id>              # dry run: counts only
aq discord purge-channel --channel-id <id> --confirm     # irreversible
```

Do not run it to "finish" the migration — the migration is finished when
`digest status`'s `cutover.status` is `complete`. Old channels can simply be
archived by hand, or left alone.

## 8. Keeping the shipped agent skills current

The `aq-*` skills that internal sessions read are maintained in-tree at
`src/skills/<name>/SKILL.md`. `ensure_default_aq_skills` seeds them to each
harness' discovery directory (`~/.claude/skills/`, `~/.gemini/skills/`,
`~/snap/gemini-cli/common/.gemini/skills/`, `~/.codex/skills/`) and — by
design — **never overwrites a copy that already exists**, so local edits survive
a restart.

That means an upgrade does not refresh an already-installed skill. After a
release that changes messaging guidance (this one changes `aq-comms`), delete
the installed copy and restart the daemon to pick the shipped version back up:

```bash
rm ~/.claude/skills/aq-comms/SKILL.md
aq restart
```

Edit `src/skills/aq-comms/SKILL.md` in the repository, not the installed copy,
when the guidance itself needs to change.
