# Dashboard state contract — typed server-backed namespaces

**Task:** `amber-stone.2` (epic `amber-stone`, "make dashboard state server-backed and
synchronized across machines")
**Date:** 2026-09-10
**Status:** approved contract, not yet implemented
**Input:** [the state-boundary inventory](2026-09-10-dashboard-state-boundary-inventory.md)
(`amber-stone.1`)
**Consumers:** `amber-stone.3` (storage + API), `.4` (nav organization), `.5` (shell
preferences), `.6` (command-center preferences), `.7` (live sync), `.8` (browser-storage
removal guard), `.9` (two-browser verification and developer docs)

This document is the contract those tasks implement against. It fixes the model, the
ownership rules, the storage shape, the revision semantics, the API, the event, and the
dashboard-side consumption rules precisely enough that the backend and dashboard tasks can
proceed in parallel. Where a downstream task has latitude, the section says so explicitly;
everything else is normative.

## 1. Principles

1. **The server is authoritative from first use.** A dashboard renders a namespace's
   *default* until the server answers, then renders the server document. There is no
   browser-side copy that outlives the page, no import of existing `localStorage`
   values, no reconciliation between a browser value and a server value, and no
   fallback path when the server is unavailable (the dashboard shows the default and an
   "unavailable" state; it never invents a durable value).
2. **One storage shape, many namespaces.** A namespace is a row in a registry plus a
   Pydantic model. Adding one never touches `tables.py`, the migration chain, the
   command set, the routes, or the event type. Only the registry, the models module
   and the generated clients change.
3. **Ownership is server-derived.** No request names the owner of a per-user document.
   The owner comes from the request principal; a caller cannot read or write another
   principal's documents by naming them.
4. **Revisions are per document, monotonic, and total.** Every accepted change
   increments the document's revision by one under a row lock; there is no revision
   reuse, including across resets. That single rule is what makes concurrent dashboards
   deterministic (§6).
5. **Events carry identity, never values.** A change event says *which* document
   changed and its new revision. Readers fetch the value through the authorized read
   path. This is what keeps per-user and per-project state from leaking over the shared
   WebSocket, which broadcasts most families to every connection today
   (`src/api/websocket.py:156-159`).
6. **Device-local transport state stays out.** The WebSocket replay cursor and epoch and
   the dashboard's own session/terminal identity are not documents, not namespaces and
   not events (§11). Everything the inventory classifies as ephemeral or URL state is
   likewise out of scope.

## 2. Model

### 2.1 Document

A **document** is the unit of storage, revision, authorization and eventing:

| Field | Type | Meaning |
|---|---|---|
| `scope` | `"workspace" \| "user"` | Shared installation state, or one principal's roaming preferences |
| `owner_id` | `str` | Server-derived. `""` for `workspace`; the human principal id for `user` (§3) |
| `namespace` | `str` | Registry key (§4), e.g. `nav_organization` |
| `subject` | `str \| null` | Namespace-declared sub-key. `null` for global namespaces; a project id for project-keyed namespaces |
| `revision` | `int ≥ 0` | `0` = never written. Increments by exactly one per accepted write or reset |
| `exists` | `bool` | `false` when the document has never been written or was last reset |
| `value` | namespace model | The typed value. Equals the namespace default whenever `exists` is `false` |
| `updated_at` | `float \| null` | Epoch seconds of the last accepted change; `null` when `revision == 0` |

The **address** of a document, and the only thing a client ever sends to name one, is
`(namespace, subject)`. `scope` is a property of the namespace, and `owner_id` is a
property of the caller. Two dashboards of the same principal address the same per-user
documents; two different principals never do.

### 2.2 Scope semantics

| Scope | Who reads | Who writes | Who is notified |
|---|---|---|---|
| `workspace` | every human principal | every human principal | every human connection |
| `user` | the owning principal only | the owning principal only | the owning principal's connections only |

"Human principal" is defined in §3. Agents, playbook steps and daemon services are not
human principals and get nothing from this API.

### 2.3 Value rules common to every namespace

- A value is a JSON object validated by the namespace's Pydantic model with
  `extra="forbid"`. Unknown keys are a validation error, not silently dropped: a
  dashboard newer than the daemon must not write fields the daemon will lose.
- Serialized size limit: **64 KiB** (`json.dumps` with compact separators). Larger
  values are refused with `value_too_large` (§7.3). Each namespace additionally bounds
  its collections (§4) so the limit is a backstop, not the working constraint.
- Identifiers that reference other records (project ids, task ids, pane view ids, node
  ids) are **advisory** inside a value: the server does not check that they exist,
  does not prune them, and does not update them when the referenced record is
  deleted. The dashboard reconciles at render time exactly as `navTree` does today
  (`dashboard/src/shell/navOrganization.ts`), and prunes on its next write. The
  exception is a project-keyed `subject`, which the server does validate (§4.3).
- Defaults are code constants in the namespace model. A reset (§8) stores no value, so
  a later change of the default in code is visible to every reset document without a
  data migration.

## 3. Ownership and principal resolution

### 3.1 The human principal today

The dashboard is unauthenticated. `dashboard/src/api/client.ts` sends no credentials,
`TokenAuthMiddleware` assigns `LOCAL_SCOPE` to a request with no bearer token
(`src/api/middleware.py:57-95`), and `CommandHandler._principal_from_scope` turns that
into `TRUSTED_LOCAL`, an `ExecutionPrincipal` of kind `LOCAL`
(`src/commands/handler.py:738-800`). There is no users table, no login and no
per-request user id anywhere in the daemon. The loopback CLI is the same principal.

This contract therefore defines exactly one human identity for now, using the literal the
escalation path already records for dashboard actions
(`src/commands/escalation_commands.py:296-299`):

```
PrincipalKind.LOCAL  →  human_id = "human:local-operator"
```

`user`-scope documents of every dashboard and every `aq` CLI invocation on this
installation belong to that one owner. That is the correct behaviour for the product as
it exists: "per-user" means "follows the operator to every browser and machine that
reaches this daemon", which is the epic's requirement. It also keeps the distinction real
rather than nominal: the storage key, the authorization check, the event filter and the
reset semantics all branch on scope, so a second human identity is a change to one
function, not to the contract.

### 3.2 Resolution function

The backend implements one pure function and calls it at the top of every
dashboard-state command:

```python
def resolve_human_id(principal: ExecutionPrincipal) -> str | None:
    """The owner id for user-scope documents, or None when the caller is not human."""
    if principal.kind is PrincipalKind.LOCAL:
        return "human:local-operator"
    return None
```

Rules:

- `None` → the command is refused with `human_required` (§7.3), for **both** scopes.
  Session tokens (agents, pool workers, supervisors — elevated or not), playbook steps
  and daemon services never read or write dashboard state. The four commands are also
  deliberately absent from `AGENT_COMMAND_SET` (`src/api/scope.py`), so a session token
  is refused at the middleware with `out of scope` before the command runs; the
  in-command check is defence in depth for the elevated-supervisor path that bypasses
  that set.
- No argument, header, body field or query parameter can name an owner. The request
  models in §7 have no `owner_id`, `user_id`, `human_id` or `principal` field, and the
  handler ignores any such key the way `_reject_authority_args` does for escalations
  (`src/commands/escalation_commands.py`). A future auth layer that derives a real human
  identity changes only `resolve_human_id`.
- `owner_id` is returned in every response (§7.2) so a dashboard can label state and
  match events, but it is output only.

### 3.3 Authorization matrix

| Caller | `workspace` read | `workspace` write | `user` read | `user` write |
|---|---|---|---|---|
| `LOCAL` (dashboard, CLI) | yes | yes | own only | own only |
| session token, non-elevated | 403 `out of scope` (middleware) | same | same | same |
| session token, elevated | 403 `human_required` | same | same | same |
| playbook step / service | 403 `human_required` | same | same | same |

"Own only" is not a check the caller can fail: the owner is derived, so a `user`-scope
address always resolves to the caller's own document. There is no admin read of another
owner's documents in this contract.

## 4. Namespace registry

### 4.1 Registry shape

`src/dashboard_state/namespaces.py` owns one frozen registry:

```python
@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    name: str                       # address key, snake_case
    scope: Literal["workspace", "user"]
    subject: Literal["none", "project"]
    write_mode: Literal["cas", "lww"]  # §6.2
    model: type[BaseModel]           # extra="forbid"; carries the default
    inventory_name: str              # provenance, documentation only

NAMESPACES: Mapping[str, NamespaceSpec]
```

The registry is the single source for validation, defaults, the `namespace` enum in the
API models, the CLI help and the event schema. A test (`tests/test_dashboard_state.py`)
asserts every registry entry has a model with `extra="forbid"`, a default that validates,
and an entry in the discriminated unions of §7.2.

### 4.2 The namespaces

| `namespace` | scope | subject | write mode | inventory name |
|---|---|---|---|---|
| `nav_organization` | workspace | none | cas | `workspace.nav_organization` |
| `shell_preferences` | user | none | lww | `user.shell_preferences` |
| `command_center_preferences` | user | none | lww | `user.command_center_preferences` (global part) |
| `command_center_project_view` | user | project | cas | `user.command_center_preferences.projects[project_id]` |
| `playbook_graph_view` | user | none | cas | `user.command_center_preferences.views.playbooks` |

Two deliberate departures from the inventory's nesting, both for the same reason: a
document is the unit of concurrency and eventing, so state that is edited independently
and scoped independently gets its own document rather than a key inside a larger one.

- The per-project command-center state is a **project-keyed namespace**, not a
  `projects{}` map inside one document. Two dashboards working in different projects
  never conflict, an event names the project it concerns, and a project's document can
  be reset or reaped alone.
- The playbook-graph positions are their own namespace instead of the `__playbooks__`
  sentinel scope used in `layout-v2/manualPositions.ts`, which collides with any project
  whose id is that string.

### 4.3 Subject rules

- `subject: "none"` — the request's `subject` must be absent or `null`; anything else is
  `subject_not_allowed`.
- `subject: "project"` — the request's `subject` must be a non-empty string naming a
  row in `projects` (archived or not); absent is `subject_required`, unknown is
  `unknown_subject`. Validated on read, write and reset. Rows whose subject no longer
  names a project are unreachable through the API; `delete_project` deletes them in the
  same transaction as the project row (no event), and `aq doctor --check
  dashboard_state.orphans --fix` reaps any left behind.

### 4.4 Value schemas

Types are given as the Pydantic model (server) and the generated TypeScript shape
(client, produced from the response models — §7.4). Defaults are the values a document
reports while `exists` is `false`. Bounds are enforced by the model; exceeding one is
`invalid_value` with the offending path.

#### `nav_organization` (workspace, global, cas)

```python
class NavFolder(BaseModel):
    id: str            # 1–64 chars, client-generated (`f-<uuid4>`); unique in the list
    name: str          # 1–120 chars after strip; never blank
    collapsed: bool = False

class NavOrganization(BaseModel):
    folders: list[NavFolder] = []                 # rail order; ≤ 200
    assignments: dict[str, str] = {}              # project_id → folder id; ≤ 2,000 keys
    project_order: list[str] = []                 # one global ranking; ≤ 2,000, no duplicates
```

Default: `{"folders": [], "assignments": {}, "project_order": []}`.

Validation beyond shape: every `assignments` value must name an id in `folders`
(`invalid_value` at `assignments.<project_id>`); `project_order` must contain no
duplicates. Folder order is list order (the inventory's `folder_order` is not a separate
field). Project ids are advisory (§2.3): an id that is no longer a project stays in the
document until a client write drops it, exactly as today.

#### `shell_preferences` (user, global, lww)

```python
class RightSurfacePane(BaseModel):
    view: str                         # a registered pane id (dashboard/src/panes/registry.ts)
    args: dict[str, Any] = {}         # the manifest-validated args, as stored by the pane store

class RightSurface(BaseModel):
    width: int = 480                  # 280–800
    kind: Literal["pane", "drawer"] | None = None
    activity_tab: Literal["gates", "events"] = "gates"
    pane: RightSurfacePane | None = None

class ShellPreferences(BaseModel):
    theme: Literal["dark", "light", "system"] = "dark"
    pane_widths: dict[str, int] = {}  # pane view id → width 200–800; ≤ 64 keys
    right_surface: RightSurface = RightSurface()
    projects_section_open: bool = True
    agent_flock_collapsed: bool = False
    last_project_id: str | None = None
```

Default: the model's field defaults. `right_surface.pane.view` and `pane_widths` keys are
advisory strings: the server does not know the pane registry. The dashboard validates
`view`/`args` against the manifest when it *restores* the pane, not the server; an
unknown view or failing `args_schema` on restore renders the closed surface and is
corrected by the next write. `last_project_id` is likewise advisory: the redirect uses it
only when the project still exists, otherwise picks the first project and writes the
repaired value.

#### `command_center_preferences` (user, global, lww)

```python
class CommandCenterPreferences(BaseModel):
    density: Literal["compact", "comfortable", "spacious"] = "comfortable"
```

Default: `{"density": "comfortable"}`. Small on purpose: this is the home for every
future global command-center preference, so adding one is a field, not a namespace.

#### `command_center_project_view` (user, project-keyed, cas)

```python
class ManualPosition(BaseModel):
    x: float          # finite; one decimal, world coordinates (density-independent, offset-corrected)
    y: float

class CommandCenterProjectView(BaseModel):
    expanded_task_ids: list[str] = []           # ≤ 5,000; no duplicates
    expanded_finished_task_ids: list[str] = []  # subset of expanded_task_ids; ≤ 5,000
    manual_positions: dict[str, ManualPosition] = {}   # task id → position; ≤ 5,000
```

Default: all empty. The subset invariant that `useGraphHierarchy.ts` maintains today is a
server validation (`invalid_value` at `expanded_finished_task_ids`), so no client can
persist a finished-expanded id that is not expanded. Filter-forced expansion
(`forced_expansion_for` on the server layout path) is derived state and is never written
here.

Expansion moves from one global set to a per-project document. Task ids are unique across
projects, so nothing is lost; what changes is that opening project B never reads or
rewrites project A's document.

#### `playbook_graph_view` (user, global, cas)

```python
class PlaybookGraphView(BaseModel):
    manual_positions: dict[str, ManualPosition] = {}   # playbook node id → position; ≤ 2,000
```

Default: empty.

### 4.5 Adding a namespace

1. Add the value model to `src/api/models/dashboard.py` and a `NamespaceSpec` to
   `NAMESPACES`.
2. Add it to the `namespace` `Literal` and to the two discriminated unions in §7.2 (a
   test fails if the registry and the unions disagree).
3. Regenerate `openapi.json` and both clients (`scripts/regenerate-api-client.sh
   --offline`, `scripts/regenerate-ts-client.sh --from-file`).
4. Add a row to the inventory's matrix with the classification that justifies the scope.

No table, migration, command, route, event type or WebSocket change is involved.

## 5. Storage

### 5.1 Table

`src/database/tables.py`, house style (`sa.JSON`, float epochs, named constraints):

```python
dashboard_state_documents = Table(
    "dashboard_state_documents",
    metadata,
    Column("scope", Text, nullable=False, primary_key=True),
    Column("owner_id", Text, nullable=False, primary_key=True),   # "" for workspace scope
    Column("namespace", Text, nullable=False, primary_key=True),
    Column("subject", Text, nullable=False, primary_key=True),    # "" for global namespaces
    Column("revision", Integer, nullable=False, server_default="0"),
    Column("value", JSON, nullable=True),                         # NULL = reset / default
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("scope IN ('workspace','user')", name="ck_dashboard_state_scope"),
    CheckConstraint(
        "(scope = 'workspace' AND owner_id = '') OR (scope = 'user' AND owner_id <> '')",
        name="ck_dashboard_state_owner",
    ),
    CheckConstraint("revision >= 1", name="ck_dashboard_state_revision"),
)
```

Notes:

- The composite natural key is the document address plus its owner. `subject` is stored
  as `""` rather than `NULL` so it can participate in the primary key; the API maps it
  back to `null`.
- A row exists only once a document has been written at least once, so a stored
  `revision` is always `≥ 1`; revision `0` is the API's name for "no row".
- `value IS NULL` is the reset state. `exists` in the API is `value IS NOT NULL`.
- The namespace is **not** a database check constraint. The registry validates it, and
  a check would make every new namespace a migration, which §1 forbids.
- Migration `a0000000000e_dashboard_state_documents.py`, `down_revision =
  "a0000000000d"`, inspector-guarded `metadata.tables[...].create(bind,
  checkfirst=True)` in the pattern of `a0000000000b_escalation_actions.py`, because the
  squashed baseline already creates the table on a fresh database. The downgrade drops
  the table: this is UI preference data, and dropping it is the roll-forward posture the
  epic asks for.

### 5.2 Write algorithm

One statement, in `src/database/queries/dashboard_state_queries.py`
(`DashboardStateQueriesMixin`, composed into `PostgreSQLDatabaseAdapter`):

```python
async def write_dashboard_document(
    self, *, scope, owner_id, namespace, subject, value, base_revision, now
) -> tuple[dict | None, dict | None]:
    """Insert or replace one document.

    value=None is a reset. base_revision=None is unconditional.
    Returns (new_row, None) on success or (None, current_row_or_None) on a
    revision conflict — never raises for a lost race.
    """
    stmt = (
        pg_insert(t)
        .values(scope=..., owner_id=..., namespace=..., subject=..., revision=1,
                value=value, created_at=now, updated_at=now)
        .on_conflict_do_update(
            index_elements=["scope", "owner_id", "namespace", "subject"],
            set_={"revision": t.c.revision + 1, "value": value, "updated_at": now},
            where=(literal(base_revision).is_(None) | (t.c.revision == base_revision)),
        )
        .returning(t)
    )
```

- Absent row + `base_revision in (None, 0)` → insert, revision `1`.
- Absent row + `base_revision ≥ 1` → the insert would succeed with revision `1`, which
  is wrong; the query layer therefore runs the insert only when `base_revision` is
  `None` or `0`, and otherwise runs a plain guarded `UPDATE … WHERE revision =
  base_revision RETURNING`. No row returned → conflict.
- Present row → the `ON CONFLICT DO UPDATE … WHERE` guard is evaluated under the row
  lock PostgreSQL takes for the conflict, so two concurrent writers are serialized and
  exactly one of two CAS writers with the same base wins.
- No row returned → the caller reads the current row (may be absent → revision `0`)
  and reports a conflict. Nothing was written.

Every write, reset and read of a `user` document filters by the derived `owner_id`; the
query layer takes it as a parameter and never defaults it.

## 6. Revision and last-write-wins semantics

### 6.1 Rules

- **R1 — origin.** A document that has never been written has revision `0`, `exists =
  false`, and the namespace default as its value.
- **R2 — monotonic per document.** Every accepted write or reset increments the
  document's revision by exactly one under the row lock of §5.2. Revisions of one
  document are a total order equal to commit order. Revisions of different documents
  are unrelated.
- **R3 — value.** A document's value is the value of its highest-revision accepted
  change. A reset's value is the namespace default.
- **R4 — guarded write.** A write carrying `base_revision = r` is accepted iff the
  document's current revision is exactly `r` (with `0` meaning "never written").
  Otherwise it is refused with `revision_conflict` carrying the current document, and
  nothing changes.
- **R5 — unconditional write.** A write with no `base_revision` is accepted
  unconditionally and takes the next revision. This is last-write-wins: "last" is
  commit order on the server, never a client clock, never a client-supplied revision.
- **R6 — client adoption.** A client keeps `(revision, value)` per document and
  replaces it only on receiving a document with a **strictly greater** revision — from a
  read, a write response, a conflict's `current`, or an event-triggered refetch. Equal or
  lower is discarded. Together with R2 this makes every dashboard converge on the same
  `(revision, value)` once writes stop, regardless of event ordering, duplication or
  loss.
- **R7 — reset is a revision.** Reset increments the revision like a write (§8), so a
  client holding revision `n` sees the reset as `n+1` and adopts the default under R6.
  Revisions never restart at `0`.

### 6.2 Per-namespace write mode

The registry's `write_mode` says what the **dashboard** must send; the server enforces
it:

- `cas` — `base_revision` is required. Omitting it is refused with
  `base_revision_required`. Used where the value is a collection edited by operations
  (add a folder, toggle an id, move a node) and two dashboards of the same owner could
  plausibly interleave edits: `nav_organization`, `command_center_project_view`,
  `playbook_graph_view`. A lost race is a `revision_conflict`, and the client
  re-applies its operation to the fresh value (§10.4); nothing is silently overwritten.
- `lww` — `base_revision` is optional. Omitted means R5. Used for scalar preference
  documents where the operator's most recent action is the intended outcome and a
  rebase would only re-apply the same setter: `shell_preferences`,
  `command_center_preferences`. A client may still send `base_revision` on an `lww`
  namespace and gets R4.

`cas` therefore requires the dashboard to have loaded a document before its first write
to it; `lww` does not. A `cas` namespace whose document was never loaded is written with
`base_revision: 0` only when the client is certain the document does not exist, which in
practice means never — load first.

### 6.3 Worked concurrency examples

Two dashboards, A and B, of the same owner. Document starts at revision 3.

*Both send a guarded write with `base_revision: 3`.* One is committed first and becomes
revision 4. The other is refused with `current = {revision: 4, …}`. It re-applies its
operation over revision 4 and writes with `base_revision: 4`, producing 5. Both end at 5.

*A writes unconditionally (`lww`) while B's guarded write is in flight.* Commit order
decides: if A lands first, A is 4 and B's `base_revision: 3` is refused; if B lands first,
B is 4 and A becomes 5 unconditionally. Either way every client that receives a document
adopts the highest revision it has seen.

*B receives the events for revisions 5 and 4 in that order, then a duplicate of 5.* B
refetches on the first event and adopts 5. The event for 4 is below its held revision and
is ignored. The duplicate is ignored. (Events do not carry values, so B refetches at most
once per new revision it learns of, and coalesces refetches per document — §10.5.)

*The daemon restarts and B reconnects with a stale replay cursor.* The epoch mismatch
clears B's cursor (`dashboard/src/ws/useEventStream.ts`). On reconnect B refetches the
bootstrap list (§10.5); every document is adopted only if its revision is higher.

## 7. API

### 7.1 Commands and routes

Four commands in `src/commands/dashboard_state_commands.py`
(`DashboardStateCommandsMixin`, added to the `CommandHandler` bases), category
`dashboard` (`CategoryMeta` in `src/tools/registry.py`, `_TOOL_CATEGORIES` and
`_ALL_TOOL_DEFINITIONS` in `src/tools/definitions.py`, CLI group `dashboard` in
`src/cli/auto_commands.py`). They flow through the codegen router, so the operation ids
are the command names:

| Command | Route | Generated TS | Purpose |
|---|---|---|---|
| `dashboard_state_list` | `POST /api/dashboard/state-list` | `dashboardStateList` | Bootstrap: every document visible to the caller |
| `dashboard_state_get` | `POST /api/dashboard/state-get` | `dashboardStateGet` | One document by address |
| `dashboard_state_put` | `POST /api/dashboard/state-put` | `dashboardStatePut` | Replace one document's value |
| `dashboard_state_reset` | `POST /api/dashboard/state-reset` | `dashboardStateReset` | Reset one document to its default |

CLI mirrors: `aq dashboard state-list`, `aq dashboard state-get --namespace … [--subject …]`,
`aq dashboard state-put --namespace … --value '<json>' [--base-revision N]`,
`aq dashboard state-reset --namespace … [--subject …]`. The CLI is the local operator,
so it addresses the same documents the dashboard does.

Every command: resolves the human id (§3.2) first; looks the namespace up in the
registry; validates the subject (§4.3); then acts. There are no other commands: no
bulk write, no import, no "list owners", no admin read of another owner.

### 7.2 Request and response models

`src/api/models/dashboard.py`, wired into `get_all_response_models()`. Every response
starts with `success: bool`.

```python
Namespace = Literal[
    "nav_organization", "shell_preferences", "command_center_preferences",
    "command_center_project_view", "playbook_graph_view",
]

class DocumentBase(BaseModel):
    scope: Literal["workspace", "user"]
    owner_id: str            # "" for workspace scope
    subject: str | None
    revision: int
    exists: bool
    updated_at: float | None

class NavOrganizationDocument(DocumentBase):
    namespace: Literal["nav_organization"]
    value: NavOrganization
# … one *Document class per namespace, each pinning `namespace` to its literal …

Document = Annotated[
    NavOrganizationDocument | ShellPreferencesDocument | CommandCenterPreferencesDocument
    | CommandCenterProjectViewDocument | PlaybookGraphViewDocument,
    Field(discriminator="namespace"),
]

class DashboardStateListResponse(BaseModel):
    success: bool
    owner_id: str
    documents: list[Document]

class DashboardStateDocumentResponse(BaseModel):   # get, put, reset
    success: bool
    document: Document
```

`dashboard_state_list` returns every `workspace` document that has a row, plus every
`user` document owned by the caller that has a row, plus a **synthesized revision-0
document for every global namespace that has no row**, so the bootstrap always contains
all five global namespaces and the client never special-cases absence. Project-keyed
documents appear only when they have a row.

Request bodies:

```python
class DashboardStateGetRequest(BaseModel):
    namespace: Namespace
    subject: str | None = None

class DashboardStateResetRequest(DashboardStateGetRequest): ...

class NavOrganizationPut(BaseModel):
    namespace: Literal["nav_organization"]
    subject: str | None = None
    base_revision: int | None = None
    value: NavOrganization
# … one *Put class per namespace …

DashboardStatePutRequest = Annotated[
    NavOrganizationPut | ShellPreferencesPut | …, Field(discriminator="namespace")
]
```

The codegen input-model generator flattens nested schemas to `dict`
(`src/api/codegen.py:85-135`), which would leave `value` untyped in the generated
clients. The backend task therefore adds a small, general escape hatch: a
`REQUEST_MODELS: dict[str, type]` merged the same way as `RESPONSE_MODELS`, which
`build_category_routers` uses in preference to `_make_input_model` when present. If the
discriminated-union body proves awkward for either generator, the accepted fallback is a
flat `{namespace, subject, base_revision, value: object}` request with server-side
validation and typed *responses* only; the dashboard wrapper (§10.2) then narrows `value`
from the response types. Either way `openapi.json` and both clients are regenerated in
the same commit.

### 7.3 Errors

Dashboard-state commands return the coded envelope the escalation and digest commands
use (`{"success": false, "error_code": …, "error": …, …}`), and are added to the
codegen pass-through set so the body reaches the client intact (`src/api/codegen.py`,
the allowlist around `digest_status`). `revision_conflict` is returned as **409** and
`human_required` as **403**; the backend task generalizes the hardcoded
`edit_intelligence_class` 409 branch into a `(command, error_code) → status` lookup so the
models below are what `openapi.json` advertises for those statuses.

| `error_code` | HTTP | When | Extra fields |
|---|---|---|---|
| `human_required` | 403 | caller is not a human principal (§3.2) | — |
| `unknown_namespace` | 422 | `namespace` not in the registry | — |
| `subject_required` | 422 | project-keyed namespace, `subject` missing | — |
| `subject_not_allowed` | 422 | global namespace, `subject` present | — |
| `unknown_subject` | 422 | project-keyed namespace, no such project | — |
| `base_revision_required` | 422 | `cas` namespace, `base_revision` missing | — |
| `invalid_value` | 422 | model validation failed | `errors: [{path: str, message: str}]` |
| `value_too_large` | 422 | serialized value over 64 KiB | `limit_bytes`, `actual_bytes` |
| `revision_conflict` | 409 | R4 failed | `current: Document` (revision 0 + default when no row) |

```python
class DashboardStateErrorResponse(BaseModel):
    model_config = {"extra": "allow"}
    success: bool = False
    error_code: str
    error: str

class DashboardStateConflictResponse(BaseModel):
    success: bool = False
    error_code: Literal["revision_conflict"]
    error: str
    current: Document
```

A session-token caller never reaches these: the middleware answers `403 {"error": "out
of scope: dashboard_state_put"}` first.

### 7.4 Generated clients

Both generated clients (`packages/aq-client`, `packages/aq-ts-client`) gain the four
operations and the `Document`/value types with no hand edits. The dashboard imports
value types from the generated package through `dashboard/src/api/client.ts` (for
example `NavOrganization`, `ShellPreferences`) and never re-declares them; the
hand-written modules in `dashboard/src/shell/navOrganization.ts` and friends switch their
local interfaces to type aliases of the generated ones.

## 8. Reset and deletion

- **Reset** (`dashboard_state_reset`) sets `value = NULL`, increments `revision`, and
  emits a change event with `change: "reset"`. The response is the document with
  `exists: false`, the namespace default as `value`, and the new revision. Reset has no
  `base_revision`: it is always unconditional, because its outcome does not depend on
  the prior value. Resetting a document that has no row creates the row at revision 1
  with `NULL` value — the same observable result as a reset of a written document, and
  it lets a client that reset before loading still hold a real revision.
- **There is no user-facing delete.** A document address is permanent; "delete" means
  reset. This keeps R7 true: a row is never removed while a client could hold its
  revision.
- **Reaping** is the only physical delete and is never user-initiated:
  `delete_project` removes that project's `command_center_project_view` rows in its own
  transaction, and the doctor check in §4.3 removes orphans. Neither emits a
  `dashboard_state` event; the project's own deletion event is what makes clients drop
  that project's view.

## 9. Events

### 9.1 Type and payload

One event type, `dashboard_state.changed.v1`, registered in `src/event_schemas.py`
(the `aq doctor` `events.registry` check refuses unregistered types):

```python
"dashboard_state.changed.v1": {
    "required": ["version", "scope", "owner_id", "namespace", "subject",
                 "revision", "change", "updated_at"],
    "optional": ["seq"],
}
```

| Field | Value |
|---|---|
| `version` | `1` |
| `scope` | `"workspace"` or `"user"` |
| `owner_id` | `""` for workspace; the human id for user scope |
| `namespace` | registry key |
| `subject` | project id or `null` |
| `revision` | the document's new revision |
| `change` | `"write"` or `"reset"` |
| `updated_at` | epoch seconds |
| `seq` | the `events` row id, for WebSocket replay dedup |

The payload carries **no value** (§1, item 5). A client that wants the value performs an
authorized read; its own write response already contains it.

### 9.2 Persistence and ordering

Persist-then-emit, the `task_commands.py` pattern: the command calls `db.log_event`
after the write commits and threads the returned id into `bus.emit` as `seq`, so the
event replays after a reconnect and the live/replay handoff dedups it. Events for one
document are emitted in commit order by a single daemon process, but a client must not
depend on that: R6 makes any order and any duplication safe.

### 9.3 WebSocket forwarding

`src/api/websocket.py` adds the `dashboard_state.` prefix to `_FORWARDED_PREFIXES` with
a per-connection filter alongside the existing `pool.` and `metrics.` ones:

- forwarded only to connections whose scope resolves to a human id (today:
  `scope.kind == "local"`);
- for `scope == "user"`, additionally only when the event's `owner_id` equals the
  connection's human id;
- never to session-token connections, elevated or not.

Because the payload has no value, a filtering mistake could leak that *something*
changed and its revision, never the content. The filter is still required so a future
second human identity does not receive another operator's change stream.

### 9.4 Client rules

- Match on the exact type `dashboard_state.changed.v1` (the client's discriminator
  normalization already exposes it as `event_type`).
- Ignore events whose `owner_id` is neither `""` nor the client's own `owner_id` from
  the bootstrap response.
- If `revision` is greater than the held revision for that address, mark the document
  stale and refetch it (coalesced per address, §10.5). Otherwise ignore.
- On the connection status transition to `connected` (initial or reconnect), refetch
  the bootstrap list. Live-only gaps, queue overflow drops and epoch resets all resolve
  through this one path.

## 10. Dashboard consumption contract

This section binds `amber-stone.4`–`.7`. It describes interfaces, not component
internals.

### 10.1 Store

One provider, `DashboardStateProvider`, mounted inside the app's `QueryClientProvider`
and above the shell. It owns:

- the bootstrap query, key `["dashboard-state"]`, fetched once on mount and refetched on
  every WebSocket `connected` transition;
- per-document entries in the query cache, key `["dashboard-state", namespace, subject
  ?? ""]`, seeded from the bootstrap response and updated under R6 only;
- `owner_id` from the bootstrap response, exposed for event matching.

The existing `aq:project-organization-changed` and `aq:command-center-graph-positions-
changed` window events and the `storage` listener are removed with their localStorage
writers; the query cache is the only in-page fan-out.

### 10.2 Hook

```ts
function useDashboardDocument<N extends Namespace>(
  namespace: N, subject?: string,
): {
  status: "loading" | "ready" | "unavailable";
  value: ValueOf<N>;          // the default while loading or unavailable
  revision: number;           // 0 until ready
  exists: boolean;
  write(next: ValueOf<N>): Promise<void>;   // full replacement; §10.3–10.4
  update(op: (current: ValueOf<N>) => ValueOf<N>): Promise<void>;  // cas namespaces
  reset(): Promise<void>;
}
```

`ValueOf<N>` is the generated value type for the namespace. Callers never touch
revisions; the hook carries them.

### 10.3 Loading and unavailable states

- `loading`: the bootstrap has not answered. Render the default. Writes on every
  namespace are **deferred** until the document is ready, then applied as `update`
  against the loaded value. (As implemented by `amber-stone.5`: an unconditional `lww`
  write sent before the load would carry the defaults of every field it did not change
  and overwrite the stored document with them — the first navigation writes
  `last_project_id` before the bootstrap answers.) Nothing is persisted in the browser
  meanwhile.
- `unavailable`: the bootstrap failed (network, 5xx). Render the default, surface the
  condition in the shell's existing connection indicator, retry with the query's
  backoff. Writes are refused with an error to the caller; they are not queued, because
  a queued write replayed later would be exactly the silent divergence the epic rules
  out.
- `ready`: normal operation.

### 10.4 Write policy

- `lww` namespaces (`shell_preferences`, `command_center_preferences`): optimistic. The
  hook shows the new value immediately and sends it the same way as a `cas` `update`:
  the operation is applied to the loaded value and sent with its `base_revision`, and a
  `revision_conflict` re-applies it to `current`. Consumers pass field patches, so a
  change to one field on one dashboard never overwrites a different field changed on
  another (§6.2 permits `base_revision` on `lww` namespaces). `write(next)` is
  `update(() => next)`. High-frequency sources (pane resize, right-surface resize)
  debounce to one write per settled gesture; the value shown during the gesture is
  component state. On error the optimistic value is dropped and the document refetched:
  the confirmed server document is shown, never a client-side snapshot.
- `cas` namespaces (`nav_organization`, `command_center_project_view`,
  `playbook_graph_view`): `update(op)` applies `op` to the cached value optimistically,
  sends `put` with the cached revision as `base_revision`, and on `revision_conflict`
  adopts `current`, re-applies `op` to it, and retries with the new base. The operations
  the dashboard already has are pure functions over the value (`createFolder`,
  `moveProject`, `toggleExpandedId`, `saveGraphPosition`, …), which is what makes the
  rebase deterministic. After **three** consecutive conflicts the hook adopts the server
  document, drops the pending operation, and reports the failure to the caller; the UI
  shows the server state, never a stuck optimistic one. A write is refused locally with
  a clear error while the document is `unavailable`.
- Writes to one address are serialized in the hook: a second `update` queues behind the
  in-flight one and rebases onto its result, so a burst of toggles produces a chain of
  revisions rather than a fan of conflicts.

### 10.5 Event handling

`useEventStream.ts` gains one branch: for `dashboard_state.changed.v1` matching the
client's owner (§9.4), when `revision > cached.revision`, invalidate
`["dashboard-state", namespace, subject ?? ""]`. Refetches are coalesced per address in a
short window (the 400 ms playbook debounce is the precedent). The bootstrap refetch on
`connected` is the gap-recovery path; nothing else is needed for missed, duplicate, or
out-of-order events.

### 10.6 Module ownership after migration

| Namespace | Owning module(s) | Replaces |
|---|---|---|
| `nav_organization` | `dashboard/src/shell/navOrganization.ts`, `useNavOrganization.ts`, `ProjectTree.tsx` | `aq.shell.project-organization` |
| `shell_preferences` | `panes/store.tsx` (pane widths, restored pane), `shell/useRightSurface.tsx`, `shell/AgentFlock.tsx`, `shell/LeftRail.tsx`, `App.tsx` (last project) | `aq:shellpane:width:*`, `aq:rightsurface:width`, `aq:flock:collapsed`, `aq.dashboard.lastProjectId`, and the in-memory kind/tab/pane/projectsOpen |
| `command_center_preferences` | `layout-v2/density.ts`, `LayoutCanvas.tsx` | `aq.command-center.graph-density` |
| `command_center_project_view` | `useGraphHierarchy.ts`, `layout-v2/manualPositions.ts`, `LayoutCanvas.tsx`, `api/graphLayout.ts` (tidy clears positions via `update`) | `aq:command-center:expanded-task-ids:v1`, `aq:command-center:expanded-finished-task-ids:v1`, project scopes of `aq.command-center.graph-positions` |
| `playbook_graph_view` | `layout-v2/manualPositions.ts`, playbook canvases | the `__playbooks__` scope of `aq.command-center.graph-positions` |

The write path is `dashboard/src/api/dashboardStateStore.ts`
(`useDashboardDocumentState`, the §10.2 hook), owned by the single
`DashboardStateProvider` (`api/DashboardStateProvider.tsx`) that also runs the bootstrap;
`shell/useShellPreferences.ts` is the `shell_preferences` wrapper, and
`testUtils/dashboardState.tsx` is an in-memory server for component tests.
Within `shell_preferences`, widths, the Projects disclosure, the flock collapse, the theme
(published as `data-theme` on the root element) and `last_project_id` render the
server's value. `right_surface.kind`, `.activity_tab` and `.pane` are **restore-on-load**:
the surface open right now is page state, written to the server on every change and
restored once when the document first becomes ready — unless a shortcut, a URL command or
an agent push already chose one — so opening a pane on one machine never pops it open on
another. The palette's "Reset shell preferences" action resets the document.

Each module keeps its pure operations and loses its storage functions; the storage seam
becomes `useDashboardDocument`. `useGraphHierarchy.ts` stops re-reading storage on every
render — its module-level sets and listener are replaced by the query cache entry for the
active project.

## 11. Explicit non-goals and device-local exceptions

- **No migration.** No code reads any of the nine feature-state `localStorage` keys, not
  even once, not even to seed. The keys are simply never consulted; whatever a browser
  still holds is inert and is removed by `amber-stone.8`.
- **No fallback.** When the server is unavailable the dashboard renders defaults and
  says so. It does not write to browser storage and does not read a cached value from
  it.
- **No import endpoint, no bulk endpoint, no cross-owner endpoint.**
- **No server-side pruning of advisory ids** (§2.3) beyond the size bounds.
- **Device-local transport state is untouched and stays in the browser**, exactly as the
  inventory justifies it: `aq:ws:last_seq` and `aq:ws:epoch` (replay cursor and epoch in
  `ws/useEventStream.ts`) and the read-only `aq:session:id` connection-identity stub.
  These are the only browser-persistent keys the `amber-stone.8` guard permits; each is
  listed there with this justification. None of them is a namespace, an event or a
  field of any value above. As implemented: `dashboard/src/deviceLocal.ts` is the one
  module that touches browser storage (its `DEVICE_LOCAL_KEYS` registry holds exactly
  these three keys), `dashboard/CLAUDE.md` § Browser storage documents them, and
  `tests/test_dashboard_browser_storage.py` — in the Python suite, because the vitest
  suite does not run in CI — fails on any other production storage reference, an
  undocumented registry key, or a retired feature key anywhere under `dashboard/src`.

## 12. Verification obligations by task

- **`amber-stone.3`** (`tests/test_dashboard_state.py`, `tests/test_api_client_contract.py`
  regeneration): registry/model/union agreement; every error code in §7.3 with its HTTP
  status; R1–R7 as query-level tests, including the two-writer race for both the
  present-row and absent-row cases; owner isolation (two derived owners, same address,
  distinct rows; a `user` read never returns another owner's row); `human_required` for
  session, elevated, playbook and service principals; `out of scope` for a session
  token at the route; the WebSocket filter for both scopes; the event is persisted with
  `seq`; `delete_project` reaps; the doctor check; `openapi.json` and both clients
  regenerated.
- **`amber-stone.4`, `.5`, `.6`** (dashboard unit tests): each namespace renders its
  default while loading, adopts the server document, and never reads or writes
  `localStorage`; each `cas` operation rebases correctly on a simulated 409; the `lww`
  debounce; reset restores the default at a higher revision.
- **`amber-stone.7`**: two clients converge under duplicate, reordered and dropped
  events; reconnect refetches the bootstrap; per-owner and per-project event scoping.
- **`amber-stone.9`**: two browser contexts against one daemon for every namespace;
  stale legacy keys present in one context are ignored, not imported.
