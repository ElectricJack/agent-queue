# Wave 4 · Discord E2E — Manual live-test checklist

Companion to `2026-08-21-wave4-discord-e2e.md` (Task 7). Run this list on
a fresh clone/branch after Tasks 1-6 merge, against a real Discord guild.
Discord.py interactions cannot be automated in-process, so this checklist
is the certification step before MVP is declared shipped.

## Config snippet (`~/.agent-queue/config.yaml`)

```yaml
messaging_platform: discord

discord:
  bot_token: "<real-bot-token>"
  guild_id: "<real-guild-id>"
  authorized_users:
    - "<your-discord-user-id>"
  # rate_guard_* defaults are fine
  per_project_channels:
    enabled: true

messages:
  enabled: true

sessions:
  enabled: true

supervisor_agent:
  enabled: true
  legacy_chat: false   # required to activate the new path
  idle_timeout: 900

work_graph:
  enabled: true

api:
  auth_tokens: []      # bearer auth intentionally off — MVP scope
```

Config validation dependency: `supervisor_agent.enabled=True` requires
both `messages.enabled=True` and `sessions.enabled=True`
(`src/config.py:1401-1410`). If any of those are off the daemon refuses
to start with a clear error.

## Step list

- [ ] **1. Boot.** `./run.sh start`. Confirm log lines:
      `Slash commands registered: /attach, /explain, /gates, /peek, /status, /tasks`,
      `Discord bot connected`, no `Config validation errors`.
- [ ] **2. Project channel.** In Discord, create or reuse a text channel;
      run `aq set project channel <project_id> <channel_id>` (or the
      discord slash equivalent if it survived — otherwise use `aq`).
      Verify with `aq list projects` that `discord_channel_id` is set.
- [ ] **3. Supervisor chat round-trip.** In the project channel, type
      `hi supervisor — what tasks do we have?`. Expected observable behavior:
    - Bot adds a 📬 reaction to your message within ~1s (Task 1).
    - No "💭 Thinking..." message appears (Task 1 — legacy view is suppressed).
    - Within a few seconds, the supervisor session reply appears as a normal
      message in the same channel (existing `_on_message_sent` path).
- [ ] **4. Gate prompt.** In another terminal:
      `aq gate create --project <project_id> --gate-type approval --title "Ship v1?" --question "Approve production deploy?"`.
      Expected:
    - Within ~1 cycle, a gold-bordered embed titled `⏸ Gate: Ship v1?`
      appears in the project channel with **Approve** and **Deny** buttons
      (Task 3).
    - Click **Approve**. Expected: an ephemeral message
      `✅ Gate g-xxx resolved (approve).`; the original embed edits to
      `✅ Gate resolved — approve` in green; buttons disappear.
    - Verify audit: `aq gate list --project <project_id> --status resolved`
      shows `resolved_by: "discord:<your-user-id>"`.
- [ ] **5. Gate denial.** Create another gate, click **Deny**. Confirm
      the embed shows `✅ Gate resolved — deny` (title emoji stays green —
      this is intentional; the resolution word disambiguates) and
      `resolved_by` is your Discord id.
- [ ] **6. Parked-message warning.** Simulate a delivery failure: create
      a message to a non-existent session, wait for the parking timeout
      (default 6h — override via `PARK_AFTER_SECONDS` env or a short
      config for the test). Confirm an orange embed
      `⚠️ Message not delivered` appears in the originating channel (Task 2).
- [ ] **7. `/gates` embed.** Create 3 gates. Run `/gates` in the project
      channel. Expected: one gold embed titled `⏸ Open Gates (3)`, three
      fields shown, no raw JSON (Task 4).
- [ ] **8. Restart smoke.** Stop the daemon, restart, run `/gates` — the
      list is still correct (comes from the DB, not view state). Open an
      older gate message and click Approve — expect the button to fail
      silently (view state was reset; this is documented MVP behavior).
      Fall back to
      `aq gate resolve --gate-id <id> --resolved-by "discord:<user>" --resolution approve`
      and verify.
- [ ] **9. Rate-guard sanity.** In the log, grep for `Rate guard blocked`
      — should be absent under normal load. If present, note the state
      and count; the guard is doing its job.
- [ ] **10. Global-channel legacy path.** In the configured global bot
      channel (`discord.channels.channel` — default `agent-queue`), type
      `@AgentQueue what's the system status?`. Expected: the legacy
      `ThinkingView` appears with progress updates (Task 6 scope
      decision — global channel stays on `Supervisor.chat()`).

After passing all steps, file the certification record. If any step
fails, file bugs against the specific task in
`2026-08-21-wave4-discord-e2e.md` and iterate before declaring MVP
shipped.
