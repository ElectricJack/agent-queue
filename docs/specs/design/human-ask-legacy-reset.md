# Discard legacy asks and start fresh

Jack's 2026-10-19 reset directive supersedes historical reconciliation for a
project that explicitly invokes `ask_legacy_reset`. The command is available to
the local operator and a live named global supervisor with its capability.

Preview is the default. It takes a complete, bounded snapshot (10,000 rows by
default; explicit maximum 100,000), simulates the reset in a rolled-back
transaction, and returns counts, the kept review IDs, projected blockers and a
content hash. Exceeding the bound refuses the whole operation. Apply requires the
same snapshot hash and `transferring` or `delivery_disabled`; changes to rows,
mode, destination or configuration require a fresh preview. Short maintenance
table locks prevent writers from changing the snapshot during the transaction.

Apply discards all existing project ask projections, old aliases, previews,
action tokens, repair cases and legacy delivery rows. It cancels legacy incidents,
actions and shared ask deliveries, revokes old ask inputs, and retires bindings.
Immutable decision packets are left as obsolete records behind discarded asks;
no decision is applied or replayed, no history is imported, and no per-row audit
is copied. Normal ask reads and delivery exclude discarded projections.

Only source reviews still `in_review`, with a human-capable decider and no pending
accepted decision, become fresh asks with new identities and pending destination
bindings. Their review content/comments and gate remain live work. The reset
makes no transport calls; after the separately authorized channel switch, the
normal sender may publish these fresh requests. Old Discord posts are not edited.

One system-config summary records the cutoff, counts/hash, actor and fresh review
IDs. Its cutoff prevents subsequent source scans and legacy input backfill from
recreating discarded history. Existing discarded sources cannot refresh themselves
back into visible asks. Sources created after reset continue normally. Repeating an
accepted apply hash returns its summary without discarding new work.

A successful reset preview/apply has zero cutover blockers apart from the active
transfer marker. It does not change the communication mode or bypass the separate
cutover generation/hash checks. Supervisor-global owns deployment, live preview,
production apply and channel switch. Fixture evidence does not claim live acceptance.
