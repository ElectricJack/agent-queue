# Audit and acceptance reports (historical, unedited)

<!-- aq:historical -->
> **Immutable evidence.** Each bundle below records what was true on the date in
> its name: what an audit found, what an acceptance run observed, what a review
> recommended. Bundles are preserved exactly as produced. This ticket added this
> index and repaired broken *link paths* inside one file; no finding, count or
> conclusion was changed. Start at
> [the documentation home](../README.md).

A report is evidence, not guidance. Read one to answer "what did we know on that
day", never "what should I do now" — the answer to that is under
[`docs/concepts/`](../concepts/) and [`docs/guides/`](../guides/README.md).

| Bundle | Date | What it evidences |
|---|---|---|
| [`cli-audit-2026-09-08/`](cli-audit-2026-09-08/README.md) | 2026-09-08 | A command-by-command audit of the `aq` CLI: which commands were exercised live, which failed, and the epic filed from the failures. Also holds [`inventory.md`](cli-audit-2026-09-08/inventory.md), the audit-time command list. |
| [`integration-reliability-2026-09-08/`](integration-reliability-2026-09-08/acceptance.md) | 2026-09-08 | An acceptance run of the integration path against real workloads, including the graph payloads it drove and the activity window it observed. |
| [`integration-safeguards-2026-09-09/`](integration-safeguards-2026-09-09/REVIEW.md) | 2026-09-09 | A review of every guard in the integration subsystem, with [`SOURCE-INDEX.md`](integration-safeguards-2026-09-09/SOURCE-INDEX.md) linking each invariant to the line that enforces it, plus the delivery note and a live summary. |
| [`unmerged-branches-2026-09-09/`](unmerged-branches-2026-09-09/README.md) | 2026-09-09 | An inventory of branches that had not reached `main`, the merge plan drawn from it, what was executed, and the Discord-simplification acceptance check. |

The link repair mentioned above: every source link in
`integration-safeguards-2026-09-09/SOURCE-INDEX.md` pointed one directory level
too high (`../../../../src/…` from a directory three levels below the repository
root), so all 549 of them 404'd on GitHub. They now resolve.

Every file here has a recorded disposition in
[the disposition ledger](../history/disposition-ledger.md).
