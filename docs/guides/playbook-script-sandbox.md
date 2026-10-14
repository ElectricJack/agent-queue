# Playbook script process foundation

Playbook scripts run as ordinary processes. The October 14, 2026 decision
supersedes the original E1 sandbox design: no cgroups, namespaces, network
isolation, privileged launcher, prerequisite doctor or separate host acceptance.
`resources.scripts.enabled` defaults to **true**; setting it false refuses new
launches. Managed job admission must also be enabled. This foundation does not
register `ScriptStep` or advertise `script_v1`; E2 owns durable jobs and protected
output, and E3 owns step dispatch and activation.

## Programs and pins

`ScriptProgram` selects exactly one of a source `script` with an explicit
interpreter, `argv` with a literal executable followed by typed argument values,
or literal `shell` text with an explicit interpreter. Source paths and executable
identifiers are literal relative paths. Dynamic arguments are separate argv
entries; stdin is bounded to 64 KiB and closed after delivery. Shell parameters
receive data as positional arguments, never through command-text substitution.

The existing proposal pinning remains: `pin_source_scripts` reads the source
frontmatter `scripts` mapping and returns typed `PinnedScript` values. Repository
files come from the approved full Git revision; scoped vault reads reject path
escapes and symlinks. Declared supporting files are copied to content-addressed
storage. Execution verifies input and declared runtime file digests again. Changed
or missing bytes refuse admission and require new proposal pins.

A `RuntimeManifest` has `version: 1`, a runtime identifier and declared `files`
(path, SHA-256, exact byte count, executable flag). `runtime_root` resolves these
relative paths; `runtime_manifest` locates the manifest. These are server settings,
not command inputs. There is no runtime image or mount: host libraries, files and
network are accessible normally. Digest pins detect changes to declared bytes;
they do not sandbox a program or freeze undeclared host dependencies.

E3 must place pins in B3's trusted execution metadata, compare compiled steps to
the source program and include the metadata in review identity. Compiler-authored
digests do not establish trust. Legacy artifact and contract identities remain
unchanged by this foundation.

## Configuration and limits

`ScriptsConfig` lives under `resources.scripts`. Paths can be unset at startup;
launch requires absolute `runtime_root`, `runtime_manifest`, `input_root` and
`state_root`. No separate installation policy or identity is needed. Temporary
input and scratch directories are created under `state_root` and removed after
execution. The source chooses `cwd: inputs` or `cwd: scratch` (default).

| Setting | Default |
|---|---:|
| `enabled` | true |
| `queue_seconds` | 300 seconds |
| `wall_seconds` | 60 seconds (maximum 900) |
| `stdout_bytes`, `stderr_bytes` | 1 MiB each |
| `input_bytes` | 64 MiB |
| `input_storage_bytes`, `output_storage_bytes` | 2 GiB each |
| `per_run_storage_bytes` | 64 MiB |
| `runtime_bytes` | 1 GiB |

Source requests may narrow wall/output ceilings. There are no CPU, memory, process,
FD, scratch-space or network controls. Unsupported old isolation fields are not
part of `ScriptProgram`/`ScriptLimits`.

Environment construction does not inherit the daemon's environment. It supplies
fixed PATH/locale and HOME pointing to the temporary scratch directory. Explicit
values require both the source and configured `env_allowlist`; interpreter hooks
and credential names remain forbidden. This avoids accidental inheritance and is
not a secret-access boundary for an ordinary host process.

## E2 execution and settlement contract

The existing detached runner calls:

```python
await launch(settings, request, jobs_enabled=jobs_enabled, cancelled=event,
             lock_dir=job_contract["lock_dir"], capacity=job_contract["capacity"])
```

The historical module `src.jobs.script_sandbox` retains `SandboxRequest` and
`SandboxResult` names. No launcher transport remains. Supply the same box lock
configuration as managed jobs and call before generic runner admission to avoid
double-acquiring capacity. The backend uses weight one, normal priority and a
per-run lock. Queue timeout does not start a process. Cancellation of a queued
async task also waits for acquisition cleanup before releasing its run lock.

E2 must first check the original principal, scope, invocation, protected-output
readiness and durable launch identity. The backend retains an exclusive
`intent-<execution_id>` file to reject local replay. E2 owns restart recovery,
ambiguous launches, retention and deletion of those intents. `QuotaLedger`
reserves input and maximum output bytes before launch and retains reservations
until E2's durable retirement decision; timeout or failure does not erase them.

Both streams drain concurrently into separate bounded private buffers. Overflow
records `output_limit_exceeded` before signaling, and its prefix is incomplete.
Wall timeout and event cancellation likewise record their cause before cleanup.
The backend starts a new process group, sends TERM, allows two seconds, then KILLs
remaining members, reaps its direct child and waits up to two more seconds for
the group to stop before releasing capacity. On Linux, exited zombies count as
stopped; a live member past that deadline makes cleanup uncertain. It also cleans
up group members when the leader exits. This is ordinary process cleanup: a program can escape with
`setsid`; no double-fork containment guarantee remains. Pipe/cleanup uncertainty
returns `runtime_error` with `script_cleanup_uncertain`, not successful settlement.

Complete stdout requested as JSON must be exactly one UTF-8 JSON value. Trailing
data, fences, non-finite numbers and invalid encoding fail parsing. Nonzero exit
precedes JSON failure; enforced cancellation/timeout/overflow precede both.
`json_required` converts invalid or oversized JSON to `invalid_json`. Optional
JSON reports `invalid`/`too_large` without changing a successful process outcome.

`SandboxResult` contains raw private stdout/stderr, parsed JSON, byte counts,
capture/cleanup status, timestamps and quota key. `binding_value()` returns a
private dictionary capped at 262,144 bytes. It includes full streams only when the
whole value fits; otherwise both strings are null and `inline_omitted` is true.
Raw buffers remain available for encrypted storage. Parsed values reserve 4 KiB
for settlement metadata and protected references. Incomplete capture never binds
a stream prefix as a complete value. Public reprs omit payloads.

E2 must encrypt streams and bindings with A3, attach run-authorized output
references, enforce the final binding ceiling after metadata, and keep raw values
out of logs, receipts and public API responses. A binding dictionary is not a
public projection. E3 binds it only inside the authorized run.

## Acceptance

All process tests run locally with temporary storage and ordinary host Python and
shell executables. They cover proposal pinning/tampering, data-only arguments,
stdin closure, environment/descriptor inheritance, both enable switches, shared
admission, queue cancellation, timeout, child cleanup, stream overflow, strict JSON
and bounded bindings. No host config, namespace probe or skipped isolation arm is
needed. The obsolete isolation tests were replaced under the explicit scope decision;
their historical failures are not claimed as passes.

```bash
aq test tests/test_playbook_script_sandbox.py
aq test --aq-all-markers tests/test_playbook_script_sandbox.py
aq test tests/test_playbook_private_values.py tests/test_doctor.py tests/test_jobs_runner.py
```

## Durable jobs and protected output

The script job boundary derives its owner from the actual pinned `ScriptStep`
invocation. It records the run, step, full iteration path, attempt, artifact and
input digest in the existing jobs queue. Repeated submission rejoins that job;
a changed program or input under the same operation key conflicts. System and
profile runs need no project, task or workspace. Dedicated input pins and a
restricting run foreign key retain ownership through result retirement.

The detached runner receives its encrypted input's plaintext and encryption key
through a closed stdin pipe. Request/intent files contain identity and protected
references; completion files contain authenticated encrypted stdout, stderr and
binding rows. Job inspection returns metadata and references. Authorized
`job_logs` calls accept `stream=stdout|stderr` and return bounded base64 ranges;
A3 run authorization also applies to these reads. Script streams, bindings and
input pins share the configured job result retention period.

Recovery uses the existing job tick and runner nonce. A live runner is rejoined;
a completed receipt is authenticated and imported atomically with the terminal
job result. Missing, mismatched, unreadable or unclean completion evidence pauses
the owning run and retains the active job and pins. It never authorizes another
launch. Cancellation requests use the existing durable cancellation file. The
ordinary-process backend's cleanup limitations still apply as described above.

`script_v1` remains unsupported until the engine integration lands. This boundary
provides the `ScriptStep` and trusted script pin types, but does not install an
executor, add scripts to the accepted step union, or enable activation.
