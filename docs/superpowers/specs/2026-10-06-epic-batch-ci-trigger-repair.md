# Missing push CI on epic batch candidates

Task: `grand-horizon-57`.

An epic branch created before the `aq/batches/**` workflow triggers were
introduced carries its older workflows into every assembled candidate. A
push to the candidate ref then creates no workflow run. Repeated authenticated
observations must not leave that exact head pending forever.

The hosted exact-commit cache gives an absent push run five minutes from its
first authenticated observation. It retains that timestamp in the existing
check detail across lane reconstruction and daemon restarts. A transport error
does not restart a known missing-run deadline. A different SHA has its own
deadline, and an actual push run replaces the absence through normal refresh.
An in-progress push run remains pending; this timeout does not bound CI execution.
Dispatch runs and push runs on another SHA do not satisfy the absence check.

After the grace period, check evidence is unavailable with classification
`ci_not_triggered`, never a fabricated conclusive red or green result. The train
projects a named blocker through project and task integration status, including
the exact candidate, its workflow paths and push filters read from its own tree.
Branch matching is diagnostic only; unfamiliar filter syntax stays unknown.

The train files one ordinary managed repair using its existing batch allocator.
The brief directs the worker to fetch the repository's default branch, review
the missing workflow changes, and merge or apply them in an ordinary candidate
commit while preserving branch-specific changes and every frozen member.
Publication uses the existing lease and expected-old OID. The repaired head's
push must produce the required checks before it can advance the epic target.
When the branch filters already allow the candidate, the brief instead calls
for diagnosing path filters, push credentials and Actions availability.

Required check names, trusted producer identity, push-event proof, exact-head
checks and authenticated attestation remain unchanged. No workflow dispatch,
CI waiver, new control, schema migration or operator-state mutation is added.

Verification covers the durable deadline, restart/reconstruction, transport
interruption, dispatch and foreign heads, real pending push runs, and recovery
to trusted green. A real local Git/PostgreSQL epic scenario starts with old
workflow filters, projects the named blocker, files the ordinary repair, then
uses a managed push of updated workflow content to obtain simulated GitHub
push evidence and attestation on the repaired exact head.
