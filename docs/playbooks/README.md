# Reviewed playbook bundles for this repository

These are **this repository's own configured policy**, not shipped defaults and
not documentation samples. Each directory is an immutable reviewed bundle: the
Markdown source a human approved, the compiled artifact, its SHA-256, and a
manifest recording who approved what and why.

A bundle is what an operator imports and activates when they want a policy
change to be auditable rather than ambient. Read
[Playbooks V2](../concepts/playbooks.md) for the source → propose → validate →
activate path these files sit at the end of.

| Bundle | Recorded change |
|---|---|
| [`integration-only/default-pipeline/`](integration-only/default-pipeline/manifest.md) | Operator-requested removal of automatic task and branch reviews, 2026-09-09. Retains the three spec/proposal rules — spec ingest, the proposal human gate, and batch commit on that gate resolving — unchanged. |
| [`standard-high-default/default-assignment-routing/`](standard-high-default/default-assignment-routing/manifest.md) | Operator policy: most development work uses `standard-high`, and `deep-high` requires a justified exceptional difficulty. Only the class-selection prompt changes. |

## What each file is

| File | Content |
|---|---|
| `source.md` | The human-authored Markdown policy: frontmatter id, scope and triggers, then the rules in prose. |
| `artifact.json` | The compiled, SHA-256-addressed V2 definition. Never hand-edited. |
| `artifact.sha256` | The artifact's hash, which is also its identity. |
| `manifest.md` | Approval record: playbook id, artifact and source hashes, contract fingerprint, referenced profiles, and the reason for the change. |

The bundles AQ *ships* live in
[`src/prompts/reviewed_playbooks/`](../../src/prompts/reviewed_playbooks/) and are
seeded by the required-playbook reconciler. These are the local additions on top.

> **Not the automatic-review pipeline.** Neither bundle creates per-task
> reviewers, final branch reviewers, or review gates on downstream work — the
> `integration-only` bundle exists precisely because that behaviour was removed.

Every file here has a recorded disposition in
[the disposition ledger](../history/disposition-ledger.md).
