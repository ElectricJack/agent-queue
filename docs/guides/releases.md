# Prepare, deploy and recover a release

A release publishes a tested promotion commit under an immutable annotated tag;
deployment selects that tag, while database restore is a separate recovery action.

> **Draft baseline.** E5a (`vivid-stone-39.6`), checked against `origin/main`
> at `ce7aca55f` on 2026-10-07. B6 deploy selection, B7 additive-migration
> tooling and B8 database backup/restore are shipped. Preparation and complete
> notes verification are **pending vivid-stone-39.2**; hotfix/back-merge is
> **pending vivid-stone-39.3**; post-publication policy is
> **pending vivid-stone-39.4**. E5 (`vivid-stone-39.5`) finalizes these sections
> in place. This page does not authorize the project's live cutover.

## Why it exists

A version bump on `dev` is not proof that a release reached `main`. A tagged
promotion binds the deployed code to the exact commit that passed its gate.
The [promotion-flow guide](promotion-flow.md) covers configuration and approval;
[builds and releases](../contributing/releases.md) covers wheel packaging.
This page covers the operator's checkout deployment and recovery path.

## Vocabulary

- **S:** the exact commit selected by the promotion request.
- **Annotated tag:** a tag object with a message and identity, whose peeled
  commit is S; a lightweight tag is only a ref and is refused by release selection.
- **Prepare task:** an ordinary task that bumps a version and writes its notes;
  its automatic filing is **pending vivid-stone-39.2**.
- **Deployment record:** `~/.agent-queue/deploy.json`, recording the selected
  tag/branch, exact commit, prior commit and installation time.
- **Restore:** replacement of the configured PostgreSQL database from a backup.

## A realistic example

Read the update command's help without selecting a release or changing state:

```bash
aq update --help
```

```text
Usage: aq update [OPTIONS]
```

Its `--ref` option selects an annotated release tag or an explicit unreleased
branch; `--check` reports a plan without applying it. The operator procedure
below assumes a source checkout installation outside any worker slot, a clean
checkout, reachable PostgreSQL, and a versioned promotion already delivered.
These are procedure templates, not release operations executed for this draft.

### Prepare the version and notes

**pending vivid-stone-39.2:** intended `aq promote prepare --step release
(--bump minor|patch | --version V)` files an ordinary task on the repository
default branch. The task bumps the configured version source, drafts the notes,
regenerates affected files and lands through the normal train. The final CLI
flags must be reconciled against E2 before using this procedure.

**pending vivid-stone-39.2:** the daemon's
`integration_promotion_notes_input` assembles immutable task metadata over
`previous step tag..default head`. For a first release, `notes.bootstrap_sha`
is the lower bound; `null` means full history. Traversal includes every newly
reachable commit, including epic children on second-parent history, and
deduplicates `AQ-Source: task@sha` identities. It includes archived task
summaries and migration files, marks reverted sources, and excludes already
released hotfix sources plus promotion/back-merge bookkeeping.

**pending vivid-stone-39.2:** notes have Added, Fixed, Upgrade, Breaking and
Changed sections, with migration-bearing work in Upgrade and unmatched commits
in Changed. `file_template` stores per-version Markdown, for example
`src/releases/notes/{version}.md`, with `sources` and `source_digest` in
frontmatter. `changelog_heading` uses a `## [V]` heading in one changelog and
stores the digest in an HTML comment. Workers read their own task's notes input;
they do not need cross-task read grants.

Current D3 requests already read the version and notes bytes at pinned S and
require `--notes-reviewed` for nonempty notes. **pending vivid-stone-39.2:**
request admission also recomputes the source digest, refuses `notes_stale`,
requires V to exceed this step's previous release, and verifies additive
migrations in the release range. The standalone
[scripts/check_additive_migrations.py](../../scripts/check_additive_migrations.py)
already exists; its presence alone does not prove those request-time checks
are wired. Intended behavior comes from the dev-branch releases revision 3
spec §§3.4–3.5, amended by the fidelity spec's PR gate.

### Request and prove the tag

Use the shipped command form `aq promote request --project demo --step release
--from S --version V`, adding `--notes-reviewed` after reading the notes at S.
The operator then uses `aq promote approve REQUEST_ID --project demo` to post
the exact-head GitHub review. See [request, approve and cancel](promotion-flow.md#request-approve-and-cancel)
for authority and PR details.

The lane fast-forwards the target to S and creates the annotated tag in two
recoverable writes. Its tag message names the promotion task, source and request.
Read-back requires the target to contain S and the tag to peel to S. A target
write with a missing tag is incomplete; a later visit finishes the tag without
moving a conflicting immutable tag. Source:
[src/integration/promotion_steps.py](../../src/integration/promotion_steps.py)
(`PromotionPublisher`).

**pending vivid-stone-39.4:** reviewed playbooks drive optional GitHub Release,
deploy-hook and post-publication actions. **pending vivid-stone-39.3:** hotfixes
use the same tag gate and send the published commit down the chain through
back-merge work. Neither a declared `after` field nor a prepared notes file
proves these actions ran.

### Select and deploy a release

For a source checkout installation, the operator configures
`~/.agent-queue/config.yaml` with the intended promotion target and tag family:

```yaml
deploy:
  tag_glob: v*
  target: main
```

`aq update --check` then selects the greatest stable semantic version matching
the glob, independently of tag creation time. `aq update --ref v<V>` selects
an explicit version. Selection pins the remote tag-object OID, fetches it,
requires an annotated tag peeling to a commit contained in `origin/<target>`,
and refuses a tag that changes during fetch. The update detaches the checkout
at that commit and records it in `deploy.json`. With no tag selector configured,
the existing branch-following update path remains available. An explicit
`--ref <branch>` is an unreleased deployment recorded as such. Sources:
[src/install/deploy.py](../../src/install/deploy.py) and
[src/cli/update.py](../../src/cli/update.py).

The installer also supports an explicit `AQ_REF` or configured
`AQ_TAG_GLOB` / `AQ_PROMOTION_TARGET`; see
[scripts/install.sh](../../scripts/install.sh). Wheel installations upgrade
the selected package rather than use checkout-only `aq update`.

An update preserves running agent sessions. If an operator needs a separate
restart after changing configuration, use `aq restart --no-dashboard` and
verify health, readiness and the supervisor's live terminal. A stop/start cycle
that kills sessions is not the update procedure. Workers and supervisors do
not deploy the operator installation. See [operations](operations.md).

### Roll code back while keeping data

`aq update --ref v<previous> --allow-rollback` selects an older release and keeps
the database; it does not run Alembic downgrades. Additive migrations and the
schema-ahead guard make compatible old code possible, but compatibility must
be verified for the actual release range. The shipped
[rollback drill](../../scripts/rollback_drill.py) exercises a disposable
database; it is not permission to downgrade the live schema. Stronger
release-request enforcement is **pending vivid-stone-39.2**. Sources:
[src/database/additive_migrations.py](../../src/database/additive_migrations.py)
and [migration guard](../../src/database/migration_guard.py).

### Back up and restore the database

`aq db backup [destination]` writes one private PostgreSQL custom archive,
the same format used for update backups. `aq db restore DUMP` first checks that
format and the archived revisions, prints the dump's UTC timestamp and the
estimated loss since that time, and refuses until the operator repeats that
timestamp as `--accept-data-loss DUMP_TIMESTAMP`.

> **Warning.** Restore replaces the configured database and loses subsequent
> state. The operator must end agent sessions and normally stop the daemon
> before applying it. The tool takes a fresh pre-restore recovery backup;
> retain that backup if recovering from a mistaken restore.

Unknown archived revisions are refused. Known older revisions can be restored;
the operator checks `aq db current` and, if needed, runs `aq db upgrade` before
restarting. `--force` overrides only the live-daemon refusal, never the active
session check or timestamp acknowledgment. A daemon-start lock and a second
pre-write check prevent a startup race. The printed loss bound cannot count
deleted rows or changes without timestamps. Workers use disposable test
databases and never run this procedure. Sources:
[src/cli/db.py](../../src/cli/db.py),
[src/database/backup.py](../../src/database/backup.py), and
[migrations](migrations.md).

## Inputs and outputs

Release input is a configured versioned step and reviewed S. Its output is a
delivered annotated tag. Deployment input is that tag; output is a detached
checkout and a deployment record. Restore input is a custom archive plus its
acknowledged loss timestamp; output is the restored database and a recovery
backup. Each result needs its own evidence.

## State ownership

The train writes promotion refs and tags. The local operator owns deployment,
restart and database replacement. The notes worker writes its ordinary task
branch (**pending vivid-stone-39.2** for automatic preparation). The database
records tasks and cached evidence; Git proves what code reached each branch.

## Common failures and recovery

| Symptom | Recovery |
|---|---|
| `deploy_tag_invalid` | Inspect the configured family, annotated object and remote OID; lightweight or changed tags are refused. |
| `deploy_not_related` | Select a tag contained in the configured promotion target. |
| Dirty checkout or worker-slot update refusal | Use a clean operator installation; do not bypass the refusal from a worker. |
| `schema behind code; ask the operator to upgrade` | Operator checks `aq db current` and upgrades outside a slot. |
| `restore_data_loss_unaccepted` | Review the printed bound; only the operator repeats the exact dump timestamp. |
| Restore refuses live sessions or unknown revisions | End the sessions or select a compatible archive/checkout; `--force` does not waive these checks. |
| `notes_stale` (**pending vivid-stone-39.2**) | Refresh notes input on the default branch, let it land, then request a new S. |

## Related pages

- [Promotion flows](promotion-flow.md) — schema, activation and PR gates.
- [Builds and releases](../contributing/releases.md) — packaging and wheel upgrades.
- [Migration policy](migrations.md) — database authority and schema guards.
- [Train runbook](git-first-train-runbook.md) — delivery and supervisor controls.

## Source and tests

[src/install/deploy.py](../../src/install/deploy.py),
[src/install/update.py](../../src/install/update.py),
[src/database/backup.py](../../src/database/backup.py).
Relevant implementation checks: `aq test tests/test_update.py
tests/test_update_rollback.py tests/test_db_cli.py`.
Documentation validation: `python3 scripts/check-docs.py docs/guides/releases.md`.
