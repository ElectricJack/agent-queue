# App-mode train S4/S5 routing rerun — 2026-09-29 UTC

Result: **pass** in the disposable App-only world at
`/tmp/aq-e2e-smart-ember-v2-seq7`. The approved fixture was
`ElectricJack/aq-gh615-app-fixture-20260923` (GitHub repository ID
`1384141153`); project ID `aqapp-0928` matches its committed trust manifest.
Supervisor approval was recorded in aq message
`msg-f7d53f1e4b994d35bc6bb364ad08c02b`. The world used PostgreSQL on
`127.0.0.1:5575`, API port `8175`, the fake session provider, and an App-only
daemon environment with no `gh` login or ambient GitHub token. The full local
evidence ledger is `/tmp/aq-e2e-smart-ember-v2-seq7/evidence.jsonl`; sampled
GitHub responses are under that world's `payloads/` directory.

The graph filed only an unrouted `standard-high` leaf. A single manually run
playbook filed the `reviewer` role task with a `discovered-from` edge. The leaf
was routed to `train-worker`; the reviewer remained on profile `reviewer`.
The parent verifier and repair delegate were routed to `train-worker` with
`standard-high`. The verifier runner retained its claim through the expected
`stale_verification` close refusals while parent CI established evidence, then
closed successfully and drained. No blocking dependency delayed the reviewer.

| Gate | Source and review | Candidate and delivery | Proof |
|---|---|---|---|
| S4 green | Epic `sharp-ember`; leaf `sharp-ember.1` pushed `9942fd262cb016c90ab21efeae403f3e37ad9f6d`; reviewer `sharp-ember.2` closed no-op, receipt `3cd66ae8-3448-4d93-8387-4e8c184f6a8a`; parent head `2b6c8f6cb3a75ab6b6af3a283393580582475e95`; [PR #19](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/19), operator review `5346381438` | Batch `integration-batch-2044f3a2d1e7ebd0c464dfc156ff384d`, [audit PR #20](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/20), candidate and final `main` `a852795bab14a37e1d53b91097ad6664de4d1ff3` | App attestation check `109203987685` passed; [main audit run `36504907339`](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/actions/runs/36504907339) succeeded; fallback `unattested-ci` skipped |
| S5 red then repair | Epic `fresh-zenith`; leaf `fresh-zenith.1` deleted `train-repaired.txt` at `15947224a761d4b41435126f97c7cd32898f8c85`; reviewer `fresh-zenith.2` closed no-op, receipt `6f1961e5-3fc4-45df-aae2-4fb0b2cc2626`; parent head `e1d468d994badb101b89996edae7c08b2689b47f`; [PR #21](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/21), operator review `5346430388` | Batch `integration-batch-d3154bd75a07f25058ca656fe75d3ecf`, revision 0 `3a7adf4311abfea0f5e3360d5cb3dd57691618ea` failed check `109205906403`; delegate `repair-repair-batch-integration-batch-d3154bd75a07f25058ca656fe75d3ecf-0` restored the file on the same integration branch at `65d65abd940c14d1d174f06b60d3417e89d0a82c`; revision 1 adopted that SHA and [audit PR #22](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/22) delivered it to `main` | App attestation check `109206390275` passed; [main audit run `36505672668`](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/actions/runs/36505672668) succeeded; fallback `unattested-ci` skipped |

Both promoted candidates matched the exact final `main` SHA at their respective
assertion. The attestation check came from App `5075923`, used version
`fixture-v2`, and carried canonical JSON with a matching external ID. The
`Train-only main` ruleset had no bypass actors at either promotion. No other
GitHub repository was mutated.

## Discarded scratch attempts and fixture cleanup

The first approved attempt exposed concurrent `task.created` playbook runs,
which filed two reviewers. A later attempt exposed that a reviewer `blocks`
edge deadlocks the leaf collection and review frontier. The code now runs the
reviewer playbook once after graph filing and keeps only provenance. An
attempt with a different project ID failed preflight because the committed
manifest binds `aqapp-0928`; it made no fixture commit or PR.

Two subsequent scratch batches using that canonical project ID reused closed
audit PR branch identities from earlier runs. They were stopped before any
promotion. Their fixture changes are recorded below; the two open epic PRs
were closed after the successful S4/S5 run. Their branches and candidate refs
remain as evidence and were not merged to `main`.

| Scratch world | Fixture artifacts | Disposition |
|---|---|---|
| `v2-rerun` | `clear-horizon.1` pushed `a03ff0089fcaeb5ec09f04a3259071092289756e`; parent head `53ad0991bf7a8c952514c7b981a81d5c8ff2cd58`; [PR #17](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/17), review `5346157468`; candidate `00139fee71f64061d9aabf09cfb2c817fb5461ac` on batch `integration-batch-8d087c8963022f418af01dc9e9053f91` | Audit PR #10 already owned the deterministic branch; PR #17 closed unmerged. |
| `v2-seq2` | `vivid-delta.1` pushed `61ba35bdb5a144910b595654e0b956e1ed634591`; parent head `4ee4d90c88eaa2c64ad6da28b1fc0c2398ed05ac`; [PR #18](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/18), review `5346293306`; candidate `2963d871dc6d8261f2dcfed1847d53b037c9d9c3` on batch `integration-batch-494199612ad17a05168fcf31d3f78ccf` | Audit PR #12 already owned the deterministic branch; PR #18 closed unmerged. |

The final world consumed six empty sweeps before S4, so its batches used fresh
request sequences 7 and 8. This was a fixture history issue: the canonical
project ID's earlier request sequences 1–4 already had closed audit PRs.
