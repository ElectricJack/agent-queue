# Sleeping named session cleanup

`aq session cleanup` removes old named session records in one operator command.
It selects sessions whose observed state is `sleeping` and desired state is
`sleeping` or `stopped`, with no task or claim. `--project-id` narrows the
selection; `--dry-run` lists it.

For each candidate, the daemon fences the stop intent to the session instance,
stops its terminal through the configured provider, and invokes the existing
prune checks. Those checks require the terminal for that *instance* and its
marked processes to be gone. A newer terminal reusing the same name does not
keep old history, so its different instance token is accepted as proof. The
daemon then removes the record and revokes its token. A concurrent wake, changed instance,
failed stop, or lingering process leaves the row intact and appears in the
command's `skipped` results. Running, task, pool and claimed sessions are not
candidates. The command does not delete transcripts or agent definitions.
