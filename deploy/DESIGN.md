# Deployment design

Why the deployment is shaped the way it is, what constrains it, and what is
planned next. [`README.md`](README.md) is the task guide — how to actually run
it. This page is the argument behind that guide.

**Status:** Phase 1 built, running locally, and verified end to end — an agent
completes a task inside the container. Phases 2–4 are proposed, not
shipped. Anything under [Roadmap](#roadmap) and [Open questions](#open-questions)
describes intent, not current behaviour.

---

## 1. Tooling: thin Terraform, no Ansible

**No Ansible.** AQ already ships a configuration-management system, and it is
better than what we would write. `src/install/` is an idempotent step engine
with a dependency-ordered registry, consent gating on every mutating step, a
durable resume record, and **resource ownership tracking** — the thing that makes
`--repair`, `--upgrade` and `uninstall` safe (`src/install/lifecycle.py`). That
is Ansible's whole value proposition, already implemented and AQ-aware. A second
idempotency model fighting it over the same host buys nothing.

**Terraform, but only for cloud resources.** VM, disks, VPC, firewall, service
account, Secret Manager, managed SQL. That is roughly 150 lines and it earns its
place: reproducible infrastructure and a real `terraform destroy`. It is
deliberately deferred until the images are proven (see [Roadmap](#roadmap)).

**Containers for the application.** Which needs its own argument, below.

## 2. Why containers, and why the daemon image skips `aq install`

`aq install` exists to mutate a developer's *host*. Its supported-platform matrix
(`src/install/platform.py`) has exactly three installable host paths —
`windows-wsl2` (Ubuntu 24.04 only), `macos-apple-silicon`, `macos-intel` — and
`evaluate_support()` carries a dedicated branch rejecting plain Linux:

> "… is not a WSL2 distribution; the supported Linux path is Windows + WSL2."

`scripts/install.sh` refuses the same host even earlier, on a `/proc/version`
grep. **A stock Ubuntu cloud VM is therefore rejected twice before anything
runs.** In an image the Dockerfile *is* the install, so the matrix never applies.
That is a genuine strategic argument for containers, independent of any
packaging preference: it removes an entire class of work.

The alternative — adding a native-Linux host path to the matrix — is real but
modest work (`src/install/wsl.py` is a plain apt adapter; only its strings are
WSL-specific, and `platform_steps()` already dispatches per host). It is worth
doing if AQ should support bare-metal Linux installs generally. It is not on the
critical path for deploying, and it is wasted if the deployment is containerised
anyway.

### 2.1 Per-worker containers and resource limits

An early draft of this design argued *against* containers on the grounds that
they lose layer-3 resource enforcement. `docs/guides/resource-gating.md` says so
directly:

> "On WSL2 and in containers, delegation is frequently unavailable and the
> [system falls] back to layer 1."

That is true of **one container holding the daemon and every agent**, where
sessions share a single cgroup and cannot be limited individually. It is not true
of **one container per worker**, where the container *is* the cgroup: `--cpus`
and `--memory` are enforced by the same kernel mechanism systemd scopes were
reaching for, with no `Delegate=yes` on a user slice required. That is a cleaner
route to the same guarantee.

Phase 1 ships the first shape (agents as tmux sessions inside the daemon
container) because it needs no new session machinery. Phase 3 moves to the
second. The trade is explicit: until then, a runaway agent can exhaust the
daemon container.

## 3. The hard constraint: the scheduler is machine-scoped

**This is the most important limit in the system for deployment purposes.**

AQ has a real work-distribution protocol. Pull-based pools (`lifecycle: pool`)
run long-lived workers that loop on `aq task claim`, with `task_heartbeat`,
`task_close` and a **claim epoch** for fencing (`src/cli/claim_epoch.py`). Push
(`lifecycle: task`) launches one session per assigned task. The daemon converges
toward `min_active`/`max_active` every 5-second cascade tick.

But none of it spans machines:

- **No host or node column anywhere.** `sessions` carries `harness`, `work_dir`,
  `instance_token` — nothing identifying a machine. `instance_token` is a
  *session* fence ("a same-named successor is a different instance"), not a host
  identity.
- **Sessions are local tmux.** A second daemon on another host could neither see
  nor manage the first's.
- **The docs scope it to the box.** `docs/concepts/scheduling.md`: *"a fleet is
  all pool workers for one profile **across the daemon**"*; *"the **machine-wide**
  worker budget … lets **one daemon share a box** fairly across projects"*.

> **One daemon per database.** Two daemons against one database will both
> converge the same fleet-wide pool bounds and double-provision, while neither
> can see the other's sessions.

This applies locally too: do not point a containerised daemon and a natively
installed daemon at the same database.

**Scale up, not out.** Distribution happens across agents on one box; you size
the machine, not the instance count.

### 3.1 What horizontal scale would actually require

More than a host column on `sessions`. Workspaces are persistent, pooled,
**path-based** directories locked in the database, and worktree slots live at
`<base_repo>/.aq/worktrees/slot-<n>`. So multi-host needs host identity on
*workspaces* as well — or a shared filesystem, and `ensure_git_exclude` uses
`flock`, whose semantics over NFS are a known hazard. The cleaner design is
per-host base clones with host-tagged workspace rows, so placement picks a
workspace on a host that already has it.

A container session provider (§5) makes this materially easier, because
placement moves into the provider and `SessionHandle` is already
location-agnostic.

## 4. Repository cost: already solved, easy to regress

A natural worry is that workers re-clone repositories on every task. **AQ already
avoids this** — the deployment's job is to not break it.

`WorktreeSlotManager.reset_slot_for_task` documents the per-task path:

> "salvage-if-dirty → fetch → `reset --hard` + `clean -fd` (never `-x`, so
> gitignored caches survive) → fresh `aq/<task_id>` → sentinel"

Four properties follow:

1. **No clone per task.** A slot is reusable; a task is an incremental `fetch`
   plus a reset.
2. **N slots ≈ one repository on disk.** Worktrees share the base clone's object
   store.
3. **Gitignored caches survive between tasks** — `clean -fd`, deliberately never
   `-fdx`. `node_modules/`, `.venv/`, build output persist. Often a larger saving
   than the clone.
4. **`worktree_setup` runs only on freshly created slots**, not per task.

**The rule this imposes:** the base repository lives on a *persistent volume*,
and the worker is the only ephemeral part. A design where each worker container
clones on startup discards all four properties and would make containers slower
than a VM. Cold-starting a new host is a disk snapshot that already contains the
clone and warm caches, not a fresh clone.

## 5. Planned: the container session provider

`src/sessions/provider.py` defines a `SessionProvider` ABC whose docstring names
three *planned* providers (`tmux`, `subprocess`, `fake`). A container provider is
a fourth, not a fork. The contract is explicitly built for this:

> "Callers branch on `Cap`, **never** on `provider.name`. A caller that wants to
> peek asks `Cap.PEEK in provider.capabilities` and degrades gracefully."

`SessionHandle` is `(name, provider, instance_token)` — no PID, no socket, no
host. **Location-agnostic by construction.** Registration is one entry in
`default_session_registry()`.

**Implementation is a subclass, not a rewrite.** All 51 tmux invocations in
`src/sessions/tmux.py` funnel through a single method, `_tmux()`. Put tmux inside
the worker image and override that one method to route through `docker exec`, and
every pane semantic is inherited — `peek` → `capture-pane`, `nudge` →
`send-keys`, dialog handling, composer checks. It also preserves three features
typed against `TmuxProvider` that would otherwise break:
`src/sessions/terminal_pty.py`, `src/sessions/pane_broadcaster.py`, and
`src/doctor/session_checks.py`.

Overrides beyond `_tmux()`: `start()` (run the container first), `stop()` (remove
it), `list_running()` (`docker ps` — and it must raise `PartialListError` on
failure, never return a short list), and `process_alive()` (currently `/proc`
via `proctable`, which cannot see into another PID namespace).

### 5.1 The real cost is filesystem, not control

The worker↔daemon channel is already network-based: `src/sessions/env.py` defines
nine `AQ_*` identity variables, and the `aq` CLI inside a session reaches its
daemon over `AQ_API_URL`/`AQ_API_TOKEN`, an injectable parameter rather than a
hardcoded loopback address. What needs design is path identity:

1. **Workspaces.** Worktree slots are subdirectories of the base clone and are
   not self-contained — they reference the parent repository's `.git`. Mount the
   base repo (which brings every slot) at the *same path* inside the worker.
2. **Transcripts.** The daemon reads agent output straight from disk —
   `~/.claude/projects/<slug>/*.jsonl`, where `<slug>` derives from `work_dir`.
   `base_dir` is injectable (`src/sessions/transcripts/base.py`), so a shared
   volume for agent homes plus a per-session `base_dir` closes it.

Both are solvable; both fail *silently* when wrong (transcripts simply come back
empty), which makes them the most likely source of trouble.

Also outstanding: `src/orchestrator/worktree_manager.py` imports `proctable`
directly for a host-local `/proc` scan — the one place the host assumption leaks
outside a provider.

## 6. Spot and other ephemeral hosts

AQ's recovery model is built for abrupt death, which makes preemptible hosts a
better fit than they first appear. `Orchestrator` documents the contract:

> "After a restart, no adapter processes are actually running, so any tasks
> marked IN_PROGRESS or agents marked BUSY are stale artifacts from the previous
> run."
>
> 1. Reset BUSY agents → IDLE
> 2. **Release all workspace locks** — "no agents hold them after restart"
> 3. Reset IN_PROGRESS tasks → **READY, not BLOCKED** — "a fresh retry is
>    appropriate"
>
> "Agent work is designed to be **idempotent** (the agent sees the workspace as
> the previous agent left it, including any partial commits)."

Supporting pieces: SIGTERM triggers a graceful shutdown that waits at most 10s
for in-flight tasks; the session reconciler's orphan step releases an open task
whose session row is not live, under the rule that **unknown is not dead**; and
`salvage_dirty` archives a crashed predecessor's uncommitted changes as a patch
on a `task_context` row — in PostgreSQL, so it outlives the machine — rather
than `git stash`, whose stack is shared by every worktree of a repository.

### 6.1 The notice window is not a drain window

GCP Spot gives roughly 30 seconds (AWS ~2 minutes, Azure ~30s), best-effort.
**That is nowhere near enough for an agent to finish a task**, and never will
be — runs take minutes. Do not design for draining in-flight work. Design for
being killed, which is what the contract above already does. The cost of a
preemption is the partial work of whatever was mid-task, mitigated by
`salvage_dirty` and by the workspace retaining prior commits.

### 6.2 What must survive: the disk

Every property that keeps repository cost flat (§4) lives on disk, and so do the
slot rows that reference those paths — restart recovery deliberately preserves
slots as durable inventory. Therefore:

- **Use Persistent Disk or Hyperdisk, never Local SSD.** Local SSD is physically
  attached and is wiped on preemption, which would destroy every worktree slot
  and every warm cache while leaving the database rows pointing at nothing.
- Keep auto-delete off so the disk outlives the instance; a replacement attaches
  the same disk and resumes warm.
- Cold-start a genuinely new host from a **snapshot** that already contains the
  base clone and caches, rather than cloning from scratch.
- A PD attaches read-write to exactly **one** VM at a time. That is a useful
  accident: the disk itself enforces the one-daemon-per-database rule (§3).

### 6.3 Two failure modes to design around

**Nothing restarts the daemon.** Those workspace locks are released *by the
daemon restarting*. A preempted VM that nothing replaces leaves them held, and
the queue stalls behind them.

**Two daemons overlap.** A replacement coming up while the old host is merely
stopped rather than dead gives two daemons converging the same fleet-wide pool
bounds. The single-writer disk attachment guards this if both use it.

### 6.4 Getting work, and knowing when it is safe to stop

**There is no worker registration protocol.** Identity flows top-down: the
daemon writes the `sessions` row, mints the nine `AQ_*` variables
(`src/sessions/env.py`), and spawns the process already carrying them. There is
no `/register` endpoint — only `/api/task/{claim,heartbeat,close}`, which is how
an already-spawned pool worker asks for *work*, not for membership. Adoption on
restart scans `/proc` for `AQ_SESSION_ID`, but that is the daemon re-finding
sessions it started.

**Consequence: a fleet of spot boxes cannot share one queue today.** Each would
need its own daemon and its own database, which is not a fleet. One spot host
running the whole stack works; several do not. Several become possible only with
the container session provider (§5) plus host-aware placement (§3.1).

**Draining before a planned stop** is well supported, and is the right procedure
before suspending or resizing a host:

```bash
aq system orchestrator-control --action pause   # stop assigning new tasks
# wait for in-flight work to finish:
curl -s localhost:8081/health | jq '.checks | {tasks, agents}'
#   safe when tasks.in_progress == 0 and agents.busy == 0
aq system orchestrator-control --action resume  # or stop the host
```

`aq --json status` gives the same signal as `tasks.in_progress` and
`tasks.ready_to_work`. Pausing only stops *new* assignment; it does not
interrupt running agents, which is exactly what a clean spin-down wants.

### 6.5 Sizing: it is not mostly idle

A reasonable-sounding assumption is that agents are LLM-bound and therefore need
little CPU. The agent's own loop is indeed mostly waiting on the network — but
**agents spawn builds and test suites**, and that is where the load is. AQ's
resource gating exists precisely because of it, naming "a shell script that
hardcodes `-n 24`, a compiler that spawns per-core" as the runaway it defends
against.

So size for the *projects*, not the agent count: RAM and disk dominate
steady-state, CPU spikes hard and briefly during builds. A box that looks idle
on average can still be saturated exactly when it matters.

## 7. Kubernetes

**Not yet, and not for the daemon.** K8s exists to schedule many pods across
many nodes, and AQ cannot use that today:

- The scheduler is machine-scoped (§3) — you would run `replicas: 1`. A
  Deployment of one is not a reason to adopt Kubernetes.
- There is no worker registration (§6.4), so worker pods have nothing to join.
- Worktree slots, warm caches and the vault make this a pet, not cattle.
- The daemon types into tmux panes and streams them to the dashboard, which is
  about as far from cloud-native as a workload gets.

You would get a single stateful pod with a ReadWriteOnce PVC: all of the
operational cost, and almost none of the benefit. The one genuine gain — a PVC
enforcing a single daemon — a Persistent Disk already provides (§6.2).

**When it becomes the right answer:** once the container session provider (§5)
exists and is pointed at the Kubernetes API rather than a local Docker socket.
Then each *agent* is a pod and K8s does real work — placement across nodes,
per-pod limits, preemption handling. That path also needs the host-awareness of
§3.1, because workspaces are path-based and locked in the database.

Build the provider first. It is what makes Kubernetes worth having, and it is
useful on a single box regardless.

## 8. The committed target

**Shape C on one ordinary VM.** Not spot, not per-worker containers, not
Kubernetes — those all come after, and none of them is wasted by starting here.

```
1. NOW     Shape C on one regular VM + Persistent Disk + Cloud SQL
2. THEN    flip that same VM to spot (a machine-type change)
3. THEN    Phase 3 container workers — hard CPU/memory limits
4. MAYBE   multi-node — needs host-tagged workspaces first (§3.1)
```

**Why an ordinary VM before spot.** Shape C is already proven: an agent
completes a real task in the container. So the unknowns in a first deploy are
*infrastructure* — VPC, Cloud SQL connectivity, IAM, the tunnel, the disk — not
the application. Debugging those and preemption simultaneously is a bad trade.
Switching later costs one `provisioning_model` flag: same disk, same image, same
DSN. The Terraform carries that flag from the start, defaulted off.

**Why not Phase 3 first.** It buys isolation, and a runaway test suite killing
the box is roughly equivalent to a preemption — which §6 shows the system
already recovers from. Worth doing when the workload is build-heavy; not a
blocker for a first deploy.

**What step 2 additionally needs**, and why it is not free: something must
restart the daemon after preemption, because workspace locks are released *by
the daemon restarting* (§6.3). A managed instance group of size one is the
obvious answer, and it also prevents the two-daemon overlap — but it wants the
baseline to be boring first.

## Roadmap

**Outstanding, not a phase:** three upstream issues found while building this
should be reported — `mcp_server.enabled` defaulting to `False` in the loader
against a documented `True`; `CLAUDE_CODE_OAUTH_TOKEN` being unreachable despite
its allowlist entry; and worker rungs being derived for harnesses whose CLI is
not installed, which routes tasks to a binary that does not exist and fails with
an empty `start-stderr.log`. All three are described in [`README.md`](README.md);
none is fixed here, because they belong upstream rather than in a deployment
directory.

1. **Phase 2 — harden.** Automated SQL backups, data-disk snapshots, log
   shipping, `aq doctor` wired into a healthcheck.
2. **Phase 3 — container session provider.** `Dockerfile.worker`, the
   `TmuxProvider` subclass above, the volume/path design in §5.1, per-worker
   `--cpus`/`--memory`, and the `proctable` leak. Upstreamable as the fourth
   planned provider.
3. ~~**Phase 4 — Terraform.**~~ Promoted to step 1 of §8 and in progress:
   `deploy/terraform/gcp/`. VM, disks, VPC, NAT, IAM, Secret Manager and Cloud
   SQL on a private IP, structured so a non-GCP sibling is a new directory
   rather than a rewrite.
4. **Phase 5 — agent-runnable install** (only if bare-metal Linux support is
   pursued). `aq install --non-interactive --yes --json` already emits one
   machine-readable object with meaningful exit codes (0 ready, 10 needs_user,
   12 unsupported_host, 20 failed) — a clean contract for an agent to drive.

## Open questions

- ~~**Does an agent run to completion inside a container?**~~ **Answered: yes.**
  A smoke task dispatched, acquired its worktree slot, ran Claude Code in a tmux
  session inside the container, edited the file and committed to `aq/<task>`,
  and closed as `COMPLETED`. Shape C is therefore viable as shipped. Still
  unexercised: long runs, several concurrent agents, and anything beyond a
  trivial single-file edit. See
  [Verifying the stack](README.md#verifying-the-stack).
- **Memory / `aq-memory`.** Off by default. Milvus needs no server (it defaults
  to embedded Milvus Lite), but embeddings default to a local Ollama, and
  `packages/memsearch` is not installed in the daemon image — its importers
  degrade with a warning rather than failing, so enabling memory silently
  no-ops until that is added.
- **Sizing.** Starting point is ~4 concurrent agents on 4 vCPU / 16 GB. No
  measurements taken; test suites, not agents, are the memory spikes.
- **Docker socket access** for Phase 3 — socket mount (root-equivalent blast
  radius) or a remote/rootless API.
- **Horizontal scale** — whether §3.1 is ever worth doing, or one large box
  remains sufficient.
- **Native Linux host path** — whether to contribute it upstream for bare-metal
  installs, independent of this deployment.

## See also

- [`README.md`](README.md) — running it, configuration, security posture, and the
  known-gotchas list (including two upstream bugs found while building this).
- [`docs/concepts/scheduling.md`](../docs/concepts/scheduling.md) — the capacity
  model §3 depends on.
- [`docs/guides/worker-pools.md`](../docs/guides/worker-pools.md) — push vs pull
  lifecycles.
- [`docs/guides/resource-gating.md`](../docs/guides/resource-gating.md) — the
  three enforcement layers §2.1 argues about.
