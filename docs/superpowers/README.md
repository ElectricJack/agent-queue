# Feature design history

<!-- aq:historical -->
> **Historical material.** Everything under this directory is the per-feature
> design and planning record: what was intended, what was planned, and what was
> observed while it was built. None of it is maintained as a description of
> current behaviour. Start at [the documentation home](../README.md).

Where [`docs/specs/`](../specs/README.md) holds the cross-cutting design of the
system, this directory holds one dated set of documents per *feature*. It is the
densest source of "why is it like this" in the repository, and current pages cite
it as background.

| Directory | What it holds | Count |
|---|---|---|
| [`specs/`](specs/README.md) | The design for one feature: problem, model, invariants, failure modes. Some are the authoritative argument behind shipping behaviour. | 51 |
| [`plans/`](plans/README.md) | The implementation plan for one feature: packages, commit order, acceptance. | 60 |
| [`reports/`](reports/README.md) | Evidence captured while a feature was built — screenshots, scenario transcripts. Preserved unedited. | 1 bundle |

## Naming

`YYYY-MM-DD-<slug>-design.md`, `-implementation.md`, `-review.md` and
`-plan.md` are the usual endings, and a feature's design and plan share the slug.
The date is when the document was written, not when the feature shipped.

## Reading one safely

* A design says what was intended. Two of these describe work that was later
  re-scoped — [global worker pools](specs/2026-09-08-global-worker-pools-design.md)
  re-scoped the [swarm work model](specs/2026-08-28-swarm-work-model-design.md)'s
  per-project sizing to fleet-wide — so read the later document too.
* A plan's checklists are historical, not a to-do list.
* Paths inside are the paths that existed on the file's date.

Every file here has a recorded disposition in
[the disposition ledger](../history/disposition-ledger.md).
