# Task 9a Re-Review — Fix Round 2

## Finding verdict

1. **The inherited credential channel is pre-populated with token bytes and drainable/retainable by arbitrary Git descendants — NOT ADDRESSED.** The new shared descriptor is now request-only: `make_request_channel()` creates an `AF_UNIX` datagram pair with `SO_PASSCRED`, and token bytes remain in the daemon's mutable buffer until the broker accepts a request carrying one private reply FD (`src/git/askpass_broker.py:17-35`, `src/git/askpass_broker.py:94-174`). Username and nonmatching prompts remain local, while an exact password prompt creates a helper-private reply pair (`src/git/askpass_fd.py:24-69`). This fixes pre-population, retained-request-FD hangs, malformed-message descriptor closure, one-shot response, and normal buffer clearing.

   The sender check is nevertheless trivially forgeable by the exact arbitrary descendant in the threat model. `_is_packaged_helper()` accepts any same-UID process in the Git leader's process group whose ancestry reaches the leader and whose current executable/argv are `/usr/bin/python3 <packaged-helper> <exact-prompt>` (`src/git/askpass_broker.py:46-76`). An arbitrary Git descendant already inherits the request FD and all request fields. It can invoke that exact public helper command before Git's legitimate prompt and redirect its stdout; the broker cannot distinguish that attacker-launched helper from the intended one. Indeed, the added fake-Git test itself launches `"$GIT_ASKPASS" <exact-password-prompt>` from an arbitrary fake Git leader and receives the token in a caller-selected output file (`tests/test_git_app_auth.py:394-436`). Its malicious probe tests only a Python process with the helper path appended as extra argv (`tests/test_git_app_auth.py:368-390`, `tests/test_git_app_auth.py:402-403`), so it does not test the straightforward exact-helper invocation that the broker authorizes.

   **Severity: Critical.** The app installation token can still be released to an arbitrary Git descendant, so the daemon-only/one-consumer credential boundary remains open. Keep the FD-backed broker, but authorize the actual Git transport topology rather than forgeable leaf argv alone: require the askpass sender's direct parent to be the pinned Git installation's root-owned, non-group/world-writable `git-remote-https` executable resolved through the trusted Git exec-path/inode, with exact production remote-helper argv, PGID, ancestry to the leader, and exact prompt/repository. A same-UID fake-Git/Python descendant invoking the packaged helper directly must receive no bytes. Pin and validate the packaged helper's own regular-file owner/mode/inode as well, and add a regression where the attacker invokes the exact helper command before the real transport prompt.

   A local loopback-only Git trace confirmed the supported host's real topology: top-level `/usr/bin/git` starts the dashed remote helper, and `/usr/lib/git-core/git-remote-http` directly starts askpass. On this host `git-remote-https` is a root-owned mode-0755 symlink resolving to that same root-owned `git-remote-http` inode. The remediation should resolve the pinned HTTPS helper/inode rather than compare a symlink spelling. This topology is Git-installation-specific, so unsupported layouts must fail closed and need an explicit supported-host probe/test rather than falling back to descendant argv.

## New breakage in the fix diff

1. **Important — early failures bypass token zeroization, and broker setup failure can also strand its socket/task.** `token_buffer` is created before three awaited isolated-import operations (`src/git/manager.py:2998-3029`), but the only general zeroizing `finally` begins after those operations and after request-channel creation (`src/git/manager.py:3032-3131`). A missing/invalid source repository, import timeout, cancellation, or verification failure therefore exits without calling `zeroize(token_buffer)`. Separately, `serve_one_credential()` builds the request payload before entering its `try/finally` (`src/git/askpass_broker.py:115-130`); if payload construction fails, its broker socket is not closed and its token buffer is not cleared. Manager settlement then propagates the completed task's exception before setting `broker_task = None`, so its own conditional zeroization is skipped (`src/git/manager.py:2928-2937`, `src/git/manager.py:3114-3131`). This violates the required all-exception cleanup and can retain credential bytes (plus a socket in the broker-setup case) until later object collection.

   **Minimal remediation:** establish an outer `try/finally` immediately after constructing the mutable token buffer and unconditionally zeroize it there; make channel/task variables owned by that same scope. Move expected-payload construction inside `serve_one_credential()`'s `try/finally`. Make broker settlement absorb/translate broker failure only after deterministically closing the channel and clearing the buffer, while preserving caller `CancelledError`. Add focused import-failure/cancellation and oversized/invalid-request-setup cases that assert explicit zeroization, closed FDs, no surviving task, and bounded completion.

## Out-of-scope observations

None.

## Checks

- Read the prior rereview, fix-round-2 report, and complete `2be63557..4020488b` review package.
- Inspected the current broker, packaged helper, manager ownership/cleanup paths, and all amended credential tests.
- Ran one read-only, loopback-only `/usr/bin/git` TRACE2 diagnostic against a temporary in-process HTTP 401 responder to establish askpass ancestry. It made no external network request and no repository or forge mutation. The trace showed top Git -> dashed `git-remote-http` -> askpass; host inspection showed the HTTPS helper resolves to the same root-owned executable inode.
- Did not rerun the reported 182-test affected-area gate.

## Verdict

**Fix round: Findings remain open.** Open findings: **Critical 1, Important 1**. The request-only broker is a substantial improvement, but exact helper argv is still launchable by any arbitrary descendant, and early exception paths do not reliably clear the token buffer and broker resources.
