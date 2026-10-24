---
id: project-defaults
name: Project defaults
version: 1
scope: system
enabled: true
triggers:
  - event_type: project.created
    filter:
      workspace_in_vault: false
---

# Project defaults

An operator-local system policy, approved by Jack on 2026-09-27. Every new
project receives the fleet's standard repo-level setup as one chore task in
that project, so every AQ project repository on a host with graft carries
graft's Claude Code hooks and ignores graft's index cache. Graft's hooks left the global
`~/.claude/settings.json` so that graft stops indexing the AQ vault; they now
belong in each project repository. This is the playbook form of the one-off
task `prime-beacon-23`, which applied the same steps to `agent-queue`.

The trigger is `project.created`, which the daemon emits once for each new
project. An onboarding request emits it on its first success with `source`
`onboarding` and the primary workspace's `workspace_id`, `workspace_path` and
`workspace_in_vault`; `create_project` emits it with `source` `command` and no
workspace fields. The trigger filter admits only an event whose
`workspace_in_vault` is `false`. That skips a project whose workspace is the AQ
vault or lies inside it: vault content is notes and policy, not a code
repository for graft to index. It also skips a project created without a
workspace, because there the field is absent rather than `false` and there is
no repository to commit to.

The playbook never edits a repository and makes no model call. It files the
chore and ends. The worker that claims the chore makes the edit and publishes
it through the project's normal delivery.

Graft is optional: not every AQ install has it, and the hooks call graft's
helper through `node`, so committing them on a host without graft would make
every hook fail. The chore therefore checks the install before it adds any
setting. Its first instruction is to confirm the graft CLI, Node.js, the hook
template and the helper the hooks run; if any is missing, the worker changes
nothing and closes the chore as a pass that names what was missing. The check
lives in the chore because no command a playbook may call reports whether a
tool is installed on the host. On a host without graft the chore costs one
short worker turn and changes nothing.

## Rule: file-graft-hooks-chore

There is no guard beyond the trigger filter; the graft install check is the
chore's first instruction. The rule performs one idempotent step and then
ends.

1. Call `ensure_task` with `project_id` bound to the event's `project_id`,
   `dedup_key` `project-defaults:graft-hooks`, `title`
   `Enable graft hooks at repo level`, `intelligence_class` `fast-high`
   (a mechanical edit to two files), and the `description` quoted below. Bind
   the result as `chore`. `ensure_task` is keyed per project, so a replayed
   event reuses the live chore instead of filing a second one. A `created` or
   `reused` outcome ends the rule; a `rejected` or `runtime_error` outcome
   fails it.

The chore's `description`, verbatim:

> Enable graft for this repository by committing its Claude Code hooks at repo
> level. Filed by the system playbook project-defaults when this project was
> created (Jack, 2026-09-27: graft's hooks live in each AQ project repository,
> not in the global ~/.claude/settings.json, so graft never indexes the AQ
> vault).
>
> Graft is optional, and not every AQ install has it. Check the install first,
> before you change anything:
> - command -v graft finds the graft CLI;
> - command -v node finds Node.js (every hook runs node);
> - the hook template
>   /home/jkern/.agent-queue/vault/system/templates-graft-claude-settings.json
>   exists;
> - the helper the template's hooks run,
>   /home/jkern/.claude/helpers/graft-hooks.cjs, exists.
>
> If any check fails, graft is not installed on this host: change nothing and
> close with outcome pass, naming the missing piece in the summary. Do not
> install graft or write the helper yourself; installing graft is the
> operator's choice.
>
> Steps, once every check passes:
> 1. Read the hook block from the template (a JSON object with a single
>    "hooks" key).
> 2. Merge it into .claude/settings.json at the repository root. Create the
>    file if it is missing, keep every existing key and hook, and do not add a
>    hook entry that is already present. Do not touch .claude/settings.local.json.
> 3. Make sure .gitignore contains the line /graft/ (graft's regenerable index
>    cache), adding it if absent.
> 4. If .gitignore ignores .claude as a whole directory (a bare .claude or
>    .claude/ pattern), git cannot re-include a file beneath it: replace those
>    lines with **/.claude/* and !/.claude/settings.json. Confirm with
>    git check-ignore --no-index that .claude/settings.json is not ignored and
>    that .claude/settings.local.json and graft/ still are.
> 5. Check the JSON parses: python3 -m json.tool .claude/settings.json.
> 6. Commit only .claude/settings.json and .gitignore with the message
>    "chore: enable graft hooks at repo level", then publish and close as
>    aq prime instructs.
>
> If the repository root lies inside /home/jkern/.agent-queue/vault, change
> nothing and close with outcome pass, saying so in the summary. No code
> changes and no tests beyond the checks above.

## Failure handling, uniformly

The rule has no retry. A refused or failed `ensure_task` ends the run at a
`failed` terminal, so the run overlay shows what broke (for example an unknown
project, or an intelligence class this install does not have). The chore can
then be filed by hand with the same title and description. The
event's `project_id` is the authorization boundary: the chore is filed only in
the project the event announced. A `project.created` event that arrives again
for the same project reuses the live chore through its `dedup_key`; once the
chore has completed, a fresh event would file a new one, which is harmless
because every step above is a no-op on an already configured repository.

A chore that closed because graft was missing stays closed: installing graft
later emits no event, so nothing refiles it. To enable graft for that project
afterwards, file the chore by hand with the same title and description.

## Operator amendment (2026-09-27): graft is main-checkout only

Jack approved a revised graft policy after the graft research:

- Onboarding runs `graft init --no-global --no-statusline`. Never global wiring.
- Graft is off for AQ workers. `~/.local/bin/graft` is a shim that sets
  `GRAFT_NO_SEED=1 GRAFT_NO_REFRESH=1` under `.aq/worktrees/` or when
  `AQ_AGENT_ID` is set. `~/.claude/helpers/graft-hooks.cjs` exits early in the same
  cases. Committed hooks and `.mcp.json` stay as they are, so worker slots don't
  copy the index (the old copies were about 8.5 GB across slots).
- Telemetry is disabled (`graft telemetry disable`, plus `DO_NOT_TRACK=1` in the shim).
- Codex and OpenCode have no global graft hooks or MCP.
- The 0.20 upgrade is on hold. `npm i -g` would replace the shim and a `graft init`
  could rewrite the helper, so re-check both guards after either one.
