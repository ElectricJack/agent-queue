# Discord replacement capability checklist

The Discord simplification implementation spec (§2 and §9) removes
Discord's operational controls only once something else genuinely does the job.
This is the ledger the cutover task (§10) works from: one row per Discord surface
that is slated for removal, the surface that replaces it, and — the part that
matters — whether that replacement exists **today**.

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

## Gaps — do not remove the Discord control yet

| Discord surface | Blocking work | Why it is not ready |
|---|---|---|
| Retry / skip / stop / reopen task buttons | §10 cutover task | The dashboard has the task controls, but the persistent Discord views must be restored-and-neutralised first, or a pre-cutover button still mutates a task. |
| Gate and playbook resume buttons | §10 cutover task | Same: the replacement (Gates drawer) exists; the retired views do not yet refuse. |
| `question_answer` modal → direct worker delivery | §6 supervisor bridge | Answer delivery must become supervisor-mediated before the modal is removed, or an in-flight worker question loses its only answer path. |
| Pending legacy conversations (open questions/gates already posted) | §10 migration | They must be migrated to escalation identities first; removing the callbacks before that strands a human request already asked in the channel. |
| Per-project channel auto-provisioning | §10 cutover task | Single-channel settings exist here; `per_project_channels` is still honoured by the adapter. |
| Digest and escalation *delivery* | §7 outbound adapter task | This change configures, previews and reports the schedule; the sender that reserves windows and posts them is separate work. Until it lands, `digest_status` will show no windows on a live install. |

## How to re-check

- Settings, preview and inbox: `aq test tests/test_discord_settings.py tests/test_digest_commands.py`
  and `npx vitest run src/pages/settings/__tests__` from `dashboard/`.
- Surviving direct mutation paths: `rg '_handler\.execute\(' src/discord/` should return
  nothing once the §10 cutover task closes. Today it still returns `stop_task`,
  `restart_task`, `skip_task` (`src/discord/notifications.py`) and `gate_resolve`
  (`src/discord/gate_view.py`) — which is exactly what the gap rows above record.
