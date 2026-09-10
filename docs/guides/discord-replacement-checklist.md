# Discord replacement capability checklist

<!-- aq:historical -->
> **Retired guide.** The procedure below no longer matches AQ and is kept only
> so existing links resolve. [Messaging, digests and
> escalations](../concepts/messaging.md) describes the surface that replaced
> these controls. Start at [the documentation home](../README.md); see
> [historical material](../history/README.md).

The Discord simplification implementation spec (§2 and §9) removes
Discord's operational controls only once something else genuinely does the job.
This is the ledger the cutover task (§10) works from: one row per Discord surface
that is slated for removal, the surface that replaces it, and — the part that
matters — whether that replacement exists **today**.

The cutover has shipped. For the operator procedure that uses this ledger —
channel selection, pending-conversation migration, settings, digest preview,
delivery health and rollback — see the
[migration runbook](discord-migration.md).

Status is asserted against this checkout, not against intent. "Ready" means the
replacement is reachable by an operator now; "gap" means removing the Discord
control today would lose a capability.

## Digest and escalation (this change)

| Discord surface | Replacement | Status |
|---|---|---|
| Immediate task/PR/budget/playbook posts | Hourly digest, `digest_preview` / `digest_status`, Settings → Messaging | Ready |
| Guessing whether a quiet hour is a fault | Preview shows the §8 suppression reason and next evaluation | Ready |
| Wondering whether a post landed | `digest_status.delivery_health` + pending escalation deliveries in the panel | Ready |
| Channel and mention configuration by hand-edited YAML | Validated `discord.channel_id` / `digest` / `escalation` settings, bounds enforced in the schema and the daemon | Ready |
| Reading an escalation in the channel | Settings → Messaging escalation inbox: state, project, supervisor owner, decision requested, conversation, task link | Ready |
| Replying in a Discord thread | Dashboard reply through `escalation_reply` — the same core command the adapter uses | Ready |

## Controls whose replacement already shipped

| Discord surface | Replacement | Status |
|---|---|---|
| `/status`, `/tasks` | Command Center task list and graph (`/command-center/tasks`) | Ready |
| `/explain` | Task detail and `aq task explain` | Ready |
| `/gates` | Gates drawer (`/command-center/tasks?openDrawer=gates`) | Ready |
| `/peek`, `/attach` | `SessionDetail` transcript/terminal and `aq session logs` | Ready |
| Per-execution task threads and streamed output | Live session view and recorded attempts | Ready |
| General channel chat / mention routing | Dashboard supervisor chat (`/agents`) | Ready |

## Cutover-complete controls

| Discord surface | Replacement | Status |
|---|---|---|
| Retry / skip / stop / reopen task buttons | Dashboard task controls | Ready; old views are made inert before inbound routing starts. |
| Gate and playbook resume buttons | Gates drawer and playbook views | Ready; the Discord view classes and callbacks are removed. |
| `question_answer` modal → direct worker delivery | Supervisor-owned escalation reply/apply flow | Ready; accepted pre-cutover answers are preserved without redelivery. |
| Pending legacy conversations | Idempotent startup migration to durable escalations | Ready; compatible bot-owned roots in the selected channel are adopted. |
| Per-project channel auto-provisioning | One explicit shared `discord.channel_id` | Ready; legacy settings only produce a migration warning. |
| Digest and escalation delivery | Durable shared-channel transports | Ready. |

## Documentation and agent guidance

| Discord surface | Replacement | Status |
|---|---|---|
| `messaging-rework.md` M4 / §4.1–4.5 as the Discord product description | Superseded in place; the implementation spec and this ledger are canonical | Ready |
| "Discord as control plane" in `docs/index.md`, `profile.md`, `docs/agent-queue-primitives.md` | Notification-only framing, dashboard/CLI as the control plane | Ready |
| `create_project`'s `auto_create_channels` parameter in `docs/specs/command-handler.md` | Removed from the command and from the docs | Ready |
| Agent guidance that pointed a worker at "the Discord channel" | `aq-comms` skill: `user:dashboard` for the human, escalations for decisions | Ready; installed copies need a refresh, see [the runbook](discord-migration.md#8-keeping-the-shipped-agent-skills-current) |
| `messaging-rework.md` §2, §3.2–3.3, §4.2, §6 M2–M4 (the separate `packages/aq-discord/` process and its `aq-discord.yaml`) | Marked superseded and never built; the adapter stays in-tree at `src/discord/` and §4.1 documents the shipped `discord:` block | Ready |
| `get_project_for_channel` in `docs/specs/mcp-server.md` | Removed; there is no channel→project lookup because there is one shared channel | Ready |
| `send_message` ("post to a Discord channel") in `docs/guides/agent-tools.md` | `message_send` for agent-to-agent, escalation commands for a human decision; no agent tool posts to Discord | Ready |

## How to re-check

- Settings, preview and inbox: `aq test tests/test_discord_settings.py tests/test_digest_commands.py`
  and `npx vitest run src/pages/settings/__tests__` from `dashboard/`.
- Retired surfaces staying retired: `aq test tests/test_discord_docs.py` — the scan fails if
  any doc names `auto_create_channels`, `per_project_channels`, `channel_overrides`,
  `aq-discord.yaml` or a project-channel lookup in a section that does not say it is gone,
  and the tool tables are resolved against the live `CommandHandler`.
- Surviving direct mutation paths: `rg '_handler\.execute\(' src/discord/` returns only
  the narrow `escalation_reply` adapter boundary; task, gate, playbook, worker-input and
  project mutation commands are absent.
