# Documentation map and page ownership contract

This page is the contract between the tickets of the
[documentation overhaul](plans/documentation-overhaul/README.md). It answers
three questions:

1. **Where does a subject live?** — the [documentation tree](#the-documentation-tree).
2. **Who writes it?** — the [page ownership table](#page-ownership).
3. **How is "everything is covered" checked?** — the
   [module coverage manifest](#module-coverage-manifest).

If you are reading AQ documentation rather than writing it, you want
[the documentation home](README.md) instead.

## Principles

**One subject, one page.** A subject is explained in exactly one place and
linked from everywhere else. If two pages would both need to explain worktree
slots, one of them links to the other.

**Concepts, guides and reference are different genres.** A *concept* page
explains a mechanism and its vocabulary to someone who has never seen it. A
*guide* walks through a task the reader is trying to complete. A *reference*
page is exhaustive and boring on purpose. Do not mix them in one file.

**Current behaviour only.** Every page describes what the code on `main` does
now. Shipped defaults, locally configured policy, optional compatibility modes
and proposed work are four different things and must be labelled. Historical
designs live under [Historical material](#historical-material) and are never
presented as current.

**Public first, private second.** A page opens with what a user needs, then
descends into contributor internals under a clearly marked heading. A newcomer
should be able to stop reading halfway down and still have learned something
true.

**Source links, not source copies.** Reference a module path and let the reader
open it. Do not paste code that will drift.

## The documentation tree

```text
README.md                             GitHub landing page
docs/
  README.md                           documentation home and reading order
  documentation-map.md                this page
  tutorials/                          Start here → First task
    install.md
    first-task.md
  concepts/                           Core concepts
    architecture.md
    tasks.md
    scheduling.md
    agents-and-routing.md
    sessions.md
    projects-and-workspaces.md
    integration.md
    playbooks.md
    configuration-and-vault.md
    providers.md
    messaging.md
  guides/                             How-to guides
    dashboard.md
    plugins-and-mcp.md
    operations.md
    ...existing guides, reconciled by the legacy ticket
  reference/                          Reference and module catalog
    README.md                         reference index
    glossary.md
    configuration.md
    cli/                              command groups, contracts, agent tools
    api/                              REST, WebSocket, generated clients
    database/                         tables, query modules, migrations
    modules/                          the module catalog, one shard per ticket
      README.md
      <shard>.md
  contributing/                       Contributing
    README.md
    setup.md
    testing.md
    repo-map.md
    scripts.md
    ci.md
    documentation-style.md
  history/                            Historical material
    README.md                         disposition index
  specs/  superpowers/  reports/  reviews/  analysis/  plans/
                                      preserved historical material
```

Directory `README.md` files are what GitHub renders when someone clicks a
folder, so every directory listed above gets one.

## Page ownership

Each row names the overhaul ticket that **writes** the pages in it. Nobody else
edits those files while the overhaul is in flight; cross-link instead. The
machine-readable form of this table is
[`module-ownership.json`](plans/documentation-overhaul/module-ownership.json)
(`shards` key).

| Ticket | Owns these pages | Module coverage shard |
|---|---|---|
| `foundation` | `docs/README.md`, `docs/documentation-map.md`, `docs/reference/glossary.md`, `docs/contributing/documentation-style.md`, `docs/reference/modules/README.md`, the overhaul's planning files | — |
| `quickstart` | `docs/tutorials/install.md`, `docs/tutorials/first-task.md`, `docs/tutorials/README.md` | — |
| `readme` | `README.md` (repository root) | — |
| `architecture` | `docs/concepts/architecture.md` | `docs/reference/modules/architecture.md` |
| `tasks` | `docs/concepts/tasks.md` | `docs/reference/modules/tasks.md` |
| `scheduler` | `docs/concepts/scheduling.md`, `docs/guides/worker-pools.md`, `docs/guides/resource-gating.md` | `docs/reference/modules/scheduler.md` |
| `routing` | `docs/concepts/agents-and-routing.md` | `docs/reference/modules/routing.md` |
| `sessions` | `docs/concepts/sessions.md` | `docs/reference/modules/sessions.md` |
| `workspaces` | `docs/concepts/projects-and-workspaces.md`, `docs/guides/project-onboarding.md` | `docs/reference/modules/workspaces.md` |
| `integration` | `docs/concepts/integration.md`, `docs/guides/development-integration.md` | `docs/reference/modules/integration.md` |
| `playbooks` | `docs/concepts/playbooks.md` | `docs/reference/modules/playbooks.md` |
| `cli` | `docs/reference/cli/**` | `docs/reference/modules/cli.md` |
| `api` | `docs/reference/api/**` | `docs/reference/modules/api.md` |
| `dashboard` | `docs/guides/dashboard.md` | `docs/reference/modules/dashboard.md` |
| `database` | `docs/reference/database/**` | `docs/reference/modules/database.md` |
| `vault` | `docs/concepts/configuration-and-vault.md`, `docs/reference/configuration.md` | `docs/reference/modules/vault.md` |
| `providers` | `docs/concepts/providers.md` | `docs/reference/modules/providers.md` |
| `plugins` | `docs/guides/plugins-and-mcp.md` | `docs/reference/modules/plugins.md` |
| `communications` | `docs/concepts/messaging.md`, `docs/guides/escalations.md` | `docs/reference/modules/communications.md` |
| `operations` | `docs/guides/operations.md` | `docs/reference/modules/operations.md` |
| `contributing` | `docs/contributing/**` except `documentation-style.md` | `docs/reference/modules/contributing.md` |
| `reference` | `docs/reference/README.md`, generated reference indexes, the documentation checks | — |
| `legacy` | `docs/history/README.md`, dispositions and link repairs across existing `docs/` | — |
| `acceptance` | final assembly of every shared index listed below | — |

### Shared indexes

Four files are indexes over other people's work. **Foundation creates them;
`acceptance` owns them from then on.** Between those two points, a ticket that
needs a link added to one appends its own row and changes nothing else:

* `docs/README.md` — the reading order and navigation.
* `docs/reference/modules/README.md` — the module catalog index.
* `docs/reference/README.md` — the reference index (created by `reference`).
* `docs/history/README.md` — the historical index (created by `legacy`).

The root `README.md` is not a shared index: `readme` owns it outright and
`acceptance` only verifies its links.

### Writing a module coverage shard

Every ticket with a shard file in the table above writes
`docs/reference/modules/<shard>.md` and nothing else in that directory. A shard
is a table with one row per production module the ticket owns:

| Column | Content |
|---|---|
| Module | The source path, as a relative link to the file. |
| Purpose | One sentence, present tense, what it does — not what it is named. |
| Component | The concept or guide page that explains it in prose. |
| Notes | Tests that cover it, or a caveat worth carrying. |

Private helpers may share one component page with the module they support, but
each still needs its own row: "covered by the same page" is an answer, "not
mentioned anywhere" is not. Nested packages are listed as their own modules,
not folded into the parent.

Generated code (`packages/aq-client/`, `packages/aq-ts-client/`,
`openapi.json`, the reviewed playbook artefacts under
`src/prompts/reviewed_playbooks/`) is covered at *resource-family* level: one
row per family, naming the schema it is generated from and the command that
regenerates it. Never hand-edit generated files, and never document them
symbol by symbol.

## Module coverage manifest

[`module-ownership.json`](plans/documentation-overhaul/module-ownership.json)
assigns **every** tracked path in the repository to exactly one shard, one
component page and one category:

| Category | Count | Documentation obligation |
|---|---|---|
| `production` | 791 | A named row in the owning shard's catalog. |
| `generated` | 1447 | Resource-family coverage plus a regeneration command. |
| `prompt` | 49 | Explained where its behaviour is explained, as shipped content. |
| `supporting` | 80 | Purpose, inputs, side effects and invocation, in a contributing page. |
| `documentation` | 445 | A disposition recorded by the `legacy` ticket. |
| `test` | 908 | Covered as a layout and a set of markers, not file by file. |

Counts are from the manifest at its recorded `source_commit`; regenerate rather
than trusting the numbers above if the tree has moved.

Regenerate and check the manifest with:

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py          # rewrite
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check  # verify
```

`--check` fails when a tracked path matches no ownership rule, or when the
committed JSON no longer matches the tree. That failure is the answer to "did
somebody add a module nobody documents": add a rule to `RULES` in that script
naming the owning shard, then regenerate. There is deliberately no catch-all
rule — an unmatched path is an error, not a default assignment.

The `reference` ticket may absorb this script into the wider documentation
check it owns; until it does, this is the check.

## Historical material

`docs/specs/`, `docs/superpowers/`, `docs/reports/`, `docs/reviews/`,
`docs/analysis/`, `docs/plans/`, `notes/` and `reports/` are historical. They
are preserved because they are the audit trail of how AQ got here.

The `legacy` ticket gives each existing file one of five dispositions —
*current*, *update*, *redirect*, *archive*, *historical* — and records the
result in a ledger. Two things follow for everyone else:

* Do not delete a historical page to resolve a contradiction. Fix the current
  page and let the ledger mark the old one.
* Do not link a historical page as though it were instructions. Link it as
  background, with the date it describes.

Known contradictions between existing pages and current behaviour are already
recorded in
[known-inaccuracies.md](plans/documentation-overhaul/known-inaccuracies.md);
add to it rather than fixing a page outside your ownership.
