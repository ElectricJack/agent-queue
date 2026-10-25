# H2 isolated test environment contract

`test_environment.py` supplies the environment factory and asynchronous Linux
launcher for B1's `pytest_main.py`. H3 owns disposable PostgreSQL setup and H4
owns `world_env` adoption. This does not activate the Bazel backend or change
production `src/env_scrub.py`.

Call `test_environment(test_tmpdir=..., runtime=..., bazel=..., output_roots=...)`
as a context manager, then await `run_isolated(environment, command, cwd=...)`.
The caller first acquires AQ's shared test slot; the launcher acquires no second
slot. Keep the context open until the launch finishes, including cancellation
cleanup. The top-level runner translates its own termination signals into
cancellation of the launch coroutine. The namespace helper independently
forwards SIGTERM/SIGINT and contains descendants if it is killed outright.

`Runtime` contains the absolute Python executable, declared input roots,
toolchain binary/library directories, optional git helper directory and declared
shell. When supplied, the shell is also mounted at /bin/sh for native helpers and
Python shell subprocesses; this never imports the host /bin/sh implicitly.
Populate it from H1's native toolchain `files`/`binaries` and the hermetic Python
runfiles. Include the complete Python standard library, native libraries, ELF
loader, git helpers, test sources/data, and symlink destinations. A runfiles
symlink tree alone does not declare its external destinations. Paths keep their
absolute spelling inside the child; disjoint declared input roots are read-only.
There is no search of host PATH, pg_config or host Python installations. The
launcher itself is a declared Python source input of the eventual Bazel rule.

The factory creates a fresh mode-0700 directory under the explicitly supplied
TEST_TMPDIR. HOME, all XDG directories, tmp, tmux state, AQ_E2E_HOME and a private
AQ_CONFIG_PATH live there. It builds a new environment with fixed locale/timezone
and toolchain PATH; it never copies ambient environment variables. Bazel protocol
variables are enumerated, runfiles paths must be declared, and output paths must
belong to separately declared, private writable output directories. The caller
passes selected Bazel variables, not `os.environ`. Unknown keys and overlapping
input/output roots fail admission. Output roots must belong to this target/shard;
never declare the host /tmp, operator home, an entire checkout containing private
state, or a shared output ancestor. These declarations are trusted build inputs.

`environment.child_env(settings)` returns an independent environment copy for
local child launchers. Explicit per-target settings cannot override isolation or
interpreter startup controls, session identifiers, tokens or database DSNs.
Inside the target, H3/H4 may add the disposable credentials and private DSN they
just created to that copy. They must never obtain those values from the host
worker. In particular the parent's AQ_DB_SCOPE/refusal sentinel stays untouched.
Pytest plugin autoload is disabled; B1 declares and loads required plugins.

The helper creates user, mount, network, IPC and PID namespaces. It maps the
caller to UID/GID 1000 inside, binds only declared inputs and private writable
roots into a new filesystem root, and mounts a private /proc. It exposes only
null/zero/random/urandom devices, private PTYs and private shared-memory storage;
host sockets and operator configuration paths are absent. Minimal passwd/group,
NSS and localhost files are generated for native account/host lookup without
reading host configuration. The executed target has no effective capabilities and cannot gain
privilege through exec. It has one network interface, private loopback. Before
admission the helper proves a loopback TCP connection works and an external
connection has no route. Namespace, mount or self-test failure returns 125;
there is no host-network fallback. Unsupported platforms receive an explicit
instruction to select the legacy backend.

The public launch deadline includes namespace setup and execution, with two
bounded cleanup grace periods. The total must fit 300 seconds (default 290 +
2×2). Timeout returns 124; signals return 128 + signal; other target statuses
are preserved. SIGTERM precedes SIGKILL when a process ignores termination.
Exiting private PID 1 causes the kernel to kill all remaining descendants,
including setsid/double-fork services. The helper's death also kills private
PID 1. No process or namespace is reused by another target/shard.

## Verification

Run the regular boundary tests through `aq test tests/test_bazel_environment.py`.
On an isolated Linux CI runner, or inside AQ admission on a namespace-capable
Linux development host, require the real poison/network/cleanup acceptance:

```bash
AQ_BAZEL_ISOLATION=1 aq test tests/test_bazel_environment.py
```

The existing outer pytest harness also needs POSTGRES_TEST_DSN pointing at the
separate disposable test server; that DSN never reaches the isolated child.
The acceptance fixture explicitly declares that runner's Python3.12 and ABI
libraries. It is a local boundary fixture, not a replacement for H1's locked
Bazel runtime. Without the flag the real namespace cases are visibly skipped;
that is not acceptance evidence. With the flag, unsupported/missing isolation
fails instead of skipping. V3 owns hosted shadow workflow integration.
