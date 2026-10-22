# Human ask cutover status

`aq ask cutover-status --project-id PROJECT --json` reads communication ownership
through CommandHandler for the local operator, daemon, or live named global
supervisor with the command grant, with the same access
boundary as `ask_cutover`. It does not reconcile, create mode rows, advance a
generation, change a deadline, or send external messages.

The result identifies the project and configured destination and lists all stored
destinations for that project in destination order. If the configured destination
has no row, include its effective `legacy` mode, generation `0`, `migrated: false`
and null timestamps. Generation `0` is the expected generation for its first CAS.
Other projects are excluded.

Each destination reports `destination`, `mode`, `generation`, `migrated`,
`first_switch_utc`, `compatibility_deadline` (Unix seconds), and
`compatibility_deadline_utc` (ISO 8601 UTC). The first switch means the first
switch to `channel`: derive it from the persisted immutable deadline minus 30
days. Both UTC fields are null until that switch. Rollback, re-enable and later
mode updates preserve these timestamps. A status read is a snapshot; callers
must still handle a stale generation returned by a subsequent cutover CAS.
