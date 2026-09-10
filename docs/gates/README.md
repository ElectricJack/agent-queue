# Gate acceptance evidence (historical, unedited)

<!-- aq:historical -->
> **Immutable evidence.** A gate file records the acceptance criteria a lane
> signed up to and the evidence it produced, on the date it was written.
> Preserved unedited. Start at [the documentation home](../README.md).

This convention comes from `docs/specs/design/trust-and-ops.md` §8 and was used
once, for the framework-overhaul waves. Nothing in the code reads these files —
the author's own note says "convention only — no tooling gates on this file".

Do not confuse a gate *file* with a **human gate**, which is a durable,
operator-resolved decision checkpoint in a playbook run. Those are live state,
explained in [Playbooks V2](../concepts/playbooks.md), and are resolved with
`aq gate resolve` or the dashboard Gates drawer.

| Date | Gate | File |
|---|---|---|
| 2026-08-19 | Wave 1 Lane 1C — Trust & Ops | [`wave1-1c-trust-ops.md`](wave1-1c-trust-ops.md) |

Every file here has a recorded disposition in
[the disposition ledger](../history/disposition-ledger.md).
