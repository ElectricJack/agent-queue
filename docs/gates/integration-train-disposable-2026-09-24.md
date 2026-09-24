# Disposable integration train proof — 2026-09-24

This run uses the retained private GitHub repository
`ElectricJack/aq-gh615-app-fixture-20260923` (repository ID `1384141153`)
and an isolated AQ daemon/database under `/tmp/aq-train-smart-stone-3c`.
The production AQ database and daemon were not changed. The scratch project
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
  delegates. This routing gap is tracked by `keen-zenith`.

The no-op disposition and parent verification service calls are coverage
limits for this run; all GitHub fetches, hosted checks and PR creation above
were live. The repository, scratch database and playbook run records are
retained for inspection.
