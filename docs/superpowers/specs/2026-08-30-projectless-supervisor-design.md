# Projectless global supervisor

Approved in the implementation conversation on 2026-08-30.

The global supervisor belongs to the system, not to a synthetic project.
Its session and message rows have `project_id = NULL`. Its address remains
`supervisor-global` and runtime name remains `n-supervisor--global`, preserving
existing conversation and restart identities. Its working directory remains
the system vault; project supervisors remain scoped to their real projects.

The explicit `system_only` message filter selects NULL-project records.
Omitting a project filter continues to select all records. Local callers and
explicit elevated tokens without a project may access system messages;
project-scoped and non-elevated tokens may not. Missing project membership
alone never grants administrative authority.

Migration preserves session/message IDs, history, reply links, and real project
data. It removes the legacy Global placeholder only when unused and otherwise
untouched. No frontend blacklist is needed: the synthetic row no longer exists
or gets recreated, so project lists and counts naturally exclude it.

Verification covers cold start, API history isolation, system-message permissions,
existing project behavior, migration preservation, and downgrade compatibility.

Global supervisor token-ledger rows also become projectless, preserving usage and cost history. Explicit real-project sends remain project-scoped, and system replies cannot be read or marked delivered by project credentials.
