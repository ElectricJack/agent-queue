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

## Roadmap

**Outstanding, not a phase:** three upstream issues found while building this
should be reported — `mcp_server.enabled` defaulting to `False` in the loader
against a documented `True`; `CLAUDE_CODE_OAUTH_TOKEN` being unreachable despite
its allowlist entry; and worker rungs being derived for harnesses whose CLI is
not installed, which routes tasks to a binary that does not exist and fails with
an empty `start-stderr.log`. Both are described in [`README.md`](README.md). Neither is fixed
here, because both belong upstream rather than in a deployment directory.

1. **Phase 2 — harden.** Automated SQL backups, data-disk snapshots, log
   shipping, `aq doctor` wired into a healthcheck.
2. **Phase 3 — container session provider.** `Dockerfile.worker`, the
   `TmuxProvider` subclass above, the volume/path design in §5.1, per-worker
   `--cpus`/`--memory`, and the `proctable` leak. Upstreamable as the fourth
   planned provider.
3. **Phase 4 — Terraform.** VM, disks, VPC, IAM, Secret Manager, managed SQL on
   a private IP. Structured so a non-GCP sibling is a new directory rather than a
   rewrite.
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
