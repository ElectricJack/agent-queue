# GitHub access #615: live App-only repair rerun

Recorded 2026-09-23 UTC for `steady-flare`. This is the App-only rerun on the
authorized disposable repository. The earlier mock and partial live evidence
is in [github-access-615.md](github-access-615.md).

## Isolation and source

- Source commit: `9cd4481321b59053154a275d6e0433ec25c80ce5`.
- Disposable private repository: `ElectricJack/aq-gh615-app-fixture-20260923`,
  numeric ID `1384141153`. App ID `5052310`, installation ID `164168761`.
- Separate fixture daemon on port `18155` and disposable database
  `aq_e2e_gh615_app_20260923`; stopped after the run. `gh` version `2.45.0`.
- The daemon used an empty `GH_CONFIG_DIR`, no `GH_TOKEN`, `GITHUB_TOKEN`,
  `GH_ENTERPRISE_TOKEN`, or `SSH_AUTH_SOCK`, disabled global/system Git config
  and SSH identity discovery. Before startup, `gh auth status` exited 1 and
  reported no GitHub login. The verifier used fresh, single-repository App
  installation tokens in its own process for read-only `gh api` calls and the
  final disposable branch cleanup.

## Remote results

| Step | Observed result |
| --- | --- |
| `github_clone` onboarding | AQ succeeded for repository `1384141153`; cloned `main` at `206e0b7c1d93415ae470a34a3569dac3f099e84f`. |
| Fixture task and commit | `quick-current` claimed branch `aq/quick-current`; harmless commit `2afdd98e463471df4b669fcda972053d5c3cad25`. |
| AQ Git push | Returned pushed branch `aq/quick-current` at `2afdd98e463471df4b669fcda972053d5c3cad25`; App-token `gh api` remote-ref read returned HTTP 200 with the same OID. |
| PR create and view | AQ created [PR 1](https://github.com/ElectricJack/aq-gh615-app-fixture-20260923/pull/1). App-token `gh api` returned HTTP 200, open, head `2afdd98e463471df4b669fcda972053d5c3cad25`, base `206e0b7c1d93415ae470a34a3569dac3f099e84f`, creator `electricjack-aq-github-test[bot]`. |
| CI | App-token `gh api` check-runs read returned HTTP 200; both `fixture` checks were completed with `success`. |
| AQ PR merge | CLI returned an ambiguous response. Before any retry, App-token `gh api` returned PR HTTP 200, merged by `electricjack-aq-github-test[bot]` at `2026-09-23T23:12:53Z`, merge OID `69f6b18f5998b074b141903b6d3a51bcec9e4b84`. `main` ref HTTP 200 matched that OID; task branch ref HTTP 404 immediately after merge. |
| AQ task close | Worker `task close --outcome pass --claim-next` returned `COMPLETED`, `pipeline_ok=true`, and `drain_requested`. The daemon logged `auto-pushed delivery` for `quick-current` through `apush_validated_delivery`, so this path used the App credential too. |
| Post-close branch state | App-token `gh api` ref read returned HTTP 200 at the original task OID: close had recreated the branch that the merge deleted. Filed separate repair task `noble-forge`. The disposable branch was then deleted with the App token (HTTP 204), and a final ref read returned HTTP 404. |

The App-only clone, push, PR, CI, merge, and AQ close sequence passed without
an ambient GitHub login. The post-merge branch recreation is a separate cleanup
regression. This rerun did not exercise token-expiry refresh, concurrent
repository selection, or the existing-login leg; those retain their earlier
evidence and disposition in the main #615 gate record.
