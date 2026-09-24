# Disposable integration train proof — 2026-09-24

This run uses the retained private GitHub repository
`ElectricJack/aq-gh615-app-fixture-20260923` (repository ID `1384141153`)
and isolated AQ daemons/databases under `/tmp/aq-train-smart-stone-3c` and
`/tmp/aq-train-smart-stone-3e`. The latter uses a dedicated disposable
PostgreSQL container on port 5556.
No production schema migration or daemon restart was performed. The scratch project
uses the reviewed project-scoped parent and root playbook artifacts
`sha256:a6a39410cf600b46cf90619966d604ae99284d48cf037f60b575a8d1068532eb`
and `sha256:b4d97387fe6477bf6aef72342fb600619f71d46ca7c82bef0f5f84f1a64cce3b`.
Preflight reported zero blockers before enabling train mode.

## Child delivery and parent verification

| Boundary | Durable evidence |
| --- | --- |
| Pool worker | `fresh-summit.1` published `aq/fresh-summit.1` at `93be052e6832a64b3decd71de4f881b43404847c`; pool close handed off the branch. |
| Pool reviewer | `fresh-summit.2` approved that exact head and closed with `work_outcome=no-op`; leaf review evidence `review-210b040a-1ba5-53ff-824c-ee404cca4d09` was recorded. |
| Parent collector | Delivery receipt `receipt-b357d049-c323-592c-9851-e8731b0bb759` incorporated the leaf into the epic branch at `1cba0e3c15e54275d814d0eafc4bfc8c5b10bd95`. |
| Hosted parent CI | AQ published immutable `aq/parent/fresh-summit/4d69e8024cd25b917e4a71caf3252c97/2/1cba0e3c15e54275d814d0eafc4bfc8c5b10bd95`. GitHub Actions check run `107503210879` on that SHA succeeded; AQ recorded evidence `ci-5bb5a20016345256957e022c7d0c26155575e757425a431a33df750fc042c8df`. |
| Epic PR | Completion playbook run `94aa7a86bb31495d9b267a21041ec285` opened [PR #3](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/3) at that same head. |
| GitHub approval | The test run submitted review `5300286371` through the fixture owner's `ElectricJack` GitHub User account after inspecting the one-file PR diff. AQ's live poller recorded exact-head evidence `review-7e446a16-72cf-5aa5-ba7b-e4a6ce835436` and armed the settling window at `2026-09-24 05:45:23 UTC`, firing at `05:50:23 UTC`. This was an operator-account test action, not an independent human review. |

The fixture workflow checks for `README.md` on parent refs. On
`aq/integration/*`, it additionally requires `train-repaired.txt`. Thus the
first train candidate is designed to fail hosted CI, and a repair must update
the same candidate before promotion.

## Findings during the run

- Private GitHub App repos exposed an unauthenticated `git fetch origin` in
  workspace handoff. The code now uses the authenticated repository fetch and
  compares the fetched default branch for detached verifier workspaces.
- The committed train policy stored artifact `compiled_at` timestamps while
  durable artifact rows had `null`, so a copied policy failed preflight. The
  policy now uses the durable value.
- AQ had no live ingestion of GitHub PR reviews into train evidence. The new
  poller validates the exact PR repository, branch, head and human review
  commit before recording an approval or rejection.
- The reviewer child produced no code receipt. A direct scratch call to
  `ParentCompletion.record_disposition` recorded no-op receipt
  `e18ad89b-a9fe-4426-bfdd-b9fc4e3dc8d9` after checking its completion,
  checkpoint, origin base and tree. A supported disposition route is tracked
  by `bold-ridge`.
- The parent playbook rejected two valid hosted CI evidence events with
  `invalid_evidence` (runs `42fba69b9bd041808582d0c3ee646836` and
  `2a7f2f33ecbe448b936e20c985b81fd0`). The command's
  `HierarchyIntegration.verify_parent` first requires a verifier-owned
  workspace checkpoint. The pool verifier was unclaimable, so that proof was
  absent and the command mapped the refusal to `invalid_evidence`. A direct
  scratch call to `ParentCompletion.verify_parent` with the same durable
  evidence succeeded; the normal completion playbook then opened the PR.
  The command behavior is tracked by `noble-cascade`.
- A scratch verifier profile configured as a pool could not claim its READY
  delegate because the materialized-origin query only exempts repair
  delegates. The corrected query admits only an active parent's exact
  verifier reservation. This routing gap was first tracked by `keen-zenith`.
- Candidate `410dbe97f7d0c792aa296b3341dd568bd31ae4f6` failed its hosted
  **push** check (run `35961738720`, check `107511582367`) as designed, while
  a newer **pull_request** check on that SHA passed (run `35961741426`, check
  `107511590455`). AQ selected the newer check without binding it to the
  required push event and falsely prepared green promotion. GitHub branch
  protection rejected the attempted `main` push, so `main` stayed at
  `5eb3dd6e217a435c7fb912c90fabba511910a20d`. Scratch database `3c`
  retains the prepared external mutation intent for audit and is not reused.
  The observer now filters required check suites to push workflow runs;
  a live read of the same SHA selected the failed push check.
- In the corrected run, the reserved verifier entered the claim frontier but
  workspace preparation rejected the epic's legitimate `aq/epic/...` branch
  because it expected `aq/<parent_id>`. The verifier now accepts its parent's
  exact recorded branch and owner fence. Its parent verification command also
  initially looked for a workspace held by the paused parent. The live scratch
  run proved the attached verifier workspace and exact pushed head. Current
  `main` independently changed this command to validate the locked parent
  generation, receipt-derived head, and durable CI evidence after the writer
  workspace may have been released; that fix was retained in the PR merge.

## Corrected isolated run

| Boundary | Durable evidence |
| --- | --- |
| Leaf and review | `bright-zenith.1` published `a57a7392d55b991fc4dd7eeaf41d6541d756a71b`; pool reviewer `bright-zenith.2` approved that head and closed no-op. The reviewer no-code disposition required a direct scratch service call; receipt `12f7ca91-a12f-47a6-a215-2e14698a35d9` pins the original checkpoint. |
| Parent collection | Receipt `receipt-05346e8d-94d4-5f7a-be0f-464938e18d6a` advanced `aq/epic/prove-isolated-corrected-train-delivery` to `ac49fa2eae8ca2f23cbf27d38bb0e18b686d570e`. |
| Parent verifier | Pool verifier `verify-0ab76b02-6d48-4e9a-beca-9eba3e638910` claimed the published branch under an attached verifier owner. The corrected command recorded verification `468ae30f-98c8-471c-9c06-c0b2e2272d6a`; reviewed playbook run `161998540fc04410903c3ff602a8abb7` completed the parent. |
| Epic PR | AQ opened [PR #5](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/5) at `ac49fa2eae8ca2f23cbf27d38bb0e18b686d570e`. The one-file diff adds only `train-fixture-change-4.txt`. Fixture owner `ElectricJack` approved that exact SHA via GitHub User review `5300703649` at `2026-09-24 06:37:19 UTC`; this is an operator-account test action, not an independent human review. |
| Seal | The five-minute approval window fired at `06:42:46 UTC`. AQ sealed `bright-zenith` in batch `integration-batch-1d0561b7b052578d1217b10217514bab` with immutable source head `ac49fa2eae8ca2f23cbf27d38bb0e18b686d570e`. |
| Candidate revision 0 | AQ built `26184db52775752aeded96702f6cbfb0af658948` on the integration branch. Hosted **push** check `107524860424` failed (run `35966089578`); a later pull-request check on the same commit succeeded. Corrected evidence `ci-fe06b86c7dce6830d7424a0246bb86f9b094c6f143d1e0e191bfbb38129bcfee` classified the candidate red and the root playbook dispatched bounded repair. |
| In-place repair | A pool repair delegate pushed `df146fb8c5a1778158968a02f793186794d8ab02` on the same integration branch, adding `train-repaired.txt`. The first stage expired during diagnosis; a resumed debug stage adopted that exact commit as immutable candidate revision 1. Hosted push run `35967157079` succeeded; AQ recorded `ci-8c702d546443b469d7789f882b3255efe99fed251f3a0a0d5c65ab29e52f30e8` and aggregate attestation `ci-aggregate-6393de57c1fcf4a3d960361dec7b16f3360302e2b11b2fc4f1f5c433d1d63ed3`. |
| Promotion and release | Root playbook run `fc97077fd5ca42aba67605d664e5c26c` promoted exact revision 1. GitHub `main` and AQ's `final_main_sha` both equal `df146fb8c5a1778158968a02f793186794d8ab02`. AQ marked repair passed and released the batch at `07:21:48 UTC`; [PR #5](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/5) and [audit PR #6](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/6) both show merged. |
| Cleanup | AQ completed the audit PR, source PR, and four remote-ref cleanup items. The retained local repository had an older `410dbe97f7d0c792aa296b3341dd568bd31ae4f6` ref from the first scratch database under the *same deterministic integration branch name*. The local-ref item's expected SHA was revision 1, so AQ correctly marked this one item `conflict` with `local ref moved after delivery`. Its database guard forbids reopening a terminal conflict. The stale scratch-only ref was removed with an exact old-OID check; the durable item remains `conflict` and is disclosed here. |

The reviewer no-op disposition required a direct scratch service call; a
supported route is tracked by `bold-ridge`. The second run's parent verification
used the pool command and reviewed playbook. The candidate audit PR #6 was
manually preseeded with AQ's exact idempotency marker because the first scratch
database had already closed PR #4 and occupied the deterministic branch name;
AQ verified and adopted #6. The repair stage was resumed by the scratch
operator after debugging consumed its deadline. Pool close handoffs required
the supported `integration release-owner` command after fake-provider session
stops; the code now handles the full integration ref and short task branch in
claim, repair, recovery and release paths. All GitHub fetches, checks, review
observations and the main push were live. The repository and scratch database
are retained for inspection. The final cleanup state is **conflict**, due to
the cross-database local ref collision described above; this run therefore
does not prove a wholly complete cleanup aggregate.

## Clean second batch in the same disposable database

The retained local ref from the first scratch database was removed with an
exact old-OID check before this batch. A new epic and branch avoided the
first run's cross-database identity collision. The scratch project used the
same private GitHub repository and dedicated database; no production daemon
or operator database was changed.

| Boundary | Durable evidence |
| --- | --- |
| Leaf, review, and no-code disposition | `quick-beacon.1` published actual child head `b596f22d6066714af20d6e45541711b219cc1726`. `quick-beacon.2` approved it in evidence `review-a6156a89-c02c-5301-8e95-928524a1a2a7` and closed no-op. Direct scratch disposition receipt `bf827690-c702-4b77-9977-27e56736b9cc` recorded the reviewer's no-code outcome. |
| Parent and PR | Collection receipt `receipt-1500856d-aeec-5ab6-a71c-299cffbf6add` advanced the epic to `458371929d5c9859a5605f8c39f008d6edd86253`. Pool verifier `verify-4c222a51-6da1-4eb3-b1e9-69f110aed2bb` and reviewed playbook run `561a5c73-6099-49c6-8481-a3c57a7b4809` completed the parent. AQ opened [PR #7](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/7); fixture owner `ElectricJack` approved its exact head in GitHub User review `5301313065` at `07:43:17 UTC`. This is an operator-account test action, not an independent human review. |
| Seal and red candidate | Batch `integration-batch-a785d88e4b2f7eb6991d9bac73b665b5` sealed the exact parent head. Revision 0 `5aa423716547f28de99eaee84b7a0159b4c0f086` failed hosted push CI (run `35971199332`), recorded as `ci-7bd454815e4cdb50b3b2374ab3c6fe743c973727983ca5c3acd407e28dc6702c`. AQ opened audit [PR #8](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/8) and dispatched stage-0 repair. |
| In-place repair and green evidence | The pool repair delegate restored `train-repaired.txt`, pushed `72ce5b2d100ca1d44758a7bb824b803692d6d779` on the same integration branch, and closed successfully. AQ adopted that SHA as revision 1. Hosted push run `35972066283` succeeded; evidence `ci-4df5d759791be4f32d5cf334dd9f1a71efa770a357863057b54553aa7a97282b` and aggregate `ci-aggregate-aaf7fbb251fb078848813c46aa97c79b5e7e323d641e5619e8f9d792ac4ead72` pinned the exact candidate. |
| Promotion, cleanup, and release | GitHub `main`, AQ `tested_candidate_sha`, and AQ `final_main_sha` all equal `72ce5b2d100ca1d44758a7bb824b803692d6d779`. [PR #7](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/7) and [audit PR #8](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/8) are merged. The batch lifecycle is `promoted`, cleanup state is `complete`, all seven durable cleanup items are `complete` (including the local ref), and the batch was released at `07:58:16 UTC`. |

This second batch also exposed two additional bugs. The prior batch's
settling-window timestamps had not been cleared on seal, so the new approval
was sealed about 34 seconds later against the previous, expired cap. The
service now clears the window after both empty and nonempty seals, with a
regression test. The corrected first batch above did exercise the full
five-minute window; this second batch exercised the red-to-green and wholly
complete cleanup sequence. Candidate publication after repair initially
waited because its recovery guard compared a short task branch with a full
`refs/heads/` batch ref. The guard now compares the short names, with an
exact closed-repair regression test. The scratch operator replayed the
idempotent candidate build and CI observation commands after loading this
fix; the reviewed root playbook performed the exact-SHA promotion and cleanup.
The fake session provider now implements authoritative stopped-session
confirmation from its live in-memory registry so its drained pool verifier
could be safely released without bypassing the branch-owner fence.
The reviewed root playbook has no `integration.repair_delegate_closed` rule;
the outbox event retried with `no enabled matching playbook durably accepted
the event`, so automatic revision-1 publication did not occur. The supported
idempotent build and CI commands completed the disposable proof. Task
`smart-stone.5` records the missing automatic continuation as a train-cutover
prerequisite before enabling a production train.
