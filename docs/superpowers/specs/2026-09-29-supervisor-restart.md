# Explicit supervisor restart

The supervisor agent window offers Restart with Fresh conversation (default) or
Resume conversation. `aq supervisor restart [--name supervisor-global] [--resume]`
and `POST /api/supervisor/restart` dispatch the same `supervisor_restart` command.
The command requires global administrator scope. An optional `session_id` binds
the request to the session displayed by the dashboard; a stale request is refused.

The session lens serializes restart with its existing message/reconciler launch
lock. It validates the supervisor profile, harness and resume compatibility before
stopping the prior instance. It records stopped intent, stops with the instance
token fence, confirms termination, retires the old row and revokes its bearer
token. It launches a new instance through `SessionSpecBuilder.build_named_spec`,
with current profile and individual agent overrides, a new row and a new token.
Fresh overrides profile wake policy; resume explicitly supplies the prior
conversation key and refuses a missing key or incompatible harness/workspace.
Stop uncertainty prevents relaunch. Failed relaunch keeps the prior instance
retired and reports failure. Restart never marks a task complete.

Named Claude sessions honor both explicit permission opt-ins:
`claude_dangerously_skip_permissions: true` and
`permission_mode: bypassPermissions`. The boolean remains specific to Claude;
it cannot grant another harness a sandbox bypass. AQ derives model, effort and
permission arguments through its shared builder and does not reuse a host launch
command containing `--permission-mode auto`.

Verification covers real command/API dispatch over a disposable PostgreSQL
database and fake provider, fresh/resumed argv, old-token revocation, stale
requests, stop/start failures, launch serialization and dashboard controls.
