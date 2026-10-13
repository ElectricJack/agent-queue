# Playbook script sandbox foundation

Script execution is disabled by default. This foundation supplies reviewed program
and input models, a proposal pinning seam, quota reservations, a Linux launcher and
the read-only `resources.script_sandbox` doctor check. It does **not** register
`ScriptStep`, advertise `script_v1`, admit jobs or persist output. Those integrations
belong to E2/E3 of the approved implementation-playbooks plan. Shipping these files
does not enable scripts or satisfy the separate real Linux acceptance gate.

## Ownership and trust boundary

An operator installs a fixed launcher, a dedicated non-root UID/GID, an immutable
runtime, protected storage and a delegated cgroup v2 subtree on an approved host.
Workers never install prerequisites, change delegation, edit operator policy or
enable execution. There is no soft-cgroup mode.

`src.jobs.script_launcher` reads only `/etc/agent-queue/script-sandbox.json`. No
request can select a host executable, launcher, runtime directory, cgroup or storage
root. Its public invocation accepts no arguments or exactly `--probe`. The
`aq-script-launcher` console entry point is provided for an operator installation;
the repository checkout is not a trusted installation.

The policy, package, Python environment, bubblewrap binary, runtime manifest and
their ancestors must be root-owned and not group/world writable. Install the Python
environment and all dependencies outside user-writable paths; use isolated Python
startup. A root-owned wrapper may perform the UID transition using an operator
rule restricted to this exact installed command with no arguments or `--probe`.
Do not grant a shell, arbitrary Python, the private `script_entry.py` shim,
caller-supplied paths or arbitrary argument forwarding. The resulting identity
must have only its designated primary group, no capabilities, and differ from the
daemon UID. The launcher itself refuses root execution.

The launcher holds the existing shared `BoxLock` with weight one and normal
priority (band 2), and a per-run lock. Its pre-exec shim joins an already limited cgroup
before bubblewrap or user code can fork, sets no-new-privileges and hard descriptor
and file-size limits, and closes inherited descriptors. Bubblewrap creates user,
PID, network, IPC, UTS, mount and cgroup namespaces, disables further user
namespaces and drops capabilities. Runtime and inputs are read-only; `/scratch`
is a size-limited tmpfs. `/dev` is read-only and `/dev/shm` is covered with an empty
read-only directory. Host sockets, credentials and the cgroup filesystem are not
mounted. The environment starts with fixed PATH/locale and HOME=`/scratch`.

The implementation relies on the documented [bubblewrap mount and namespace
semantics](https://github.com/containers/bubblewrap/blob/main/bwrap.xml) and
[cgroup v2 controllers and cgroup.kill](https://docs.kernel.org/admin-guide/cgroup-v2.html).
Those mechanisms must be tested together on the actual designated host. The
bubblewrap binary must support unprivileged user namespaces without setuid/setgid.

## Operator policy contract

The fixed policy file is a JSON `LauncherPolicy` with these fields. Paths and
identities are supplied by the operator; none are inferred from a worker slot.

| Field | Required meaning |
|---|---|
| `version` | `1` |
| `daemon_uid` | The daemon's real, non-root UID; execution must use another UID |
| `jobs_enabled` | Explicit job admission switch; must be true for execution |
| `lock_dir`, `capacity` | The **same** box lock directory and capacity as managed jobs |
| `scripts` | The `ScriptsConfig` object described below |

`scripts.enabled` defaults to false. `launcher` is the absolute fixed wrapper
path; `bwrap` is the absolute installed binary. `runtime_root`, `runtime_manifest`,
`input_root`, `state_root`, `cgroup_root`, `execution_uid` and `execution_gid` are
mandatory when enabled. Only `network: "none"` is supported. `env_allowlist`
permits explicit values only where the authored allowlist also permits the name;
credential names, interpreter hooks, PATH/HOME and AQ/DB/provider settings remain
forbidden even if listed accidentally.

The cgroup root must be beneath `/sys/fs/cgroup`, empty of processes, writable by
the execution UID, and already have `cpu memory pids` enabled in
`cgroup.subtree_control`. Child groups require `cpu.max`, aggregate `cpu.stat`,
`memory.max`, `memory.swap.max`, `memory.oom.group`, memory/pids event counters,
`pids.max`, `cgroup.events` and `cgroup.kill`. Setup writes and reads back every
enforced limit before spawning the private shim. Doctor only inspects delegation;
it never creates or enables the operator's subtree.

The operator provisions access to the shared box lock directory, existing lock
inodes, its protocol record and waiters directory for both identities. Preserve
lock inodes; never replace/unlink them to repair permissions. The input store is
daemon-owned, readable by the launcher, and not writable by sandboxed code. The
private state root holds per-run locks, launch identities, staging and quota
metadata. Provision daemon access for E2's recovery/retention owner without making
it generally readable or writable. Neither storage root is mounted in a sandbox.

| Ceiling | Default |
|---|---:|
| Queue (`queue_seconds`) | 300 seconds |
| Wall (`wall_seconds`, maximum 900) | 60 seconds |
| Aggregate CPU (`cpu_seconds`) | 30 seconds |
| Memory (`memory_bytes`) | 256 MiB |
| Processes (`pids`) | 32, including namespace helpers |
| Descriptors (`fds`) | 128 per process |
| Scratch (`scratch_bytes`) | 16 MiB total tmpfs |
| Each stream (`stdout_bytes`, `stderr_bytes`) | 1 MiB |
| Pinned source inputs (`input_bytes`) | 64 MiB |
| Input/protected-output reservations | 2 GiB each per host |
| Combined reservations per run (`per_run_storage_bytes`) | 64 MiB |
| Runtime (`runtime_bytes`) | 1 GiB |

Requests may narrow ceilings. CPU is charged to the whole owned cgroup, with a
one-core bandwidth ceiling and a 20 ms accounting interval. File-size and tmpfs
limits are complementary: many small files cannot evade the total scratch cap.
Caught kernel errors such as EMFILE/ENOSPC remain observable to the program;
cgroup events, SIGXFSZ, timeouts and stream overflow are classified by the backend.

## Runtime and proposal pins

A `RuntimeManifest` contains `version: 1`, a `runtime` identifier, a `probe` path
(default `usr/bin/true`) and a `files` array. Each file has canonical relative
`path`, SHA-256 `sha256`, exact `byte_count` and boolean `executable`. The probe
must be an immutable declared executable that exits zero without arguments; doctor
runs only that operator-approved program in disposable namespaces. It never runs
authored scripts or changes host configuration.

Build a flattened root tree: no symlinks, hard links, sockets, devices, setuid
files, extra undeclared files or writable file modes. Include all interpreter
libraries, loader paths and dependencies explicitly. Supply empty `proc`, `dev`,
`inputs` and `scratch` mountpoint directories. The acceptance runtime additionally
needs `usr/bin/python3`, `usr/bin/sh` and their declared dependencies. Runtime
verification hashes every file again before execution; operator updates require
new reviewed pins.

`pin_source_scripts` reads source frontmatter `scripts`, a mapping from step ID to
`ScriptProgram`. A program selects exactly one of `script`, `argv` or literal
`shell`, plus a runtime ID and, for script/shell, a literal relative interpreter.
Supporting files are explicit. Repository sources are read as blobs at an approved
full Git commit; dirty working-tree bytes are irrelevant. Scoped vault reads walk
directory descriptors without following symlinks. Content-addressed blobs are
append-only and are checked again when staged. Execution never reads live source
paths. Arguments are resolved separately; shell text is never templated.

E3 must place the returned typed `PinnedScript` values in B3's trusted execution
metadata, compare each compiled step to its source-authored program, and include
that metadata in review identity. This seam intentionally leaves old artifacts
and fingerprints unchanged. It does not trust compiler-authored digests.

## E2 private transport and settlement

After E2 verifies principal/run/invocation identity, protected-output readiness and
durable launch intent, its existing detached runner calls
`launch(settings, SandboxRequest, jobs_enabled=..., cancelled=event)` **before**
the generic runner acquires another box lock. Do not double-acquire capacity.
Do not route through the generic merged stdout/stderr log path.

`SandboxRequest` carries one opaque execution ID, run ID, reviewed pins and bounded
explicit arguments/environment/base64 stdin. The fixed helper protocol is private
JSON over pipes. EOF or a control byte requests cancellation. Raw stdout/stderr
stay in bounded memory and private pipes; never print, log, persist or return this
wire JSON through a command/API. E2 encrypts them with A3's run-owned private-value
service before durable storage and implements authorized output retrieval and JSON
parsing. The dataclass repr omits payloads but `to_wire()` deliberately contains
them; it is not a public serializer.

The backend drains both streams concurrently, counts every observed byte and stops
the cgroup on overflow. Terminal causes are recorded before signaling. TERM gets
two seconds; cgroup.kill then removes remaining descendants. Cleanup requires an
empty cgroup and reaped launcher child before settlement. Failed cleanup or broken
transport returns incomplete evidence and must enter E2 recovery, never a retry
that executes the same identity again. An exclusive `intent-<execution_id>` file
prevents local replay; E2 still owns durable job identity and recovery.

`QuotaLedger` reserves pinned/dynamic input and both maximum stream sizes before
launch under a cross-process lock. Its `quota_key` remains reserved until E2
durably retires the protected artifacts. There is no expiry-based release;
crashes consume quota conservatively. E2 must retain intent/quota identities until
its recovery and retention decisions make removal safe. It must not release a
reservation merely because submission or transport failed.

## Acceptance runbook

The operator first designates the host, owner, identities, immutable installation
and isolated test policy. Keep production script admission disabled. In that
environment, run the installed fixed wrapper with `--probe` and retain the JSON
readiness result, installed source SHA, runtime manifest digest and kernel/bwrap
versions. The daemon's `aq doctor --check resources.script_sandbox` uses the same
probe when script admission is configured; disabled reports are not readiness.
Workers have no authority to widen doctor scope or repair prerequisites.

Set `AQ_SCRIPT_ACCEPTANCE_CONFIG` to the operator-supplied policy copy matching the
fixed launcher's installed policy, and set `POSTGRES_TEST_DSN` to the documented
**disposable** test PostgreSQL service. Never use the operator database. Leave at
least one admitted slot available for the nested launcher; do not raise worker
counts or replace the shared lock directory to make a run pass.

```bash
aq test tests/test_playbook_script_sandbox.py
aq test --aq-all-markers tests/test_playbook_script_sandbox.py
aq test tests/test_playbook_private_values.py tests/test_doctor.py tests/test_jobs_runner.py
```

The default arm verifies literal policy, byte-exact Git/vault pins, malicious
paths, quotas, environment rules and real missing-prerequisite refusal. The Linux
arm requires the dedicated identity, host/loopback/pathname/abstract socket denial,
shell-data separation, simultaneous floods, CPU/memory/pids/FD/file-size/tmpfs
limits, cancellation, wall timeout and double-fork cleanup. Missing host config is
an explicit failure, never a skip. A default-only pass, mocks or a same-UID
namespace probe cannot satisfy this gate. Preserve all unmet arms in the task's
evidence and do not pass-close or enable script support until the real arm passes.
