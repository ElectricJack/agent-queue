# Task10c fix round1 review — 12c05e1b

Independent reviewer /root/review_10c_astra: spec and quality Needs fixes.
Original findings 1,3–11 addressed in execution paths; finding2 partially open.
No out-of-scope observations. No tests rerun or mutations performed.

## Open findings (verbatim)

2. **Conflict/red repair cannot replay the existing start identity — NOT ADDRESSED for rebuilt conflicts.**
Initial conflict and red routes correctly dispatch the existing stage instead of restarting it. However, the replacement rebuild route maps `conflict` directly to failure (`scripts/rebuild-reviewed-playbook-artifacts.py:299`). When main moves and rebuilding introduces a conflict before the repair deadline, no primary repair is dispatched. `CandidateService.rebuild` dispatches only when the deadline has expired; otherwise it returns `build()`’s result (`src/integration/candidates.py:425`, `src/integration/candidates.py:460`). Route rebuilt conflicts through the existing server-derived repair dispatch and test that path through the engine.

- **Important — Failed HEAD lookup is treated as proven worktree removal.**
`src/integration/cleanup.py:461` returns `complete` whenever `arev_parse` returns `None` and a prewrite exists. That helper returns `None` for any subprocess failure, timeout, or unavailable checkout—not specifically absence (`src/git/manager.py:3886`). A failed removal followed by a transient HEAD-read failure can therefore mark an existing retained worktree permanently complete. Verify absence through the recorded base repository’s worktree registration and filesystem identity; preserve retryable failure when absence cannot be established. Add a regression where the directory remains present after prewrite and HEAD lookup fails.

- **Important — Partial downgrade can discard unresolved irreversible-write reservations.**
`migrations/versions/a10c5e1e4f02_cleanup_irreversible_prewrite.py:73` drops the prewrite guard and both reservation columns without checking existing cleanup items or terminal batch state. Downgrading to `a10c5e1e4f01` with a marked unresolved comment, then upgrading again, recreates null reservations and permits another POST. The later source-retention downgrade checks only frozen source identities, so legacy items are not protected (`migrations/versions/a10c5e1e4f03_frozen_source_retention.py:170`). Apply the brief’s live-cleanup downgrade refusal before removing reservation state, and test seeded marked items through partial downgrade targets on both dialects.

## Accepted evidence

Pending observation now emits exact durable events, disabled frozen routes are retained,
release replay uses immutable results, comments have exclusive prewrite, PG aggregate
serialization is tested, source retention frozen, local ownership/occupancy protected,
all-item command projection correct, successful removal replay works, PR NULL CHECK fixed.
Reviewer confirmed tests/report against diff, including320pass2skip and controller PG.
