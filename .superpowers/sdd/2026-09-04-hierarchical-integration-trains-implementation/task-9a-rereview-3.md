# Task 9a Re-Review — Fix Round 3

## Finding verdicts

1. **Same-UID/same-PGID exact-helper invocation can impersonate the intended credential consumer — ADDRESSED.** The broker now pins `/usr/bin/git`, `/usr/bin/python3`, the trusted Git exec-path's resolved HTTPS remote-helper device/inode, and the packaged askpass path/device/inode/content digest after enforcing regular-file ownership and non-group/world-writability (`src/git/askpass_broker.py:20-95`). At release it rechecks the Git leader executable, sender UID/PGID/interpreter/exact askpass argv, packaged helper identity and digest, direct parent executable/device/inode, exact `git-remote-https <frozen-url> <frozen-url>` parent argv, and ancestry to the leader (`src/git/askpass_broker.py:132-207`). Missing `/proc` data or a changed/unsafe file fails closed; there is no argv-only fallback.

   The production manager derives this topology itself through pinned `/usr/bin/git --exec-path` under the minimal environment and supplies it directly to the broker (`src/git/manager.py:2985-2997`, `src/git/manager.py:3064-3067`, `src/git/manager.py:3131-3143`). A test-only replacement for `_APP_GIT_EXECUTABLE` cannot weaken credential release because the broker independently requires `/proc/<leader>/exe` to match the pinned system Git (`src/git/askpass_broker.py:177-180`). The exact-helper attacker test now receives no token (`tests/test_git_app_auth.py:461-558`), while the bounded local-TLS test exercises the real `/usr/bin/git` -> installed HTTPS remote-helper -> packaged askpass chain and receives the dummy credential once (`tests/test_git_app_auth.py:561-669`).

   Reassessment against the approved threat model found no production worker-controlled path that can manufacture a second pinned remote-helper process inside this daemon-owned process group: worker checkout hooks, checkout/local/global/system configuration, URL rewriting, proxies, credential helpers, external transports, inherited daemon environment, and executable selection were already removed in fix round 1. The hypothetical in which arbitrary native code already runs inside the trusted root-owned Git group is equivalent to compromise of the trusted transport boundary, not a reachable input supplied by the worker checkout. It therefore does not keep the Task 9a finding open.

2. **Early failures bypass token zeroization and broker setup can strand its socket/task — ADDRESSED.** `_zeroized_credential` is entered immediately after mutable-buffer construction and encloses temporary repository creation, all import/verification and topology awaits, channel creation, spawn, broker lifecycle, success, failure, and cancellation (`src/git/manager.py:76-81`, `src/git/manager.py:3028-3168`). Request-channel setup closes both endpoints on every setup exception (`src/git/askpass_broker.py:105-116`). Broker request construction is now inside the broker's cleanup `try/finally`, which always closes its channel and clears the token (`src/git/askpass_broker.py:246-306`), and settlement converts broker errors only after awaiting task completion (`src/git/manager.py:2942-2953`). The manager continues to re-raise caller `CancelledError` after group and broker cleanup (`src/git/manager.py:3150-3158`). Added behavior tests cover real import failure, cancellation during import, and oversized broker setup with empty recorded buffers, stable FD counts, no surviving broker task, and bounded completion (`tests/test_git_app_auth.py:225-330`).

## New breakage in the fix diff

None.

## Out-of-scope observations

- **Minor / Task11 enablement prerequisite:** static inode/content rechecks protect against changed packaged-helper files, but release enablement must also establish that the installed helper and its directory are outside worker write authority. The fix report explicitly carries this to Task11. This is nonblocking for disabled-by-default Task 9a; production must not enable the transport until that deployment check passes.

## Checks

- Read the prior round-two rereview, fix-round-three report, and complete `c4b4d60a..23e2fecd` review package once.
- Inspected the current manager, broker, and amended credential tests, including production topology derivation and the absence of a caller-supplied topology override.
- Confirmed the report's external-base account matches the package: runtime changes are confined to `src/git/askpass_broker.py`, `src/git/manager.py`, and `tests/test_git_app_auth.py`; the subsequent commit adds only the fix report.
- Did not rerun the reported 186-test gate. The report records 17 focused tests passing and the 186-test affected Git/auth gate passing with 11 existing warnings.

## Verdict

**PASS.** Open findings: **Critical 0, Important 0**. Both round-two findings are addressed and the fix introduces no new Critical/Important regression. The architecture does not require reconsideration within the approved Task 9a threat model; the named Task11 installed-helper authority check remains mandatory before enablement.
