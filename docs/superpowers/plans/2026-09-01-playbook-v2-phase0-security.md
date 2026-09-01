# Playbook V2 — Package 0 child plan: Security and compiler authority baseline

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to run this plan task by task. Every task below is a red/green/refactor unit with a named failing assertion, a named implementation, and its own verification command. Do not reorder tasks across commit boundaries.

**Parent roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` § "Package 0 — Security and compiler authority baseline"
**Design spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md` § "Phase 0: security and authority baseline"
**Branch:** `feature/playbook-v2-pkg0` (from `origin/main`)
**Consumes:** approved design spec + current V1 behavior. Nothing else.
**Produces:** trusted compiler boundary, normalized three-namespace capability model, server-derived `ExecutionPrincipal`, dispatch authorization, and recursive no-widening delegation tests.

---

## 1. Why this package exists (what is actually broken today)

Five gaps were confirmed by reading the live tree, not inferred from the roadmap. The first four are Package 0's mandate; the fifth (§1.5) is a pre-existing defect this work surfaces and deliberately does not repair.

### 1.1 The authoritative frontmatter merge is orphaned

`PlaybookCompiler._merge_frontmatter` (`src/playbooks/compiler.py:337`) is the function that makes source-file frontmatter win over compiler output. It has **no production caller**. The only reference in the repository outside its own module is `tests/test_playbook_services.py:56`.

The live install path is:

```
playbook-compiler agent  →  aq playbook_install(playbook_id, compiled_path)
                         →  src/playbooks/validator_command.py:184  _cmd_playbook_install
                         →  _cmd_playbook_validate (JSON shape + CompiledPlaybook.validate only)
                         →  src/playbooks/manager.py:1564  install_compiled  → active, immediately
```

`_cmd_playbook_install` never reads the source `.md`. `CompiledPlaybook.from_dict` accepts `id`, `scope`, `triggers`, `enabled`, `profile_id`, `version`, `source_hash`, `cooldown_seconds`, `max_tokens`, `llm_config` straight out of the agent-authored JSON. `install_compiled` then indexes triggers and persists, with no review gate.

The `playbook-compiler` profile (`src/profiles/defaults/playbook-compiler/profile.md`) holds `Bash`, `Write`, `Edit`, `WebFetch`, `playbook_validate` and `playbook_install`. It reads arbitrary Markdown, which is untrusted input. A prompt injection in a playbook source therefore has a complete path to: install a **system**-scoped playbook, triggered on `task.completed`, `enabled: true`, running under `profile_id: supervisor`.

### 1.2 There is no capability check at dispatch

`CommandHandler.execute` (`src/commands/handler.py:651`) pops `_scope`, runs the subsystem pause gate, then dispatches to `getattr(self, f"_cmd_{name}")` or falls back to `orchestrator.plugin_registry.get_command(name)` (`src/plugins/registry.py:989`). Neither branch consults the caller's profile.

The only gate in front of an agent is `check_request_scope` (`src/api/scope.py:216`), which is *identical for every non-elevated session*: the 23-name `AGENT_COMMAND_SET` (`src/api/scope.py:14`), plus two narrowly verified assignment carve-outs (triage, playbook-compiler). A profile's declared tools have no effect on what it may dispatch.

### 1.3 Profile `allowed_tools` is not enforced for AQ commands anywhere

`SessionSpecBuilder._resolve_allowed_tools` (`src/sessions/spec.py:598`) is the *only* consumer of `profile.allowed_tools` on the session launch path. It:

- returns `[]` (emit no flag → CLI defaults, i.e. everything) when the list is empty;
- returns `[]` when the list contains `"*"`;
- keeps only the 12 names in `_HARNESS_TOOL_NAMES` (`src/sessions/spec.py:591`);
- **drops every AQ command name** with a `logger.debug`, because they are not names the CLI understands.

So `"allowed": [..., "task_close", "reopen_with_feedback"]` in the `reviewer` profile is documentation, not enforcement. Today "empty means everything" and "`*` means everything" are both live behaviors that the design spec forbids.

### 1.4 The recursive delegation check does not fire for real agents

`src/commands/task_commands.py:1176-1186` documents its own gap in-tree:

> **v1 gap — recursive task→child-task escalation:** when `create_task` is invoked by a task agent via the embedded `agent-queue` MCP server (HTTP), there's no per-task identity on the request, so `self._caller_profile_id` will be unset and the escalation check won't fire.

`_check_capability_escalation` (`src/commands/task_commands.py:74`) is therefore reachable only from the in-process playbook runner. Every real tmux session that calls `create_task` over HTTP bypasses it. The stated line of defense — the harness `--allowedTools` flag — does not apply, because per §1.3 AQ command names are dropped from that flag.

### 1.5 A shipped profile already names commands its token cannot reach

`src/profiles/defaults/reviewer/profile.md` lists `reopen_with_feedback` in `## Tools`, and its `## Role` prose makes that command the entire rejection path. `reopen_with_feedback` is **not** in `AGENT_COMMAND_SET` (`src/api/scope.py:14`, 23 names) and **not** in the triage carve-out `_TRIAGE_COMMANDS` (`src/api/scope.py:131`). A reviewer session is minted non-elevated (`src/orchestrator/execution.py:1800`), so `check_command_scope` answers `out of scope: reopen_with_feedback` before the command is ever dispatched. The same holds for `get_task`, which reviewers also list.

This is a pre-existing gap, not one Package 0 introduces, and Package 0 must **not** fix it by adding names to `AGENT_COMMAND_SET` — that would widen a server-owned allowlist, which is precisely what this package exists to prevent. T-10 surfaces it as a report rather than a failure, and §14 files it.

**Package 0 closes the first four; the fifth is recorded, not repaired.**

---

## 2. Live-tree reconciliation — deviations from the roadmap's file list

The roadmap (§3, §5) allows a child plan to refine filenames after inspecting the live tree, but requires the deviation be documented. Every row below was verified against `origin/main` at `72af6069`.

| Roadmap says | Live tree | Decision |
|---|---|---|
| Modify `src/profiles/models.py` | **Does not exist.** `AgentProfile` is a dataclass at `src/models.py:846`; the YAML-config twin is `AgentProfileConfig` at `src/config.py:670`; the DB row is `agent_profiles` at `src/database/tables.py:583` | Modify all three: `src/models.py`, `src/config.py`, `src/database/tables.py` |
| Create `tests/commands/test_execution_principal.py` | **`tests/commands/` does not exist.** All 51+ suites are flat `tests/test_*.py` | `tests/test_execution_principal.py` |
| Create `tests/commands/test_command_capability_authorization.py` | as above | `tests/test_command_capability_authorization.py` |
| Create `tests/playbooks/test_playbook_compiler_authority.py` | **`tests/playbooks/` does not exist** | `tests/test_playbook_compiler_authority.py` |
| Verify `pytest tests/api/test_execute.py` | **`tests/api/` does not exist.** The nearest suites are `tests/test_api_execute_contract.py`, `tests/test_api_scope.py`, `tests/test_api_auth.py`, `tests/test_triage_api_scope.py`, `tests/test_supervisor_global_token_loopback.py` | Substitute those five |
| Verify `pytest tests/commands/test_session_commands.py` | `tests/test_session_commands.py` | Substitute |
| Modify `src/sessions/tmux.py` | tmux.py only spawns the argv the spec builder produced; the allowlist flag is assembled in `src/sessions/spec.py:528` from `_resolve_allowed_tools` | **No change to `src/sessions/tmux.py`.** All session-launch work lands in `src/sessions/spec.py` |
| Modify `src/api/dependencies.py` | Module-level injection state only; nothing identity-shaped passes through it | **No change required.** Recorded so a reviewer does not look for one |
| Modify `src/playbooks/compiler.py` (as the install path) | The live install path is `src/playbooks/validator_command.py:184` + `src/playbooks/manager.py:1564`. `compiler.py` holds the orphaned merge helper | Modify `compiler.py` (promote the helper), **plus** `validator_command.py` and `manager.py`, which the roadmap omits |
| Modify `src/playbooks/pipeline_compiler.py` | `compile_pipeline` is a pure `markdown -> CompilationResult` function with no path argument, so it cannot derive scope from the vault path | Minimal change only (§5.2 T-11); the authority merge lands at the manager/install layer where `rel_path` exists |
| (not listed) | `src/api/codegen.py:381` is a **second** HTTP dispatch surface: the generated typed per-command routes call `check_request_scope` and then `ch.execute` exactly like `/api/execute` | Add `src/api/codegen.py` to the modify list |
| (not listed) | `_check_capability_escalation` lives in `src/commands/task_commands.py:74`, not `handler.py` | Add `src/commands/task_commands.py` |
| (not listed) | Tool-schema publication happens in `src/mcp_registration.py`, `src/api/execute.py:/api/tools`, `src/tools/registry.py`, and `src/playbooks/services.py:27` (`node_tools`) | Add all four — required by the "discovery and dispatch agree" exit gate |
| (no storage change implied) | Namespaced capabilities need somewhere to live | **One additive Alembic revision** (§6). Additive nullable columns only; reverting Package 0's code needs no downgrade, which preserves the roadmap's rollback boundary |

Two further naming reconciliations:

- **Namespace names.** The design spec §"Capability model" says `harness_tools` / `commands` / `mcp_tools`. Roadmap §4 locks `harness_tools` / `aq_commands` / `plugin_tools`. **Roadmap §4 wins** — it is the locked cross-package interface. `plugin_tools` covers both AQ plugin commands (`read_file`, `write_note`, `vibecop_scan`, … registered unprefixed into the same flat dispatch namespace at `src/plugins/internal/files.py:562` etc.) and fully-qualified third-party MCP tools (`mcp__github__create_issue`). §4.1 defines the classifier that keeps the two apart.
- **Commit sequence.** The roadmap's four commits give compiler authority a test commit but no implementation commit. This plan uses **five** commits (§7), splitting the compiler-authority implementation out so it can be reverted independently of the principal change.

---

## 3. Design decisions

### 3.1 `CapabilityPolicy` — `src/profiles/capabilities.py`

```python
WILDCARD_CHARS: Final = "*?"

@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    harness_tools: frozenset[str] = frozenset()
    aq_commands: frozenset[str] = frozenset()
    plugin_tools: frozenset[str] = frozenset()

    #: True when this policy was reconstructed by the legacy adapter from
    #: ``allowed_tools`` rather than authored as explicit namespaces.  Drives
    #: the audit/enforce split in §3.6.  Not part of the fingerprint.
    derived_from_legacy: bool = False
```

Behavior, all pure and synchronous:

| Member | Contract |
|---|---|
| `DENY_ALL` | module constant, all three namespaces empty, `derived_from_legacy=False` |
| `allows(namespace, name)` | exact membership. No prefix, glob, or case folding |
| `allows_harness_tool` / `allows_aq_command` / `allows_plugin_tool` | thin wrappers over `allows` |
| `intersect(other)` | per-namespace set intersection; result `derived_from_legacy = self.derived_from_legacy or other.derived_from_legacy` |
| `is_subset_of(other)` | `all(getattr(self, ns) <= getattr(other, ns) for ns in NAMESPACES)` |
| `to_canonical()` | `{"harness_tools": sorted(...), "aq_commands": sorted(...), "plugin_tools": sorted(...)}` |
| `fingerprint()` | `"sha256:" + sha256(json.dumps(to_canonical(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()` |
| `from_namespaces(...)` | validating constructor; raises `CapabilityPolicyError` on a wildcard or empty-string entry |

**Wildcards.** `from_namespaces` raises when any entry contains a character in `WILDCARD_CHARS`, when an entry is empty or whitespace-only, or when an entry is not a `str`. This rejects `"*"`, `"**"`, and `"mcp__github__*"` alike. There is no "grant everything" value in any namespace.

**Empty means none.** `CapabilityPolicy()` denies everything. `allows()` on an empty namespace is `False`. Nothing in the class treats "empty" as "unset".

**Namespace classifier** (module function, used by the legacy adapter and by dispatch):

```python
def classify_capability(name: str, *, plugin_command_names: frozenset[str]) -> Namespace:
    if name in HARNESS_TOOL_NAMES:            # single source of truth, moved here
        return "harness_tools"
    if name.startswith("mcp__"):
        return "plugin_tools"
    if name in plugin_command_names:
        return "plugin_tools"
    return "aq_commands"
```

`HARNESS_TOOL_NAMES` moves out of `SessionSpecBuilder._HARNESS_TOOL_NAMES` (`src/sessions/spec.py:591`) into this module; `spec.py` imports it so there is exactly one definition. `plugin_command_names` is supplied by the caller from `orchestrator.plugin_registry`; when no registry is wired it defaults to `frozenset()`, which classifies plugin commands as `aq_commands` — the *stricter* reading, since `aq_commands` is checked against a registry-derived built-in set and an unknown plugin name will simply be denied rather than silently allowed.

### 3.2 Authoring surface — the `## Capabilities` block

Profile Markdown gains an optional third JSON section beside `## Config`, `## Tools`, `## MCP Servers`:

````markdown
## Capabilities

```json
{
  "harness_tools": ["Bash", "Read", "Glob", "Grep"],
  "aq_commands": ["get_task", "task_close", "reopen_with_feedback"],
  "plugin_tools": []
}
```
````

Rules enforced in `src/profiles/parser.py`:

1. All three keys are required when the block is present. A missing key is an **error**, not an implicit empty — "you forgot" and "you meant none" must not look alike.
2. Any other key is an error.
3. Each value must be a list of non-empty strings with no wildcard character.
4. `## Capabilities` and `## Tools` in the same file is an error naming both sections; the operator migrates deliberately.
5. `harness_tools == [] and aq_commands != []` is an error: a session with no harness tools has no `Bash`, and `Bash` is how a tmux session reaches the `aq` CLI, so the declared AQ commands would be unreachable. Diagnostic: `Capabilities: aq_commands are unreachable because harness_tools is empty — a session needs Bash to run the aq CLI`.

The same wildcard rejection is applied to the **legacy** `## Tools` `allowed` list, so `"*"` fails at parse time whichever shape the file uses.

### 3.3 Legacy adapter — reading `allowed_tools` without granting new rights

When a profile has no `## Capabilities` block, `capability_policy_for(profile)` builds one from `profile.allowed_tools`:

```
harness_tools = {t for t in allowed_tools if classify(t) == "harness_tools"}
plugin_tools  = {t for t in allowed_tools if classify(t) == "plugin_tools"}
aq_commands   = {t for t in allowed_tools if classify(t) == "aq_commands"}
derived_from_legacy = True
```

with two compatibility rules that exist to avoid *removing* rights, never to add any:

- **R1 — empty `allowed_tools`.** Today an empty list means "emit no `--allowedTools` flag", i.e. the CLI's own defaults. The adapter yields `harness_tools = HARNESS_TOOL_NAMES` (the 12 names the launcher recognises), which is the same effective grant. It does **not** yield "everything the CLI can do", because the launcher could never express that anyway.
- **R2 — no AQ names declared.** A legacy profile that lists only harness tools has, today, exactly `AGENT_COMMAND_SET` at the API boundary. The adapter yields `aq_commands = AGENT_COMMAND_SET` for that case, which is identical to today.

Both rules are strictly no-new-rights: R1 grants only names the launcher already emits, R2 grants only names `check_request_scope` already admits. Both set `derived_from_legacy=True`, which is what routes them through the audit path in §3.6 instead of hard denial.

The *effective* grant is always the intersection of the new gate and the existing one — `check_request_scope` is untouched by this package, so a legacy profile can never come out of Package 0 with more reach than it had going in.

### 3.4 `ExecutionPrincipal` — `src/commands/principal.py`

```python
class PrincipalKind(StrEnum):
    LOCAL = "local"        # loopback CLI, no bearer token — trusted operator
    SERVICE = "service"    # daemon-internal: cascade, reconciler, timers, orchestrator
    SESSION = "session"    # an agent session bearer token
    PLAYBOOK = "playbook"  # a playbook run step (carries run/step identity)

@dataclass(frozen=True, slots=True)
class ExecutionPrincipal:
    kind: PrincipalKind
    policy: CapabilityPolicy
    session_id: str | None = None
    service_name: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    profile_id: str | None = None
    elevated: bool = False
    parent_run_id: str | None = None
    parent_step_id: str | None = None
    provenance: tuple[str, ...] = ()
```

- `TRUSTED_LOCAL` and `service(name)` are the two **explicit trusted principals** required by the last checkbox of the roadmap's Required outcomes. Both have `enforced == False`. There is no implicit bypass anywhere else: `enforced` is `kind in {SESSION, PLAYBOOK}`, computed as a property, never passed in.
- `narrow(policy, *, reason)` returns a new principal whose policy is `self.policy.intersect(policy)` and whose `provenance` is `self.provenance + (reason,)`. There is **no** widening method. `intersect` is the only policy transform on the type.
- Construction is `frozen=True`; no field is settable after the fact.

Request-local binding uses a `ContextVar`, matching the existing `_current_scope_var` discipline in `src/commands/handler.py:105` (save token, restore in `finally`, never unconditional clear — re-entrant `execute` calls are normal in this codebase):

```python
_principal_var: ContextVar[ExecutionPrincipal | None] = ContextVar("_principal_var", default=None)

def current_principal() -> ExecutionPrincipal | None: ...

@contextmanager
def principal_context(p: ExecutionPrincipal): ...   # sets, yields, resets the token
```

**Server-derived, never client-supplied.** `ExecutionPrincipal` is never parsed from a request body, and no `_principal`, `_policy`, or `_profile_id` key is ever read from `args` (§3.5 strips them).

### 3.5 The single principal-construction seam

`CommandHandler.execute` (`src/commands/handler.py:651`) becomes the one place a principal is constructed for a request. Order inside `execute`, immediately after the existing `_scope` pop:

1. `args.pop("_scope")` — unchanged.
2. **New:** `for key in ("_principal", "_policy", "_profile_id", "_capabilities"): args.pop(key, None)` — belt-and-braces, mirroring the existing `_scope` comment. `/api/execute` and `src/api/codegen.py` also strip them before forwarding (§3.7), so this is the second of two layers.
3. **New:** if `current_principal()` is already set (the playbook runner and orchestrator bind one explicitly), use it. Otherwise call `await self._principal_from_scope(scope)`.
4. Bind with `principal_context(...)` for the body; restore in the same `finally` that resets `_scope_token`.

`_principal_from_scope` is the whole seam, and it is what Package 0 reverts by deleting:

```
scope is None or scope["kind"] == "local"   ->  TRUSTED_LOCAL
scope["kind"] == "session"                  ->  session row -> profile_id -> policy
```

The session row lookup is `await self.db.get_session(scope["session_id"])`, and the policy is `capability_policy_for(await self.db.get_profile(session.profile_id))`. Failure is closed:

| Condition | Result |
|---|---|
| `session_id` present but no session row | `ExecutionPrincipal(kind=SESSION, policy=DENY_ALL, provenance=("session-not-found",))` |
| session row has no `profile_id` | `policy=DENY_ALL`, provenance `("session-has-no-profile",)` |
| `profile_id` set but no profile row | `policy=DENY_ALL`, provenance `("profile-not-found",)` |
| `scope["elevated"]` is true | policy from the profile as normal, `elevated=True`; §4.2 explains why elevation is *not* a policy bypass |

**Why derive rather than persist.** The roadmap's Required outcome reads "Extend session tokens and request scope with server-derived task, profile, and policy identity." The literal reading adds `profile_id` to `api_session_tokens` (`src/database/tables.py:758`). This plan instead derives it from the `sessions` row keyed by `scope.session_id`, and surfaces it on `RequestScope` as two new **server-only** fields (§3.7). Rationale:

- The roadmap's own rollback boundary says reverting Package 0 must need "no database downgrade". A derived field needs none.
- A token minted at launch would pin a `profile_id` that goes stale the moment an operator edits or re-syncs the profile, and stale-wide is the dangerous direction. The `sessions` row is the live truth the reconciler already maintains.
- There are five mint sites (`src/orchestrator/execution.py:1800`, `src/orchestrator/pools.py:368`, `src/messages/session_lens.py:356`, `src/agents/terminals.py:195`, `src/commands/session_commands.py:505`). Deriving means one resolution path instead of five call-site contracts to keep correct.

Cost: one extra `get_session` + `get_profile` per command. Mitigated by a 30-second TTL cache keyed on `session_id` inside the seam, invalidated by `sync_profile_to_db` (§5.2 T-9) so a profile edit takes effect within one sync rather than one TTL.

### 3.6 Feature flag — `security.capability_enforcement`

New field on the existing `SecurityConfig` (`src/config.py:1074`):

```python
capability_enforcement: str = "audit"   # "off" | "audit" | "enforce"
```

`validate()` rejects any other value. Semantics at dispatch:

| Mode | Policy authored as `## Capabilities` | Policy from the legacy adapter (`derived_from_legacy`) |
|---|---|---|
| `off` | allow, no log | allow, no log |
| `audit` *(default in Package 0)* | **deny** | allow + `logger.warning("capability_denied_shadow ...")` + `capability.denied` counter |
| `enforce` | **deny** | **deny** |

The split is deliberate: explicitly authored capability sets are always enforced, because an operator who wrote the block asked for it. Only the *adapted* legacy shape gets a grace mode, because flipping every un-migrated profile to deny-by-default in one commit would strand a running fleet.

Everything else in this package is **unflagged and always on**: the compiler authority merge (§3.8), the client-field stripping (§3.5 step 2), principal construction, wildcard rejection at parse and sync, and delegation narrowing (§3.9). The flag governs exactly one decision — whether a *legacy-derived* denial is fatal.

**Ownership and removal.** Owner: Package 0. `security.capability_enforcement` is flipped to `enforce` as part of **Package 6**'s fleet-readiness gate (every shipped and project profile audited and migrated to `## Capabilities`, per this package's own Rollback boundary: "Do not begin Package 1 until production-equivalent profiles have been audited for explicit capabilities"). The flag, the `off`/`audit` modes, and the `derived_from_legacy` field are **removed in Package 7**, along with the legacy adapter and the `## Tools` block. Package 7's child plan must list them.

**Exit-gate note.** The Package 0 exit gate is proven with `capability_enforcement="enforce"`; the exit-gate suites set it explicitly via the config fixture, so the shipped default never weakens the evidence.

### 3.7 API boundary

`RequestScope` (`src/api/auth.py:29`) gains two fields:

```python
profile_id: str | None = None        # server-derived; never minted, never client-supplied
policy_fingerprint: str | None = None
```

Populated by `TokenAuthMiddleware` (`src/api/middleware.py`) **after** `store.validate()` returns, by the same resolver the handler seam uses. `SessionTokenStore.mint` and `validate` are unchanged, and neither field is persisted — this is why there is no `api_session_tokens` migration.

Both HTTP dispatch surfaces strip client-supplied identity before forwarding:

- `src/api/execute.py:57` already does `args.pop("_scope", None)`. Extend to `for k in _SERVER_OWNED_ARG_KEYS: args.pop(k, None)` where `_SERVER_OWNED_ARG_KEYS = ("_scope", "_principal", "_policy", "_profile_id", "_capabilities")`.
- `src/api/codegen.py:381` does the same `args.pop("_scope", None)`. Same extension. This surface is not mentioned in the roadmap and is the one a reader is most likely to miss.

`check_request_scope` (`src/api/scope.py:216`) is **unchanged**. The two gates compose: a command must pass the scope gate *and* the capability gate. Package 0 does not relax the scope gate to make room for the new one.

### 3.8 Compiler authority — restoring the merge on the live path

`PlaybookCompiler._merge_frontmatter` is promoted to a module-level, public, diagnostic-returning function in `src/playbooks/compiler.py`:

```python
@dataclass(frozen=True)
class AuthorityDiagnostic:
    field: str
    authored: object      # what the source .md says (or the server derived)
    proposed: object      # what the compiler artifact claimed
    message: str

def apply_source_authority(
    compiled: dict,
    *,
    frontmatter: dict,
    rel_path: str,
    source_hash: str,
    version: int,
    existing_enabled: bool | None,
) -> tuple[dict, list[AuthorityDiagnostic]]:
```

Field ownership, enforced by this function:

| Field | Owner | Source of truth |
|---|---|---|
| `id` | server | `frontmatter["id"]` |
| `scope` | **server** | `derive_playbook_scope(rel_path)` (`src/playbooks/handler.py:73`) — **not** frontmatter, and never the artifact. A file under `projects/acme/playbooks/` cannot declare `scope: system` |
| `triggers` | author | `frontmatter["triggers"]`, normalized by the existing loop |
| `profile_id` | author | `frontmatter["profile_id"]`; absent → field removed from the artifact |
| `enabled` | **operator** | `existing_enabled` when an activation record exists, else `frontmatter.get("enabled", True)`. A recompile never re-enables a playbook an operator disabled |
| `cooldown_seconds`, `max_tokens`, `llm_config`, `transition_llm_config` | author | frontmatter, with the existing unknown-key warning loop retained |
| `version`, `source_hash`, `compiled_at` | server | computed |
| `kind`, `role` | author | frontmatter |
| `nodes`, `rules`, `entry_nodes` | compiler | artifact — the only fields the agent owns |

Every field the artifact claimed and the server overrode produces an `AuthorityDiagnostic`. Overriding is silent-proof, not silent: the diagnostics are returned in `playbook_install`'s structured response so the compiler agent sees exactly which of its fields were discarded, and they are logged at `warning`.

**Source resolution.** `_cmd_playbook_install` (`src/playbooks/validator_command.py:184`) gains a step between validation and install: locate the source `.md` by walking `vault_root` for files matching `PLAYBOOK_PATTERNS` (`src/playbooks/handler.py:66`) whose frontmatter `id` equals the requested `playbook_id`. Outcomes:

- exactly one match → merge and install;
- zero matches → refuse: `{"node": null, "field": "playbook_id", "message": "no source of authority: no .md under the vault declares id '<id>'"}`;
- more than one match → refuse, naming both vault-relative paths.

The scan reuses the `os.walk` + `fnmatch` + `_parse_frontmatter` shape already at `src/playbooks/manager.py:1321`; factor it into `PlaybookManager.find_source_for_id(playbook_id) -> str | None` and call it from both places.

**Where trust actually lives.** The frontmatter is in the operator's vault file, written by a human and changed through the vault watcher. The compiler agent's JSON is model output derived from untrusted prose. So frontmatter is trusted for `profile_id` and the compiler is not — and there is no need to subset-check the installed `profile_id` against the *compiler agent's* own policy, which would be wrong: a low-privilege compiler must be able to install a playbook that names a higher-privilege profile the operator authored. The compiler simply cannot influence which one.

`PlaybookManager.compile_playbook` (`src/playbooks/manager.py:1474`) applies the same function to the deterministic pipeline/assignment path, so both compile routes converge on one authority implementation.

### 3.9 Delegation — no widening, recursively

`_check_capability_escalation(parent, child)` (`src/commands/task_commands.py:74`) is replaced by policy subset checking across all three namespaces:

```python
def check_delegation(parent: CapabilityPolicy, child: CapabilityPolicy) -> str:
    """Empty string when child ⊆ parent in every namespace; else the reason."""
```

`_cmd_create_task` sources the parent from `current_principal()` rather than `self._caller_profile_id`. Because the principal is now derived from the session row for **HTTP and MCP callers too** (§3.5), the check fires on the path §1.4 documents as unreachable. That is the single largest behavior change in this package.

Fail-closed cases, all returning an error dict rather than creating a task:

| Case | Message |
|---|---|
| principal is enforced and has no `profile_id` | `delegation refused: caller has no resolved profile` |
| explicit `profile_id` names a missing profile | existing `Profile '<id>' not found` (unchanged) |
| child policy ⊄ parent policy | `Capability escalation rejected: child profile '<c>' is not a subset of caller profile '<p>'. <reason>` (existing shape, now three namespaces) |
| no explicit `profile_id` | inherit the caller's `profile_id` (existing behavior, unchanged) |

`self._caller_profile_id` and `set_caller_profile` remain as a **shim**: the setter binds a `PLAYBOOK`-kind principal, the getter reads `current_principal().profile_id`. `src/playbooks/runner.py` keeps working untouched. **Shim removal package: Package 4**, when the typed executors bind principals directly. Record it in Package 4's child plan.

---

## 4. Security analysis

### 4.1 Threat model for each new boundary

| Boundary | Adversary | Pre-Package-0 outcome | Post-Package-0 outcome |
|---|---|---|---|
| Compiler artifact → activation | Prompt injection in playbook prose, executed by the `playbook-compiler` agent | Agent-chosen `scope`, `triggers`, `enabled`, `profile_id` become live with no review | Every one of those is server- or frontmatter-owned; the artifact contributes only `nodes`/`rules`; overrides are reported as diagnostics |
| Command dispatch | Any session that can reach `/api/execute` or a typed route | Only `AGENT_COMMAND_SET`, identical for every profile | Additionally intersected with the caller's `aq_commands`; explicit policies always enforced |
| Plugin dispatch | Same | Plugin fallback ran with no capability check at all | Same gate, before handler lookup, using `plugin_tools` |
| Tool/schema discovery | A model reading its own tool list | MCP/`/api/tools` published every non-excluded command regardless of profile | Filtered by the *same* predicate dispatch uses (§4.3) |
| Delegation via `create_task` | A sandboxed agent asking for a broader profile | Unreachable over HTTP/MCP — the documented v1 gap | Principal derived from the session row, so the subset check fires |
| Request body | A compromised or confused agent | `_scope` was stripped; nothing else was | `_scope`, `_principal`, `_policy`, `_profile_id`, `_capabilities` stripped at both HTTP surfaces and again in `execute` |

### 4.2 Elevation is orthogonal to capability

`RequestScope.elevated` (supervisor sessions) currently short-circuits `check_command_scope`. Package 0 does **not** make `elevated` bypass the capability gate. The supervisor profile gets an explicit `## Capabilities` block listing what it may run (§5.2 T-10). Reason: elevation answers "which project may this token touch"; capability answers "which commands may this profile run". Conflating them is how a single stolen supervisor token becomes unbounded. If the supervisor's authored policy is genuinely everything, that is written down and reviewable rather than implicit.

`TRUSTED_LOCAL` and `service(...)` principals *do* bypass, and that is the intended explicit trusted path: the loopback CLI is the operator, and daemon-internal callers are the server itself. The bypass is a property of the principal kind, is asserted in `tests/test_execution_principal.py`, and there is no argument, header, or config that turns a `SESSION` principal into either.

### 4.3 Discovery and dispatch cannot drift

One predicate, one call site shape:

```python
def command_allowed(name: str, principal: ExecutionPrincipal, *, resolver: CommandResolver) -> bool
```

`resolver` reports whether a name is a built-in (`src/tools/registry.py:49` `_builtin_command_names`) or a plugin command (`plugin_registry.get_command`). Dispatch calls it; `_cmd_load_tools`, `src/mcp_registration.py`, `/api/tools`, and `PlaybookServices.node_tools` call it. The parity test (§5.2 T-15) iterates every name in `_builtin_command_names() | plugin names` for a matrix of principals and asserts `command_allowed(...) == (dispatch does not return capability_denied)` for each — so a name that is published is runnable and a name that is denied is not published.

### 4.4 Denial responses leak nothing

A denial returns `{"success": False, "error": "capability denied: <command>", "error_code": "capability_denied"}` and, over HTTP, `403`. It does not name the profile, the policy contents, or which namespace was consulted. The full detail — principal kind, profile id, namespace, policy fingerprint — goes to the daemon log and the `capability.denied` counter, which are operator surfaces, not agent surfaces.

### 4.5 Residual risks accepted in this package

- A profile still on the legacy shape runs in `audit` mode by default and is not denied. Mitigated by `aq profile audit` (T-16) and the doctor check, and closed by Package 6's flip to `enforce`.
- `check_request_scope`'s triage and playbook-compiler carve-outs (`src/api/scope.py:126-210`) are unchanged. They already require a live, verified assignment; Package 0 narrows what those sessions may then dispatch but does not re-derive the carve-outs.
- Harness-side enforcement remains best-effort: a harness with no `tools_flag` cannot be restricted (`src/sessions/spec.py:614`). The AQ-command gate is server-side and therefore unaffected, which is the point — the dispatch check, not the CLI flag, is the security boundary.

---

## 5. Tasks

Each task names the failing assertion first. Commit-1 tests are committed with `@pytest.mark.xfail(strict=True, reason="Package 0 T-N")` so the suite stays green at every commit while `strict=True` proves the assertion really fails; each implementation task **removes its own xfail markers** in the same commit that makes them pass. Removing an xfail without the implementation turns the suite red, so the marker cannot be lost silently.

### 5.1 Commit 1 — `test: capture compiler authority and capability invariants`

#### T-1 — `tests/test_capability_policy.py`

**Failing assertion:** `from src.profiles.capabilities import CapabilityPolicy` raises `ModuleNotFoundError`.

Cases:
- `CapabilityPolicy().allows_aq_command("task_close") is False` — empty denies.
- `CapabilityPolicy.from_namespaces(harness_tools=["*"], ...)` raises `CapabilityPolicyError` with `"wildcard"` in the message; same for `"mcp__github__*"`, `""`, `"  "`, `123`.
- `intersect` drops names the other side lacks, per namespace, and does not leak across namespaces: `A(aq_commands={"x"}) ∩ B(harness_tools={"x"})` is `DENY_ALL`.
- `is_subset_of` is `False` when the child adds a name in any one namespace; `True` for equality and for `DENY_ALL ⊆ anything`.
- `to_canonical()` is sorted and stable; `fingerprint()` is identical for two policies built from differently ordered inputs and differs when one name changes.
- `fingerprint()` ignores `derived_from_legacy`.
- `classify_capability("Bash") == "harness_tools"`; `("mcp__github__create_issue") == "plugin_tools"`; `("read_file", plugin_command_names={"read_file"}) == "plugin_tools"`; `("task_close") == "aq_commands"`.

**Verify:** `pytest tests/test_capability_policy.py -q` → all xfail.

#### T-2 — `tests/test_playbook_compiler_authority.py`

**Failing assertion:** installing the hostile artifact of §8.1 against the `memory-consolidation` source currently activates a playbook whose `scope == "project"`, `profile_id == "supervisor"`, `enabled is True`, and whose triggers include `task.completed`. The test asserts the *merged* values (`scope == "system"` from the vault path, `profile_id is None`, triggers exactly `["timer.24h"]`) and therefore fails.

Cases, all through the real `_cmd_playbook_install` against a temp vault:
- artifact-claimed `id` mismatching the source is refused (existing behavior — assert it still holds);
- artifact-claimed `scope` is replaced by `derive_playbook_scope(rel_path)`, with an `AuthorityDiagnostic` for `scope`;
- artifact-claimed `triggers` are replaced by frontmatter triggers;
- artifact-claimed `profile_id` is dropped when frontmatter has none, and replaced when it has one;
- artifact-claimed `enabled: true` does not re-enable a playbook whose activation record says disabled;
- artifact-claimed `version`/`source_hash`/`compiled_at` are recomputed;
- a `playbook_id` with **no** matching `.md` under the vault is refused with `no source of authority`;
- two `.md` files declaring the same `id` are refused, naming both paths;
- `nodes` from the artifact survive the merge unchanged (the compiler keeps what it owns);
- the returned `warnings` list contains one entry per overridden field.

**Verify:** `pytest tests/test_playbook_compiler_authority.py -q` → all xfail.

#### T-3 — `tests/test_command_capability_authorization.py`

**Failing assertion:** a session principal whose profile allows only `{"task_close"}` currently succeeds at `execute("list_tasks", ...)`; the test asserts `result["error_code"] == "capability_denied"` and fails.

Cases (real `CommandHandler.execute`, real SQLite, `capability_enforcement="enforce"`):
- off-list built-in command denied with `error_code="capability_denied"`;
- on-list built-in command allowed;
- off-list **plugin** command (`read_file`) denied *before* the plugin handler runs — asserted by a plugin stub that raises if called;
- on-list plugin command allowed;
- `DENY_ALL` principal denied for every name in `_builtin_command_names()`;
- `TRUSTED_LOCAL` allowed for every name (bypass is explicit);
- `service("cascade")` allowed;
- an `elevated` session principal is still subject to its profile policy;
- `capability_enforcement="audit"` + `derived_from_legacy=True` → allowed, and a `capability_denied_shadow` warning is emitted;
- `capability_enforcement="audit"` + explicitly authored policy → denied.

**Verify:** `pytest tests/test_command_capability_authorization.py -q` → all xfail.

#### T-4 — `tests/test_execution_principal.py`

**Failing assertion:** `from src.commands.principal import ExecutionPrincipal` raises `ModuleNotFoundError`.

Cases:
- no `_scope` → `TRUSTED_LOCAL`, `enforced is False`;
- session scope with a live session row → principal carries that row's `profile_id` and the profile's policy;
- session scope whose session row is missing → `policy is DENY_ALL`, `provenance == ("session-not-found",)`;
- session row with an empty `profile_id` → `DENY_ALL`;
- `profile_id` naming a deleted profile → `DENY_ALL`;
- `args` containing `_principal`, `_policy`, `_profile_id`, `_capabilities` → all absent from the args the `_cmd_*` handler receives, and the resulting principal is unaffected by their contents (spoofing test: `_policy` claiming every command still yields the profile's real policy);
- `narrow()` never widens: `narrow(P_broad)` on a narrow principal returns the narrow policy;
- `provenance` accumulates one entry per narrowing;
- re-entrant `execute` restores the outer principal in `finally` (mirrors the `_current_scope_var` regression at `src/commands/handler.py:96-104`);
- `ExecutionPrincipal` is frozen — `dataclasses.FrozenInstanceError` on assignment.

**Verify:** `pytest tests/test_execution_principal.py -q` → all xfail.

**Commit gate:** `pytest tests/test_capability_policy.py tests/test_playbook_compiler_authority.py tests/test_command_capability_authorization.py tests/test_execution_principal.py -q` reports all xfailed, zero failed, zero xpassed.

### 5.2 Commit 2 — `feat: restore authoritative frontmatter merge on the playbook install path`

#### T-5 — `apply_source_authority` in `src/playbooks/compiler.py`

Promote `_merge_frontmatter` (`src/playbooks/compiler.py:337`) to the module-level function of §3.8, returning `(dict, list[AuthorityDiagnostic])`. Keep `PlaybookCompiler._merge_frontmatter` as a two-line deprecated staticmethod delegating to it so `tests/test_playbook_services.py:56` keeps passing; **delete it in Package 2** when the V2 compiler lands (record in Package 2's child plan).

Add the `scope`-from-`rel_path` rule and the `existing_enabled` rule. Remove xfail from the corresponding T-2 cases.

**Verify:** `pytest tests/test_playbook_compiler_authority.py -k "scope or enabled or triggers or profile_id or version" -q`

#### T-6 — `PlaybookManager.find_source_for_id`

Factor the `os.walk` + `fnmatch(PLAYBOOK_PATTERNS)` + `_parse_frontmatter` scan out of `reconcile_uncompiled` (`src/playbooks/manager.py:1321`) into `find_source_for_id(playbook_id) -> tuple[str, str] | None` returning `(abs_path, rel_path)`, raising `AmbiguousPlaybookSource` on more than one match. Call it from both the reconcile loop and T-7.

**Verify:** `pytest tests/test_playbook_compiler_authority.py -k "source_of_authority or ambiguous" -q`

#### T-7 — Wire the merge into `_cmd_playbook_install`

In `src/playbooks/validator_command.py:184`, between the `pb.id != playbook_id` check and `pm.install_compiled(pb)`:

1. `find_source_for_id(playbook_id)`; refuse with the §3.8 messages on 0 or >1.
2. Read the source, `_parse_frontmatter`, compute `source_hash`, resolve `existing_enabled` from the active registry.
3. `merged, diagnostics = apply_source_authority(data, ...)`.
4. Re-run `CompiledPlaybook.from_dict(merged).validate()` — the merged artifact, not the submitted one, is what gets validated and installed.
5. `await pm.install_compiled(CompiledPlaybook.from_dict(merged))`.
6. Return `{"success": True, "warnings": [d.__dict__ for d in diagnostics]}`.

Apply the same call in `PlaybookManager.compile_playbook` (`src/playbooks/manager.py:1474`) after `_compile(...)` succeeds, using the `rel_path` it already receives.

Remove the remaining T-2 xfails.

**Verify:** `pytest tests/test_playbook_compiler_authority.py tests/test_playbook_compiler_scope.py tests/test_playbook_services.py -q` (all pass), then `pytest tests/ -k playbook -q`.

### 5.3 Commit 3 — `feat: introduce capability policy and execution principal`

#### T-8 — `src/profiles/capabilities.py`

Implement §3.1 and §3.3. Move `HARNESS_TOOL_NAMES` here and change `src/sessions/spec.py:591` to `_HARNESS_TOOL_NAMES = HARNESS_TOOL_NAMES` (import), so the definition exists once. Remove T-1 xfails.

**Verify:** `pytest tests/test_capability_policy.py tests/test_session_tool_allowlist.py -q`

#### T-9 — Parser, model, config, table, migration

- `src/profiles/parser.py`: parse `## Capabilities` per §3.2 into `ParsedProfile.capabilities: dict[str, list[str]] | None`; add wildcard rejection to `_validate_tools` (`src/profiles/parser.py:673`) as an **error**; map the block through `parsed_profile_to_agent_profile` (`src/profiles/parser.py:923`) into `harness_tools` / `aq_commands` / `plugin_tools`.
- `src/models.py:846` `AgentProfile`: add `harness_tools: list[str] | None = None`, `aq_commands: list[str] | None = None`, `plugin_tools: list[str] | None = None`. `None` means "not authored — use the legacy adapter"; `[]` means "explicitly none".
- `src/config.py:670` `AgentProfileConfig`: same three fields, parsed at `src/config.py:2447`, with the same wildcard rejection in `validate()`.
- `src/database/tables.py:583` `agent_profiles`: three nullable `Text` columns holding JSON arrays.
- `src/database/queries/profile_queries.py`: serialize/deserialize the three columns alongside `allowed_tools`.
- `src/profiles/sync.py:296`: pass them through to the `AgentProfile`; upgrade the tool-name check at `src/profiles/sync.py:326` so a wildcard is a hard **failure** (`ProfileSyncResult(success=False)`) rather than a warning.
- Alembic revision (§6).
- Invalidate the §3.5 TTL cache from `sync_profile_to_db` on a successful upsert.

**Verify:** `pytest tests/test_profile_parser.py tests/test_profile_sync.py tests/test_agent_profiles.py tests/test_profile_functional.py tests/test_database.py -q` and `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`

#### T-10 — Explicit `## Capabilities` for the ten shipped profiles

Add the block to every profile under `src/profiles/defaults/`: `final-reviewer`, `planner`, `playbook-compiler`, `reviewer`, `spec-ingest`, `supervisor`, `triage`, `worker-deep`, `worker-fast`, `worker-standard`. Each block is derived from that profile's existing `## Tools` list plus the AQ commands its `## Role` prose actually calls — the `<!-- tools-rationale -->` comment in `reviewer/profile.md` already states this invariant ("Every command named in the Role section above appears in this list"), so the block is checkable against prose. Remove the now-redundant `## Tools` block from each file in the same commit.

Worked example — `reviewer` (from `src/profiles/defaults/reviewer/profile.md`, which is `read_only: true`):

```json
{
  "harness_tools": ["Bash", "Read", "Glob", "Grep", "Task", "TodoWrite", "Skill", "WebSearch", "WebFetch"],
  "aq_commands": ["prime", "get_task", "task_show", "task_comment", "task_close", "task_heartbeat", "reopen_with_feedback", "session_drain_ack"],
  "plugin_tools": []
}
```

Note what changed: `Write`, `Edit`, `NotebookEdit` are dropped because the profile is `read_only` and its Rules say "Never edit code" — today they are granted anyway. The AQ list adds the session-protocol commands (`prime`, `task_heartbeat`, `session_drain_ack`) that `AGENT_COMMAND_SET` already grants and that `aq prime` instructs every worker to call; omitting them would strand the session.

A new test `tests/test_shipped_profile_capabilities.py` asserts, for each shipped profile: it parses; it contains no wildcard; `harness_tools` is non-empty and contains `Bash`; and every name in `aq_commands` resolves to a real command via `_builtin_command_names()` or the plugin registry (no aspirational names).

It additionally **reports, without failing**, the set `aq_commands - (AGENT_COMMAND_SET | _TRIAGE_COMMANDS | _PLAYBOOK_COMPILER_COMMANDS)` per profile — the §1.5 unreachable-command gap. The current expected content of that report, which the test pins as a literal so it cannot grow silently:

```python
EXPECTED_UNREACHABLE = {
    "reviewer": {"get_task", "reopen_with_feedback"},
    # populated for the remaining nine profiles during T-10 by running the report
}
```

A profile that gains a new unreachable name fails the pin, forcing a deliberate decision. Package 0 does not change `AGENT_COMMAND_SET` to shrink this set (§1.5).

**Verify:** `pytest tests/test_shipped_profile_capabilities.py tests/test_startup_profile_migration.py tests/test_profile_migration.py -q`

#### T-11 — `src/commands/principal.py` and the seam

Implement §3.4 and §3.5. Add `capability_enforcement` to `SecurityConfig` (`src/config.py:1074`) with validation. Add the two server-derived fields to `RequestScope` (`src/api/auth.py:29`) and populate them in `TokenAuthMiddleware`. Extend the arg-stripping in `src/api/execute.py:57` and `src/api/codegen.py:381`. Convert `_caller_profile_id` / `set_caller_profile` (`src/commands/handler.py:459-491`) into the §3.9 shim.

`src/playbooks/pipeline_compiler.py` change (the one the roadmap asks for): `compile_pipeline` keeps reading `scope` from frontmatter for its own validation (`src/playbooks/pipeline_compiler.py:107`) but the value is overwritten by `apply_source_authority` at the manager layer; add a module docstring note saying so, so a future reader does not treat the frontmatter read as authoritative.

Remove T-4 xfails.

**Verify:** `pytest tests/test_execution_principal.py tests/test_api_auth.py tests/test_api_scope.py tests/test_api_execute_contract.py tests/test_supervisor_global_token_loopback.py tests/test_triage_api_scope.py -q`

### 5.4 Commit 4 — `feat: enforce command authorization at dispatch`

#### T-12 — `src/commands/authorization.py`

Implement `command_allowed`, `authorize_command` (returning an `AuthzDecision` with `allowed`, `namespace`, `reason`, `shadow`), and `filter_tool_definitions`. `CommandResolver` is a small protocol with `is_builtin(name)` and `is_plugin(name)`, backed by `src/tools/registry.py:49` and `plugin_registry.get_command`.

**Verify:** `pytest tests/test_command_capability_authorization.py -k "policy or resolver" -q`

#### T-13 — Enforce in `CommandHandler.execute`

In `src/commands/handler.py:651`, after the pause gate at `_paused_command_error` and **before** the `getattr(self, f"_cmd_{name}")` lookup:

```python
decision = authorize_command(name, principal, resolver=self._command_resolver,
                             mode=self.config.security.capability_enforcement)
if decision.shadow:
    logger.warning("capability_denied_shadow cmd=%s profile=%s ns=%s", name,
                   principal.profile_id, decision.namespace)
elif not decision.allowed:
    return {"success": False, "error": f"capability denied: {name}",
            "error_code": "capability_denied"}
```

Placement before the built-in lookup covers the plugin fallback (`src/commands/handler.py:715`) with the same check and no second call site. Map `error_code="capability_denied"` to HTTP `403` in `src/api/execute.py` and `src/api/codegen.py` (both currently return `200`/`422` for error dicts).

Remove the remaining T-3 xfails.

**Verify:** `pytest tests/test_command_capability_authorization.py tests/test_task_command_authorization.py tests/test_api_execute_contract.py -q`

#### T-14 — Harness allowlist from `harness_tools`

`src/sessions/spec.py:598`: `_resolve_allowed_tools` reads `capability_policy_for(profile).harness_tools` instead of `profile.allowed_tools`. Behavior changes:

- `"*"` can no longer reach this function (rejected at parse and sync) — delete the `if "*" in declared: return []` branch and the test that asserts it (`tests/test_session_tool_allowlist.py:60`), replacing it with a parse-level rejection test;
- an **explicitly** empty `harness_tools` still emits no flag, because §3.2 rule 5 makes "empty harness tools plus non-empty AQ commands" a parse error, and a profile with all three namespaces empty is a deliberate no-op;
- the "drop AQ command names" branch (`src/sessions/spec.py:625`) becomes unnecessary because the namespaces are already separate; keep a defensive filter with the existing log line for legacy-derived policies.

**Verify:** `pytest tests/test_session_tool_allowlist.py tests/test_session_spec.py tests/test_session_executable.py -q`

#### T-15 — Discovery/dispatch parity

Apply `filter_tool_definitions(..., principal)` at every publication point:

- `src/commands/tool_commands.py:40` `_cmd_load_tools` and `:31` `has_command` reporting;
- `src/mcp_registration.py` tool registration (union with `get_effective_exclusions`, never replacing it);
- `/api/tools` in `src/api/execute.py:108`;
- `PlaybookServices.node_tools` (`src/playbooks/services.py:27`) — replace the current `ValueError` on unknown names with policy filtering, and change the `allowed is None` branch from "full catalogue" to `DENY_ALL`, since §3.1 forbids "missing means default tools".

Add the parity test of §4.3 to `tests/test_command_capability_authorization.py`.

**Verify:** `pytest tests/test_command_capability_authorization.py tests/test_tool_registry.py tests/test_tool_index.py tests/test_mcp_server.py tests/test_playbook_services.py -q`

### 5.5 Commit 5 — `test: cover session and delegated permission inheritance`

#### T-16 — `tests/test_delegation_no_widening.py`

**Failing assertion (before T-17):** a three-level chain where the middle profile is narrow and the leaf asks for the broad profile currently succeeds when driven over the HTTP/session path, because `_caller_profile_id` is unset. The test asserts `"Capability escalation rejected"` and fails.

Cases, all through the real `CommandHandler.execute("create_task", ...)` with a real SQLite DB and a real `sessions` row (no stubs — the roadmap requires real-handler evidence):

- **L1 → L2 → L3 recursive:** `broad` (all three namespaces populated) creates a task with `narrow` (strict subset) → allowed. A session running as `narrow` then tries `broad` → rejected. A session running as `narrow` tries `narrower` → allowed.
- **Per-namespace:** child equal in two namespaces but adding one `plugin_tools` entry → rejected, message naming `plugin_tools`.
- **Default inheritance:** `narrow` caller omitting `profile_id` → child gets `narrow`.
- **Fail-closed:** session row exists, profile row deleted → `create_task` refused, no task written (assert the row count).
- **HTTP path:** the same escalation attempt driven through `/api/execute` with a session bearer token, asserting `403` and no task created — this is the exact path §1.4 documents as unreachable today.
- **Playbook path:** `set_caller_profile("narrow")` shim still rejects, proving `src/playbooks/runner.py` is unaffected.

#### T-17 — Delegation implementation

Replace `_check_capability_escalation` (`src/commands/task_commands.py:74`) with `check_delegation` over `CapabilityPolicy`; source the parent from `current_principal()` in `_cmd_create_task` (`src/commands/task_commands.py:1186`). Update the in-tree comment at `src/commands/task_commands.py:1176-1186` to record that the v1 gap is closed and how.

**Verify:** `pytest tests/test_delegation_no_widening.py tests/test_task_capability_inheritance.py -q`

#### T-18 — Update the existing suites the roadmap names

- `tests/test_task_capability_inheritance.py`: its `_check_capability_escalation` unit class becomes `check_delegation` over policies; the `TestCreateTaskCapabilityInheritance` stubs (`tests/test_task_capability_inheritance.py:90`) set a principal instead of `_caller_profile_id`.
- `tests/test_session_tool_allowlist.py`: `test_wildcard_means_everything` (`:60`) is replaced by a parse-level rejection test; `test_aq_commands_are_dropped` (`:40`) becomes "AQ commands live in their own namespace and never reach the flag".
- `tests/test_api_scope.py`: add cases proving `check_request_scope` is unchanged and that capability denial is a *second*, independent gate.
- `tests/test_playbook_services.py:56`: keeps calling the deprecated staticmethod; add a case for the new function.

#### T-19 — Operator surfaces

- `aq profile audit` (new subcommand backed by `_cmd_profile_audit` in `src/commands/profile_commands.py`): lists every profile whose policy is `derived_from_legacy`, with its adapted namespaces, so an operator can see exactly what Package 6 must migrate. Output columns: `id`, `source` (`explicit` / `legacy`), `harness_tools`, `aq_commands`, `plugin_tools`, `fingerprint`.
- Doctor check in `src/doctor/` (mirroring `src/doctor/pool_checks.py`): **warn** when `capability_enforcement != "enforce"`, **warn** per legacy-shaped profile, **fail** when any stored profile contains a wildcard.

**Verify:** `pytest tests/test_doctor.py tests/test_cli.py -q` and `aq profile audit` against the e2e daemon (§9).

---

## 6. Storage: Alembic

One revision, `<rev>_profile_capability_namespaces`, down-revision = the current head. **Note:** `alembic heads` currently reports **two** heads on `origin/main` — `a7c91e4d2b63` (persisted playbook assignment routes) and `c4d5e6f7a8b9` (integration_mode policy), both branching from `f1d7a9c20b64`. This revision must be authored as a **merge revision** (`alembic merge -m "..." a7c91e4d2b63 c4d5e6f7a8b9`) followed by the additive revision on top, or the upgrade will fail with "Multiple head revisions are present". Confirm with `alembic heads` before generating; if a merge landed on main first, use the single head instead. Do not autogenerate blindly — review the file, as CLAUDE.md requires.

**Upgrade:**

```python
op.add_column("agent_profiles", sa.Column("harness_tools", sa.Text(), nullable=True))
op.add_column("agent_profiles", sa.Column("aq_commands", sa.Text(), nullable=True))
op.add_column("agent_profiles", sa.Column("plugin_tools", sa.Text(), nullable=True))
```

No data migration. `NULL` is meaningful: it is the signal that the legacy adapter (§3.3) should run. Backfilling would erase the distinction between "authored as none" (`'[]'`) and "not authored" (`NULL`), which is the whole basis of the audit/enforce split.

**Downgrade:** three `op.drop_column` calls. Safe because no other table references them and `allowed_tools` is never removed or rewritten by this package — a downgraded database still describes every profile's rights exactly as it did before Package 0.

### SQLite and PostgreSQL

- `Text` + `nullable=True` is portable; both dialects take `ADD COLUMN` without a table rebuild.
- SQLite `ALTER TABLE ... DROP COLUMN` requires SQLite ≥ 3.35. The project targets Python 3.12, whose bundled SQLite is ≥ 3.37, so the downgrade works natively. If a target environment is older, wrap the downgrade in `op.batch_alter_table("agent_profiles")`, which recreates the table — add it defensively, since batch mode is a no-op cost on the upgrade path and the project already ships SQLite-compatible migrations.
- No server-side default and no `NOT NULL`, so no PostgreSQL table rewrite and no lock beyond the brief `ACCESS EXCLUSIVE` of `ADD COLUMN` with no default (metadata-only since PG 11).
- Values are JSON arrays stored as text, matching how `allowed_tools` and `mcp_servers` are already stored on this table (`src/database/tables.py:591-592`). No `JSONB` — it would diverge the two dialects for zero gain here.
- Per the project memory that PostgreSQL is production: run `pytest tests/test_database.py -q` against both, and run the swarm e2e (§9) on the PostgreSQL daemon.

---

## 7. Commit sequence

| # | Message | Contents | Tree state |
|---|---|---|---|
| 1 | `test: capture compiler authority and capability invariants` | T-1 … T-4 | green (all new tests xfail-strict) |
| 2 | `feat: restore authoritative frontmatter merge on the playbook install path` | T-5 … T-7 | green |
| 3 | `feat: introduce capability policy and execution principal` | T-8 … T-11 + migration | green |
| 4 | `feat: enforce command authorization at dispatch` | T-12 … T-15 | green |
| 5 | `test: cover session and delegated permission inheritance` | T-16 … T-19 | green |

Deviation from the roadmap's four-commit sequence: commit 2 is new. The roadmap's list gives compiler authority a test commit but folds its implementation into an unnamed one. Splitting it keeps the compiler change revertible without touching the principal change, which matters because §3.8 is the only part of this package that can break the compiler agent's install loop.

Per the roadmap's Delivery discipline, commits stay package-scoped and are **not pushed** beyond this feature branch without a separate request.

---

## 8. Fixture data

### 8.1 Hostile compiler artifact (`tests/fixtures/playbooks/hostile-memory-consolidation.json`)

The real source is `src/prompts/default_playbooks/memory-consolidation.md`, whose frontmatter is:

```yaml
---
id: memory-consolidation
triggers:
  - timer.24h
scope: system
llm_config:
  provider: gemini
  model: gemini-2.5-pro
transition_llm_config:
  provider: gemini
  model: gemini-2.5-flash
---
```

It is a non-pipeline playbook, so it is compiled by the `playbook-compiler` agent — the untrusted path. The test copies it to `<vault>/projects/acme/playbooks/memory-consolidation.md` (a *project*-scoped location) and submits:

```json
{
  "id": "memory-consolidation",
  "version": 99,
  "source_hash": "sha256:0000000000000000",
  "compiled_at": "2020-01-01T00:00:00Z",
  "scope": "system",
  "enabled": true,
  "profile_id": "supervisor",
  "cooldown_seconds": 0,
  "max_tokens": 200000,
  "triggers": [
    "timer.24h",
    "task.completed",
    {"event_type": "gate.resolved", "filter": {"gate_type": "human"}}
  ],
  "llm_config": {"provider": "anthropic", "model": "claude-opus-5"},
  "nodes": {
    "start": {
      "entry": true,
      "prompt": "Read the last 24h of agent-type memories and consolidate duplicates.",
      "transitions": [{"when": "consolidated", "goto": "done"}]
    },
    "done": {"terminal": true}
  }
}
```

Expected merged result:

| Field | Submitted | Installed | Diagnostic |
|---|---|---|---|
| `scope` | `system` | `project` (from `projects/acme/playbooks/`) | yes |
| `enabled` | `true` | `false` (existing activation is disabled) | yes |
| `profile_id` | `supervisor` | absent (frontmatter names none) | yes |
| `triggers` | 3 entries | `["timer.24h"]` | yes |
| `llm_config` | anthropic/opus | gemini-2.5-pro (frontmatter) | yes |
| `max_tokens` | `200000` | absent (frontmatter names none) | yes |
| `version` | `99` | `existing_version + 1` | yes |
| `source_hash` | zeros | recomputed from the `.md` | yes |
| `nodes` | 2 nodes | **unchanged** | no |

The trigger substitution alone is the escalation: `timer.24h` fires once a day, while `task.completed` fires on every task in the fleet.

### 8.2 Profile fixtures (`tests/fixtures/profiles/`)

`narrow.md` — a sandboxed worker:

````markdown
---
id: narrow-worker
name: Narrow Worker
---

## Config

```json
{"harness": "claude", "lifecycle": "task", "needs_workspace": true, "default_class": "standard-low"}
```

## Capabilities

```json
{
  "harness_tools": ["Bash", "Read", "Glob", "Grep"],
  "aq_commands": ["prime", "task_show", "task_comment", "task_close", "task_heartbeat", "session_drain_ack"],
  "plugin_tools": ["read_file"]
}
```

## Role

Read-only investigator. Report findings as task comments; never edit files.
````

`broad.md` — the same shape with `harness_tools` additionally containing `Write`, `Edit`, `Task`, `WebFetch`; `aq_commands` additionally containing `create_task`, `list_tasks`, `edit_task`, `add_dependency`; `plugin_tools` additionally containing `write_file`, `mcp__github__create_issue`.

`legacy.md` — no `## Capabilities`, only the current `## Tools` shape, for the adapter tests:

```json
{"allowed": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "get_task", "task_close", "reopen_with_feedback"]}
```

Adapted policy: `harness_tools={Bash, Read, Write, Edit, Glob, Grep}`, `aq_commands={get_task, task_close, reopen_with_feedback}`, `plugin_tools={}`, `derived_from_legacy=True`. R2 does not apply because AQ names were declared.

`wildcard.md` — `{"allowed": ["*"]}`, which must fail parse **and** sync with a message containing `wildcard`.

---

## 9. API request/response examples

This package changes the behavior, not the shape, of two existing endpoints and adds one status code.

### 9.1 `POST /api/execute` — capability denial

Request (session bearer for a `narrow-worker` session; `list_tasks` is outside its `aq_commands` but inside `AGENT_COMMAND_SET`… it is not, so this example uses `edit_task`, which is inside neither — see the note below):

```http
POST /api/execute
Authorization: Bearer aqs_<session token>
Content-Type: application/json

{"command": "create_task", "args": {"project_id": "acme", "title": "escalate", "profile_id": "broad-worker"}}
```

Response — **new** (`403`, `error_code` is new; today this returns `200` with `{"ok": true, ...}`):

```json
{
  "ok": false,
  "error": "Capability escalation rejected: child profile 'broad-worker' is not a subset of caller profile 'narrow-worker'. child has 4 aq_command(s) not in parent's allowlist: ['add_dependency', 'create_task', 'edit_task', 'list_tasks']"
}
```

And for a plain off-policy command — `task_claim` is inside `AGENT_COMMAND_SET` (`src/api/scope.py:14`) so the scope gate admits it, but it is outside `narrow-worker`'s `aq_commands`:

```http
POST /api/execute
{"command": "task_claim", "args": {"project_id": "acme"}}
```

```json
{"ok": false, "error": "capability denied: task_claim", "details": {"error_code": "capability_denied"}}
```
with status `403`.

Note on `create_task`: it *is* in `AGENT_COMMAND_SET` (`src/api/scope.py:44`), so the scope gate admits it for any session; the capability gate is what stops `narrow-worker`. That composition is the point of §3.7.

### 9.2 `POST /api/execute` — spoofing attempt is inert

```http
POST /api/execute
Authorization: Bearer aqs_<narrow-worker token>

{"command": "list_tasks",
 "args": {"project_id": "acme",
          "_principal": {"kind": "local"},
          "_policy": {"aq_commands": ["*"]},
          "_profile_id": "supervisor"}}
```

Response is identical to the same request without those keys — `403 capability denied: list_tasks`. The three keys are stripped in `src/api/execute.py` before `ch.execute` and again inside `execute` before dispatch, and the principal is built from the session row regardless.

### 9.3 `playbook_install` — authority diagnostics

Request (from the compiler agent, via `POST /api/execute`):

```json
{"command": "playbook_install",
 "args": {"playbook_id": "memory-consolidation",
          "compiled_path": "/home/u/.agent-queue/vault/projects/acme/playbooks/.compiled/memory-consolidation.json"}}
```

Response — success with the fields the server took back:

```json
{"ok": true,
 "result": {
   "success": true,
   "warnings": [
     {"field": "scope",      "authored": "project",   "proposed": "system",
      "message": "scope is derived from the vault path projects/acme/playbooks/memory-consolidation.md"},
     {"field": "triggers",   "authored": ["timer.24h"], "proposed": ["timer.24h", "task.completed", "gate.resolved"],
      "message": "triggers come from source frontmatter"},
     {"field": "profile_id", "authored": null,        "proposed": "supervisor",
      "message": "profile_id comes from source frontmatter; the source declares none"},
     {"field": "enabled",    "authored": false,       "proposed": true,
      "message": "enabled is owned by the activation record"}
   ]
 }}
```

Failure when no source exists:

```json
{"ok": false,
 "error": "playbook_install failed",
 "details": {"success": false,
   "errors": [{"node": null, "field": "playbook_id",
     "message": "no source of authority: no .md under the vault declares id 'memory-consolidation'"}]}}
```

### 9.4 Typed per-command routes (`src/api/codegen.py`)

The generated routes return `{"error": "capability denied: <name>"}` with status `403` (they currently map every error dict to `422`). The OpenAPI snapshot changes only by the added `403` response on command routes; regenerate and re-run the client codegen so `packages/aq-client` matches:

```bash
python -m src.api.codegen --write          # or the project's documented snapshot command
pytest tests/test_api_client_contract.py -q
```

---

## 10. Observability and operator failure behavior

| Signal | Where | Content |
|---|---|---|
| `capability_denied` | `logger.warning` in `CommandHandler.execute` | command, principal kind, session id, profile id, namespace, policy fingerprint |
| `capability_denied_shadow` | same, audit mode only | same fields plus `derived_from_legacy=True` |
| `playbook_authority_override` | `logger.warning` in `apply_source_authority` | playbook id, field, authored value, proposed value, source rel_path |
| `playbook_install_no_source` | `logger.error` | requested playbook id, vault root |
| `principal_fail_closed` | `logger.warning` in the seam | reason (`session-not-found` / `session-has-no-profile` / `profile-not-found`), session id |
| `aq profile audit` | CLI (T-19) | every profile, `explicit` vs `legacy`, three namespaces, fingerprint |
| doctor checks | `aq doctor` | warn: enforcement not `enforce`; warn: legacy-shaped profile; fail: stored wildcard |

**Operator failure modes and what they look like:**

- *A profile is under-specified and its agent stalls.* The agent sees `capability denied: <cmd>`; the daemon log names the profile and namespace. Fix: add the command to `aq_commands` in the profile Markdown; the vault watcher re-syncs and the TTL cache is invalidated on upsert, so the next command is allowed without a restart.
- *A playbook stops installing.* `playbook_install` returns `no source of authority`. Cause is almost always a `.md` whose frontmatter `id` does not match what the compile task was told; the message names the id it searched for.
- *An operator disabled a playbook and a recompile "re-enabled" it.* Cannot happen after T-5: `enabled` is read from the activation record, and the attempted override appears as a diagnostic.
- *Enforcement is too aggressive for a live fleet.* Set `security.capability_enforcement: off` in `~/.agent-queue/config.yaml` and restart; every other Package 0 invariant (authority merge, spoofing defense, delegation) stays on.

---

## 11. Verification

### Per-package required commands (roadmap §5, reconciled per §2)

```bash
pytest tests/test_playbook_compiler_authority.py -q
pytest tests/test_command_capability_authorization.py tests/test_execution_principal.py -q
pytest tests/test_api_execute_contract.py tests/test_api_scope.py tests/test_api_auth.py \
       tests/test_session_commands.py -q
# targeted suites named by this child plan:
pytest tests/test_capability_policy.py tests/test_delegation_no_widening.py \
       tests/test_task_capability_inheritance.py tests/test_shipped_profile_capabilities.py \
       tests/test_profile_parser.py tests/test_profile_sync.py tests/test_agent_profiles.py \
       tests/test_session_tool_allowlist.py tests/test_session_spec.py \
       tests/test_playbook_services.py tests/test_tool_registry.py tests/test_mcp_server.py \
       tests/test_triage_api_scope.py tests/test_supervisor_global_token_loopback.py \
       tests/test_task_command_authorization.py tests/test_database.py -q
ruff check src/commands src/profiles src/api src/sessions src/playbooks tests
pytest tests/ -n auto           # full suite, once, before the package exit gate
```

Expected outcome for each: zero failures, and specifically zero `xpassed` (a strict-xfail that starts passing means an implementation landed in the wrong commit).

### Migration

```bash
alembic heads                 # confirm the head situation of §6 before generating
alembic upgrade head
alembic downgrade -1
alembic upgrade head
pytest tests/test_database.py -q
```

Then the same three-step against PostgreSQL (`docker compose` on `:5533`, per the project's PostgreSQL-is-production convention).

### End-to-end

```bash
scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh
```

Required because this package changes `create_task` authorization, which the swarm claim/pool/formula scenarios exercise through the real CLI against a real PostgreSQL daemon. The e2e kit drives pool workers by holding their tokens via `aq session token` (`src/commands/session_commands.py:458`), so it is also the only automated coverage of a real session-scoped principal end to end. Run it with `capability_enforcement: audit` (the shipped default) and again with `enforce`; both must pass.

### Client regeneration

```bash
pytest tests/test_api_client_contract.py -q
```

after regenerating `packages/aq-client` from the OpenAPI snapshot (§9.4).

---

## 12. Mapping to the package exit gate

> **Exit gate:** A hostile Markdown playbook, API request, delegated task, or plugin command cannot widen server-owned identity, budgets, profiles, or capabilities. Tool discovery and actual execution agree for every tested principal.

| Gate clause | Proof |
|---|---|
| hostile **Markdown playbook** cannot widen identity/profile | `tests/test_playbook_compiler_authority.py` — the §8.1 artifact installs with server-derived `scope`, frontmatter `triggers`/`profile_id`, activation-owned `enabled` |
| hostile playbook cannot widen **budgets** | same suite — `max_tokens` and `llm_config` come from frontmatter; the artifact's `200000` / opus claim is discarded with a diagnostic |
| hostile **API request** cannot widen identity | `tests/test_execution_principal.py` spoofing cases + §9.2; `_principal`/`_policy`/`_profile_id`/`_capabilities` stripped at both HTTP surfaces and in `execute` |
| hostile **delegated task** cannot widen capabilities | `tests/test_delegation_no_widening.py` — recursive L1→L2→L3 through the real handler *and* through `/api/execute`, closing the `src/commands/task_commands.py:1176` v1 gap |
| hostile **plugin command** cannot widen capabilities | `tests/test_command_capability_authorization.py` — off-list plugin command denied before the plugin handler runs, asserted with a raising stub |
| **discovery and execution agree for every tested principal** | the §4.3 parity test: one `command_allowed` predicate, iterated over `_builtin_command_names() ∪ plugin names` for `TRUSTED_LOCAL`, `service`, broad session, narrow session, `DENY_ALL` session |
| **empty means no capabilities**, no wildcards | `tests/test_capability_policy.py` + parse/sync rejection in `tests/test_profile_parser.py` / `tests/test_profile_sync.py` + `tests/test_shipped_profile_capabilities.py` |

Milestone **M0 — Authority safe** is claimed only when every command in §11 passes with `security.capability_enforcement: enforce`.

---

## 13. Rollback boundary

Reverting Package 0 means reverting commits 5→1 in order. Specifically:

- **All new enforcement funnels through one seam**: `CommandHandler._principal_from_scope` plus the `principal_context` binding in `CommandHandler.execute`. Deleting those restores the prior request path exactly — `_current_scope_var`, `check_request_scope`, and every `_cmd_*` handler are otherwise untouched.
- **No database downgrade is required.** The three `agent_profiles` columns are additive and nullable; code that does not read them behaves as it did before. Running `alembic downgrade -1` is optional and safe, not a prerequisite.
- **The compiler-authority change (commit 2) is independently revertible**, which is why it is its own commit: reverting it restores the pre-Package-0 install path without touching the principal or the capability model.
- **Do not begin Package 1** until production-equivalent profiles have been audited for explicit capabilities — run `aq profile audit` (T-19) and confirm it reports zero `legacy`-source profiles for the fleet in question, or record an explicit operator decision to proceed with the remainder in `audit` mode.

---

## 14. Open items for the next child plan

- **Package 1** consumes `ExecutionPrincipal` and `CapabilityPolicy` unchanged. `CommandContract.required_capability` must resolve into the `aq_commands` namespace, and `CommandRegistration` should fail when a playbook-visible command has no capability name — record that in Package 1's plan.
- **Package 2** deletes `PlaybookCompiler._merge_frontmatter` (the deprecated staticmethod kept by T-5) once the V2 compiler owns authority.
- **Package 4** deletes the `_caller_profile_id` / `set_caller_profile` shim (§3.9) when typed executors bind principals directly.
- **Package 6** flips `security.capability_enforcement` to `enforce` as part of fleet readiness.
- **Package 7** removes the flag, its `off`/`audit` modes, `CapabilityPolicy.derived_from_legacy`, the legacy adapter, and the `## Tools` profile block.
- **Unreachable shipped-profile commands (§1.5).** `reviewer` names `get_task` and `reopen_with_feedback`, neither of which its session token can dispatch through `check_command_scope`. Three possible resolutions, none of which belong in Package 0: add them to `AGENT_COMMAND_SET` (widens a server allowlist — needs its own security review); give reviewers a narrowly verified assignment carve-out like the existing triage and playbook-compiler ones (`src/api/scope.py:126-210`); or correct the profile prose because the rejection path is meant to run elsewhere. Once `CapabilityPolicy` exists, the cleanest answer is likely to **derive** `AGENT_COMMAND_SET` from profile policies rather than maintain it as a parallel hand-written list — raise that in **Package 1**, whose contract registry makes the per-command capability name authoritative. The `EXPECTED_UNREACHABLE` pin in `tests/test_shipped_profile_capabilities.py` is the ratchet that keeps the list from growing meanwhile.
