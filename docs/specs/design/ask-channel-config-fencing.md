# Ask channel configuration fencing

Discord ask intake requires a durable `channel` ownership row for the configured
project and destination, pinned to the current Discord configuration fingerprint.
When `discord.project_id` is unset, the initial router check may find any current
channel row: this selects the shared input ledger before the ask is known. Once
correlation selects an ask, its own project/destination row and current fingerprint
are required before discussion or decision interpretation. Another project's mode
never grants that authority.

Delivery reconciliation and explicit cutover share the same configuration fence.
They persist the last observed destination/fingerprint in the existing
`system_config` entry `ask-delivery-config:v1`, serialized under a transaction
advisory lock. Only ownership rows at the current or previously observed configured
destination are candidates for retirement. A changed fingerprint disables a pinned
nonlegacy writer, advances its generation, preserves migration/deadline history,
and revokes external action tokens. Reverting configuration never enables it;
re-enabling requires explicit transfer and inventory evidence. Other destinations
retain their rows unchanged. Unrelated configuration fields are outside the
fingerprint and do not retire writers.

Explicit cutover excludes its target row from automatic retirement and advances
that row through the existing generation CAS and transition/inventory checks.
The observation and all mode changes commit together. A rejected cutover changes
neither. A read-only cutover-status request never records an observation.

Migration: the first observation has no previous destination and examines only the
currently configured destination. Historical unrelated destinations are not inferred
to have changed merely because their hashes differ. Legacy rows are left unchanged,
including their generation and `migrated` flag. Missing/empty pins (including nulls
from historical schemas; current metadata requires non-null pins) are also left for
explicit operator cutover and cannot admit shared input or delivery. No YAML edit
implicitly migrates such a row. Operators use the existing shadow/dashboard/transfer
procedure for legacy rows and transfer for already migrated rows; no schema migration
or automatic channel enablement is introduced.
