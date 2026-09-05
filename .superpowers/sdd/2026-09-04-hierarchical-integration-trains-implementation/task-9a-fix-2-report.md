# Task 9a Fix Round 2 Report

## Identity and scope

- Rereview HEAD at start: `2be635579360726d0fcd7cb78be80c7325c9321f`.
- Required runtime/report fix base: `989789de12115dea9ce5beffca26119e8e3535fd`.
- Runtime/tests commit: `065cbe45370d7670b937eb04d071476d403ce66d`.
- Read `task-9a-rereview-1.md` completely and verified the remaining Critical
  finding against the pre-populated credential pipe in `src/git/manager.py` and
  `src/git/askpass_fd.py` before editing.
- Read the required TDD `writing-good-tests.md` reference before writing the
  regression.
- Scope is only the Task 9a credential-channel finding. Accepted CI subject
  binding, configuration closure, and malformed-ID behavior were not changed.
  No Task 9b, Task 10, Task 11, live network, live credential, forge, project,
  daemon, or operator-database action was performed.
- No schema or migration changed; SQLite/PostgreSQL migration proof is not
  applicable.

## Break and root cause

The inherited FD was a pipe pre-populated with the installation token. Every
process in Git's controlled process group inherited the read end. Therefore an
arbitrary descendant could read the token before askpass, retain the FD, or
consume the only credential and deny it to askpass. The pipe was non-seekable,
but it was still a shared bearer-secret channel rather than an askpass-only
request capability.

The initial real regression used a fake Git leader that spawned an unrelated
descendant. That descendant read the inherited FD and captured the exact dummy
token. The process also retained the request capability while the packaged
askpass helper attempted username, first password, and second password prompts.

### Required RED

Command:

`timeout 10s pytest -q tests/test_git_app_auth.py::test_descendant_cannot_drain_or_retain_askpass_credential_channel`

Result: **1 failed, 2 warnings in 0.49s**, exit 1. The expected failure was:
`captured.read_bytes()` contained `b'installation-token-sentinel'` instead of
`b''`. This directly reproduced the reviewed credential theft against the
pre-populated pipe.

Fix-attempt count: one protocol implementation made the real drain/retain
regression green. A later self-review strengthened the same regression against
argv-only helper impersonation and found one additional fail-closed validation
gap; that exact RED/GREEN is recorded below and was fixed without changing the
protocol.

## Broker protocol and threat model

- The only descriptor inherited by Git is one end of a Linux/POSIX `AF_UNIX`
  datagram socketpair. It is a request channel and never contains token bytes.
- The daemon retains the credential in a mutable `bytearray`. The broker begins
  only after the Git leader PID/process-group ID exists.
- The packaged helper handles the exact username prompt locally and sends no
  broker request. For the exact password prompt, it creates a private stream
  socketpair and passes only the private reply descriptor to the daemon with
  `SCM_RIGHTS` alongside a bounded, versioned request containing the exact
  authority, literal repository destination, and prompt.
- `SO_PASSCRED`/`SCM_CREDENTIALS` supplies the actual sending PID/UID/GID. The
  broker requires the daemon UID, the controlled Git process group, ancestry to
  the Git leader, the exact system Python interpreter, the exact resolved
  packaged helper script argv position, and the exact prompt. Merely placing
  the helper path and prompt elsewhere in an attacking descendant's argv does
  not authenticate it.
- Malformed, truncated, wrong-descriptor-count, wrong-payload, or untrusted
  requests have every received descriptor closed and receive no bytes. They do
  not consume the credential.
- The first valid password request receives the token only through its private
  reply endpoint. The broker then closes the reply and shared broker socket,
  overwrites and clears the mutable token buffer, and terminates. A second
  request receives nothing.
- The broker uses the event loop's readable callback and bounded socket send;
  it creates no worker thread and never awaits shared-channel EOF. A descendant
  retaining the inherited request FD cannot hold broker completion open.
- Absence of `SO_PASSCRED` or `SCM_RIGHTS` fails closed before privileged Git
  starts. No pre-populated-FD fallback exists.

The protected threat boundary is a malicious/unrelated descendant of the
credentialed Git process that can read inherited environment and descriptors,
send arbitrary requests, retain descriptors, and forge request bytes/argv
content. It cannot become the exact packaged helper invocation backed by the
pinned system Python process while preserving its attack program. Existing
round-1 isolation still removes checkout-controlled hooks/config/helpers and
unrelated daemon environment before this broker is used.

## Lifecycle behavior

- Spawn failure closes both socketpair ends and overwrites the token without
  starting a broker task.
- Broker timeout closes its socket and token buffer on its own deadline; a
  descendant-held request FD does not delay it.
- Git timeout and caller cancellation kill and reap the entire new process
  group, settle/cancel the broker task, close request/private reply endpoints,
  and leave no broker task or FD-count delta. Cancellation re-raises
  `CancelledError`.
- Normal leader exit is followed by a process-group kill/reap to remove a
  retaining descendant before broker settlement. The legitimate first helper
  response remains successful, the second is empty, and no orphan remains.
- Each lifecycle regression compares `/proc/self/fd` counts before/after and
  checks `asyncio.all_tasks()` for surviving `serve_one_credential` tasks.

## RED/GREEN evidence

1. Required drain/retain GREEN after the broker implementation:

   `timeout 10s pytest -q tests/test_git_app_auth.py::test_descendant_cannot_drain_or_retain_askpass_credential_channel`

   Result: **1 passed, 2 warnings in 0.51s**. After adding malformed-request,
   argv/environment leakage, and second-request assertions, the same node was
   **1 passed, 2 warnings in 0.55s**.

2. Focused file before the lifecycle additions:

   `timeout 30s pytest -q tests/test_git_app_auth.py`

   Result: **10 passed, 2 warnings in 1.22s**.

3. Spawn failure, broker timeout, Git timeout, and cancellation lifecycle
   subset:

   `pytest -q tests/test_git_app_auth.py -k 'broker_timeout or unsupported_credential or timeout_or_cancellation or spawn_failure'`

   Result: **5 passed, 7 deselected, 2 warnings in 1.21s**.

4. Focused file after lifecycle coverage:

   `timeout 30s pytest -q tests/test_git_app_auth.py`

   Result: **12 passed, 2 warnings in 1.58s**.

5. Both required kernel capabilities fail closed:

   `timeout 15s pytest -q tests/test_git_app_auth.py::test_unsupported_credential_broker_fails_closed`

   Result: **2 passed, 2 warnings in 0.55s** after parameterizing
   `SO_PASSCRED` and `SCM_RIGHTS`.

6. Self-review impersonation RED: the malicious probe was invoked with the
   genuine helper path and exact password prompt as extra argv. With the initial
   contains-anywhere validation, the same required descendant node failed:

   `timeout 10s pytest -q tests/test_git_app_auth.py::test_descendant_cannot_drain_or_retain_askpass_credential_channel`

   Result: **1 failed, 2 warnings in 0.59s**, because the probe captured
   `b'installation-token-sentinel'`.

   The smallest fix requires the actual sender to be the exact `/usr/bin/python3`
   packaged-helper invocation with only the expected script and prompt argv.
   The same node then returned **1 passed, 2 warnings in 0.51s**.

## Final affected-area verification

Exactly one combined affected auth/Git gate was run:

`aq test tests/test_git_app_auth.py tests/test_git_manager_async.py tests/test_github_app.py -x`

Result: **182 passed, 11 warnings, 0 failed in 10.06s** (`-n 3`, slot 1 of
2). The gate covered the broker protocol, containment, async Git manager, and
GitHub App authentication area. The exact sender-argv hardening described in
item 6 arose in the subsequent source self-review; only its single real
descendant node was rerun, rather than violating the instruction to run one
combined gate.

Final changed-file lint and whitespace command:

`ruff check src/git/askpass_broker.py src/git/askpass_fd.py src/git/manager.py tests/test_git_app_auth.py && git diff --check`

Result: `All checks passed!`, exit 0; `git diff --check` emitted no output.
After the exact sender-argv hardening, the two changed files were checked again:

`ruff check src/git/askpass_broker.py tests/test_git_app_auth.py && git diff --check`

Result: `All checks passed!`, exit 0.

The warnings are existing `pkg_resources` namespace and `discord.audioop`
deprecations. They were not introduced or broadened into this fix.

## Files and reconciliation

- `src/git/askpass_broker.py`: Linux credential request channel, kernel sender
  authentication, private reply-FD handling, one-shot release, bounded event-loop
  lifecycle, descriptor cleanup, and mutable-buffer overwrite.
- `src/git/askpass_fd.py`: packaged askpass request protocol, local username,
  helper-private reply socket, and no inherited token channel.
- `src/git/manager.py`: broker ownership around isolated privileged Git spawn,
  minimal request-only environment, process-group/broker settlement on every
  exit path, and HTTPS fail-closed proof that a credential was served.
- `tests/test_git_app_auth.py`: real descendant theft/retention/impersonation,
  valid one-shot helper, username/nonmatching prompt, second request,
  argv/env/sentinel leakage, spawn/timeout/cancel/leader cleanup, unsupported
  kernel capabilities, FD/task leaks, and orphan-process coverage.

Every required behavior has a real subprocess/socket regression. Tests do not
assert source text or substitute a mocked Git spawn for the credential boundary.

## Self-review and concerns

- Reviewed the complete four-file diff and the privilege boundary from token
  acquisition through buffer overwrite. Token bytes are never placed in argv,
  the inherited request channel, Git config, process environment, logs, or
  exceptions.
- Reviewed every descriptor owner: helper request duplicate, helper-private
  pair, received `SCM_RIGHTS` copies, daemon socketpair, and subprocess inherited
  request end all have deterministic close paths. Malformed ancillary data closes
  all received descriptors.
- Reviewed broker-task ownership across channel creation, spawn failure, leader
  success, nonzero exit, timeout, cancellation, and post-spawn exception. There
  is no `asyncio.to_thread`/executor lifecycle and no wait for descendant EOF.
- Reviewed process authentication after the source audit: PID credentials,
  process group, ancestry, interpreter, exact helper argv, prompt, authority,
  and repository all bind the release. The strengthened real descendant test
  proves argv substring injection is insufficient.
- No known blocker remains in this fix scope. The broker intentionally supports
  Linux `/proc`, `SO_PASSCRED`, and `SCM_RIGHTS` only and fails closed elsewhere,
  matching the approved POSIX/Linux design.

## Commits

- Runtime/tests: `065cbe45370d7670b937eb04d071476d403ce66d`.
- Report: recorded by the subsequent documentation commit.
