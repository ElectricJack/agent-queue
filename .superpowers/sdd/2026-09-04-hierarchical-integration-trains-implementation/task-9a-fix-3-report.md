# Task 9a Fix Round 3 Report

## Identity, external state, and scope

- Task start HEAD: `a43d92e4007c48da9f87b9888b7d65bb2a60ea48`.
- During implementation the shared branch advanced externally to
  `c4b4d60aa7a5b9b7927598dccdd1f09150b4fda2`, a merge of `origin/main`.
  The runtime commit was made on top of that merge without reset or rebase.
- `openapi.json` was externally modified at task start. I did not edit, stage,
  format, regenerate, or commit it. The external merge made it clean during the
  run; no action by this implementation changed that file.
- Runtime/tests commit: `808c7089afd97d7adc92312b1225c65d7f294bcf`.
- Read `task-9a-rereview-2.md` completely before editing and re-read the required
  test-quality/TDD reference. This is the third Task 9a credential-channel fix
  round.
- Scope is limited to process-origin authentication and unconditional credential
  resource cleanup. Accepted CI/config/malformed-ID behavior was not touched.
  No Task 9b, Task 10, Task 11, external network, real credential, forge,
  project, daemon, or operator-database action was performed.
- No schema or migration changed, so SQLite/PostgreSQL migration proof is not
  applicable.

## Supported-host topology proof before implementation

The installed topology was measured before production edits, under the same
minimal isolated environment used by the privileged Git path:

- `env -i HOME=/tmp LANG=C LC_ALL=C PATH=/usr/bin:/bin ... /usr/bin/git --exec-path`
  returned `/usr/lib/git-core`.
- `readlink -f /usr/lib/git-core/git-remote-https` returned
  `/usr/lib/git-core/git-remote-http`.
- `stat` showed `/usr/bin/git` and the final remote helper are root-owned,
  mode 0755 regular files. The HTTPS symlink resolves to the same device/inode
  as `git-remote-http` (device 2112, inode 16423 on this host).
- A loopback-only `strace -f -e trace=process` against a temporary local 401
  responder showed:

  `/usr/bin/git` leader → `/usr/lib/git-core/git remote-https <URL> <URL>` →
  `/usr/lib/git-core/git-remote-https <URL> <URL>` (resolved HTTP inode) →
  askpass as the remote helper's direct child.

No external connection was made. A second real regression uses a temporary
self-signed TLS server and dummy token to prove that this exact installed HTTPS
chain is accepted end to end.

The topology is distinguishable on this host: the legitimate askpass process
has a direct parent whose executable inode is the pinned root-owned HTTPS remote
helper, while an attacker-launched exact askpass has the fake Git/script process
as its direct parent. No fourth protocol or fallback was needed.

## Finding A: exact-helper impersonation

### RED

The regression launches a fake Git leader in a new process group. Before any
legitimate transport, that same-UID/same-PGID leader invokes the exact public
packaged helper with the exact password prompt and redirects stdout. This is a
real process/socket topology, not a source assertion or mocked spawn.

After correcting the local topology fixture to issue the two authentication
challenges Git requires before invoking askpass, the canonical RED command was:

`timeout 15s pytest -q tests/test_git_app_auth.py::test_exact_helper_launched_by_fake_git_descendant_cannot_take_credential tests/test_git_app_auth.py::test_supported_git_https_remote_helper_is_credential_origin`

Result: **1 failed, 1 passed, 2 warnings in 0.63s**, exit 1. The fake leader's
redirected output contained `b'installation-token-sentinel'`; the real installed
Git HTTPS topology already qualified under the older argv-only check.

The first diagnostic version returned **2 failed, 2 warnings in 2.57s** because
the local server returned 403 on Git's empty-password retry before Git invoked
askpass. Process tracing and a local response capture showed two HTTP Basic
rounds are required. Updating the fixture to return a second 401 made the
supported topology pass and isolated the intended impersonation failure. This
fixture correction did not change production code.

### Minimal production fix

- `/usr/bin/git --exec-path` is executed through the existing minimal isolated
  environment. The result must be an absolute root-owned directory tree with no
  group/world-writable component.
- `/usr/bin/git`, `/usr/bin/python3`, and the resolved HTTPS remote helper are
  pinned by resolved path, owner, device, and inode. Git installation executables
  must be root-owned regular files and not group/world writable. The root-owned
  HTTPS symlink spelling is retained for exact argv binding; its final target
  inode is the executable identity.
- The packaged askpass file must be a regular file owned by the daemon euid and
  not group/world writable. Its resolved path, device, inode, and SHA-256 content
  digest are pinned for the operation and rechecked at credential release.
- Kernel credentials must name the daemon uid. The exact `/usr/bin/git` leader
  PID/executable is rechecked. The sender must share its PGID and have the pinned
  system Python executable, exact pinned askpass argv, and exact prompt.
- Most importantly, the sender's direct parent must share the controlled PGID,
  match the pinned HTTPS remote-helper inode, have exact
  `git-remote-https <frozen-url> <frozen-url>` argv, and have ancestry to the
  exact Git leader. A fake Git/Python descendant cannot satisfy this parent
  identity by invoking the public helper itself.
- Missing `/proc`, unsafe/missing executables, absent `SO_PASSCRED`/`SCM_RIGHTS`,
  or unexpected process topology fails closed. There is no argv-only fallback
  and no caller-supplied trust override in the production manager path.

### GREEN

The exact RED command after implementation returned **2 passed, 2 warnings in
0.64s**. The fake exact-helper invocation received zero bytes, while the local
TLS test exercised `/usr/bin/git` and the real pinned HTTPS remote helper and
served the dummy token exactly once.

## Finding B: unconditional cleanup ownership

### RED

Command:

`timeout 15s pytest -q tests/test_git_app_auth.py::test_source_import_failure_zeroizes_dummy_credential tests/test_git_app_auth.py::test_cancellation_during_source_import_zeroizes_dummy_credential tests/test_git_app_auth.py::test_oversized_broker_request_setup_closes_and_zeroizes`

Result: **3 failed, 2 warnings in 0.40s**, exit 1.

- A real invalid source repository failed during the isolated fetch after the
  mutable dummy buffer was constructed, but recorded no zeroization.
- Cancellation at the first awaited isolated-import boundary propagated, but
  recorded no zeroization.
- Oversized request-payload construction raised before the broker's old
  `try/finally`; the full dummy buffer remained and its broker socket stayed
  owned by the caller cleanup.

### Minimal production fix

- A zeroizing context is entered immediately after mutable buffer construction
  and surrounds temporary repository creation, every import/verification,
  topology discovery, socket setup, spawn, broker lifecycle, success, failure,
  and cancellation. Its unconditional `finally` overwrites and clears the
  buffer.
- Request-payload construction moved inside `serve_one_credential`'s
  `try/finally`, so invalid/oversized setup closes the broker socket and clears
  the buffer.
- Request-channel setup now closes both socketpair ends if `SO_PASSCRED` setup
  fails.
- Broker settlement always cancels if necessary and awaits the task before
  translating its failure to a safe false result. Caller `CancelledError`
  remains explicitly re-raised by the manager.
- Existing descriptor-count/malformed request handling still closes every
  received private reply FD. The event-loop reader creates no worker thread and
  does not wait for descendant EOF.

### GREEN

The exact cleanup RED command returned **3 passed, 2 warnings in 0.35s**.
Each manager test observed an emptied mutable buffer, stable `/proc/self/fd`
count, no surviving broker task, bounded completion, and correct cancellation
propagation. The oversized direct broker test observed an emptied buffer, closed
broker socket, stable FD count, and bounded exception completion.

## Focused and affected-area verification

Full focused file:

`timeout 45s pytest -q tests/test_git_app_auth.py`

Result: **17 passed, 2 warnings in 1.69s**.

Exactly one combined affected Git/auth gate was run:

`aq test tests/test_git_app_auth.py tests/test_git_manager_async.py tests/test_github_app.py -x`

Result: **186 passed, 11 warnings, 0 failed in 9.85s** (`-n 3`, slot 1 of 2).

Changed-file lint and scoped whitespace check:

`ruff check src/git/askpass_broker.py src/git/manager.py tests/test_git_app_auth.py && git diff --check -- src/git/askpass_broker.py src/git/manager.py tests/test_git_app_auth.py`

Result: `All checks passed!`, exit 0; scoped `git diff --check` emitted no
output. Staging named only those same three files, so no external merge or
OpenAPI state entered the runtime commit.

The warnings are pre-existing `pkg_resources` namespace and `discord.audioop`
deprecations and were not broadened into this security fix.

## Files and deliverable reconciliation

- `src/git/askpass_broker.py`: pinned executable/file identities, supported-host
  path validation, direct remote-helper parent authentication, exact process
  topology rechecks, safe socketpair setup, and request-setup cleanup.
- `src/git/manager.py`: minimal-env exec-path discovery, production-owned pinned
  topology passed into the broker, outer unconditional mutable-buffer ownership,
  and deterministic broker failure settlement.
- `tests/test_git_app_auth.py`: exact-helper attacker topology, real local TLS
  `/usr/bin/git` HTTPS topology, source-import failure/cancellation zeroization,
  and oversized request setup/FD/task cleanup.

No test-only topology override was added to the production manager call. The
manager derives and pins the installed Git/askpass identities itself before
credentialed spawn.

## Self-review and concerns

- Traced the supported production topology before designing the check, then
  verified the real local TLS chain after implementation. The broker binds the
  direct transport parent, not merely descendant argv or ancestry.
- Reviewed every mutable-buffer exit from construction onward: import failure,
  verification failure, topology failure, channel failure, spawn failure,
  broker setup failure, timeout, cancellation, nonzero exit, and success all
  reach unconditional overwrite.
- Reviewed FD owners for socketpair creation, subprocess inheritance, ancillary
  descriptor rejection, valid private response, task cancellation, and setup
  exceptions. No thread or EOF-dependent lifecycle remains.
- Supported-host behavior is intentionally Linux-specific and fails closed when
  `/proc`, Unix credential passing, the pinned Git layout, ownership, or modes
  differ.
- Development checkout askpass is euid-owned mode 0700 and its inode/content are
  pinned. Production packaging must place this helper outside any path writable
  by worker authority; Task 11 enablement must fail if the installed helper path
  is worker-writable. That enablement/configuration work is intentionally out of
  Task 9a scope.
- No open implementation blocker remains for the measured supported host.

## Commits

- Runtime/tests: `808c7089afd97d7adc92312b1225c65d7f294bcf`.
- Report: recorded by the subsequent documentation commit.
