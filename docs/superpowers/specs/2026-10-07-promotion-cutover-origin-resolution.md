# Promotion cutover: immutable origins and exact receipt reversal

Task `wise-ember-69.5` revises the origin rewrite in the release plan's
cutover step. Materialized origins are immutable provenance. Cutover must
leave every origin row and its guard unchanged.

The generation-fenced cutover transaction stores a nullable
`projects.default_branch_cutover` record containing the repository, old and
new defaults, activated generation and cutover time. Routing resolves a live
top-level origin in that repository whose parent names the old default and
whose creation predates cutover to the current default. This includes leaf
roots and unfinished roots. Nested origins and later hotfix origins retain
their recorded targets. Batch selection, batch admission, delivery lookup,
completion provenance and worker instructions share this rule. Reverse clears
the record in the same transaction as restoring the previous binding. An
active record must be reversed before another cutover replaces it.

Receipt inventory matches both bare branch names and `refs/heads/` names.
The saved plan and audit capture each receipt's original target string.
Forward cutover preserves its prefix, and reverse restores the exact string.
Only the receipt update guard may be suspended, under the existing table lock
and transaction with before/after guard verification. Lock acquisition has a
bounded timeout; cancellation rolls back rows, configuration and trigger DDL.

Every apply, including reverse, requires a saved plan. Changes to its fenced
inputs refuse with `plan changed; preview again`. The plan lists creation of
flow targets as well as the default branch. Aborted-member inventory reports
the recorded abort/eject/supersede event time, or an unknown time when no event
exists; cleanup's mutable `updated_at` is never presented as the abort time.

The standalone default-branch command retains its previous best-effort fetch
behavior. A branch-creation push or its exact-OID verification failure refuses
the configuration write.
