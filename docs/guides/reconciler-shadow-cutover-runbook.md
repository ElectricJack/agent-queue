# Shadow week and the guarded root cutover

The root subject engine ships **off**. `integration.reconciler_shadow` and
`integration.reconciler_active` both default to `false`, and nothing in this
guide turns either of them on for you. The procedure is: observe for a week
with the shadow loop installed and legacy still holding every decision, compare
the two, then transfer one root subject at a time behind an explicit human
decision.

Three things this guide will not do for you, because they are not the tool's to
decide:

- **It does not declare a week elapsed.** The report reads `recorded_at` from
  the journal and the legacy ledgers. A window shorter than seven days is
  reported incomplete and blocks. A worker session must never sit idle waiting
  for it.
- **It does not transfer a subject.** `aq integration shadow-report` is
  read-only. It *renders* the `engine-transfer` command for a human to read and
  run.
- **It does not approve anything.** `cleared_for_review: true` means the report
  found no open gate. Approval is a separate human decision recorded outside the
  report.

Read the mechanism first: [the subject foundation](../superpowers/specs/2026-10-02-integration-subjects-foundation.md),
[the decision tables](../superpowers/specs/2026-10-02-integration-policy-tables.md),
[the root adapters and cutover](../superpowers/specs/2026-10-02-root-integration-adapters.md),
and [the scenarios](../superpowers/specs/2026-10-02-root-reconciliation-scenarios.md).

## What the report compares

Two arms, both already durable, joined over one explicit window:

| Arm | Source | What one row is |
|---|---|---|
| Old | the declared legacy ledgers in `LEGACY_LEDGER` | one decision legacy actually made |
| New | `integration_subject_journal` where `mode = 'shadow'` and `entry_kind = 'decision'` | one decision the pinned policy table would have made |

A root batch maps to exactly one root subject (`uq_integration_subjects_batch` is
unique), so correlation is by batch. Both arms are grouped into coarse *action
classes* before comparison — `seal`, `build`, `publish`, `ci`, `repair`,
`promote`, `cleanup`, `ownership`, plus `observe`, `human` and `eject` — so one
primitive spelling cannot manufacture a divergence you have to adjudicate by
hand.

Every legacy decision in the window appears exactly once in the table. Each row
carries a verdict, and `no_route` is a separate, stronger flag:

| Verdict / flag | Meaning | Blocks |
|---|---|---|
| `agree` | the mapped subject made a shadow decision of the same class | no |
| `divergent` | the subject was observed but chose other classes | no |
| `missing` | nothing could cover the decision | yes |
| `no_route` | nothing in the window chose that class at all | yes |

`no_route` means legacy acted where the new engine would not have acted at all.
That is the finding the week exists to produce.

## Procedure

### 1. Install the shadow loop (legacy keeps every decision)

In `~/.agent-queue/config.yaml`, under `[integration]`:

```yaml
reconciler_shadow: true
reconciler_active: false
```

Then `aq restart --no-dashboard`. `integration` is not a hot-reloadable section:
a config value alone installs no loop into a running service.

Confirm the loop is visiting subjects before you start counting days. The
subjects and their journal are visible through the report itself:

```sh
aq integration shadow-report --project "$AQ_PROJECT_ID" --since 0 --until "$(date +%s)"
```

A non-empty `rows` table with `mode='shadow'` decisions means the loop is
installed. An empty window at this point means it is not; check
`integration.reconciler_shadow` and restart before continuing.

### 2. Record the window you are actually observing

The window is data, not a promise. Note the moment you installed the loop; that
timestamp is the `since` bound. From here the **operator** owns the week. No
worker session should hold a claim for it — file the observation as an operator
task or a note and close the worker.

Roughly daily, read the window and keep the artifact:

```sh
aq integration shadow-report --project "$AQ_PROJECT_ID" \
  --since "$START_EPOCH" --until "$(date +%s)" \
  --output "shadow-comparison-$(date +%F).md"
```

Read the `## Gates` section each time. It grows; nothing shrinks it. Re-running
with a later `--until` re-reads the same journal, so a day-old artifact stays
valid evidence for the days it covered — keep them.

### 3. Review the unknowns

The adapters spec is explicit that unknown observations, including unavailable
publisher authority, **are not successful actions**. The report therefore lists
every unknown observation of a fact-sensitive class and blocks until an operator
names the exact journal sequence:

```sh
aq integration shadow-report --project "$AQ_PROJECT_ID" \
  --since "$START_EPOCH" --until "$END_EPOCH" \
  --acknowledge-unknown 412 --acknowledge-unknown 419 \
  --output shadow-comparison-final.md
```

Acknowledging is a statement that a human read that row and accepted it. Do it
per sequence, in the artifact, with the reason — not by acknowledging every
sequence to make the gate go green.

### 4. Read the report's own gates

A non-empty `blocking_reasons` means cutover is **not** approved. The gates are:

| Gate | Cleared when |
|---|---|
| `the window contains no shadow decision` | the loop produced decisions |
| `the window spans N policy artifacts` | the artifact was recompiled mid-window; re-run one window per artifact |
| `the observation window covers Xs of the required 604800s` | a full week elapsed, read from recorded timestamps |
| `the reconciler already owned N root subject(s)` | legacy held exclusive ownership for the whole window |
| `N legacy decision(s) have no shadow comparison` | every legacy decision was covered or explained |
| `N legacy decision(s) have no route in the pinned policy artifact` | a reviewed decision table gained the route |
| `N legacy batch(es) changed in the window with no accounted decision` | the ledger covers everything legacy did |
| `N unknown shadow observation(s) are unacknowledged` | step 3 |

### 5. Two findings you should expect on a first week

**`unrouted_decisions` for candidate publication.** The shipped `root-train`
decision table has no route for publishing a candidate branch and its pull
request, so every legacy `integration_candidate_publications` row in the window
appears as `no_route` and blocks. This is correct: after a cutover the new
engine would not have published the candidate. The fix is a reviewed decision
table that routes the phase, not a report setting. Do not acknowledge it away.

**`unrouted_decisions` for branch-ownership release.** The same applies to
`integration_branch_owners` releases, which legacy performs for a root's
integration branch and no root primitive currently performs.

Either finding can also be resolved by an explicit human amendment recorded
against the change: a human may decide to cut over with the gap, provided the
gap is named in the cutover reason. That is a human decision, not a report
setting, and it belongs in `--reason` on the transfer.

### 6. Cut over one subject at a time

Only after the final artifact clears its gates and a human has recorded the
approval. The report prints the exact command; read it rather than composing
your own.

```sh
# Preview first. It writes nothing.
aq integration engine-transfer REPOSITORY_ID --engine reconciler \
  --expected-subject SUBJECT_ID:VERSION --reason "" --evidence ""
```

Copy the exact `SUBJECT_ID:VERSION` pairs from the preview into the apply, with
a reason and the evidence references. A cutover to `reconciler` is refused unless
`integration.reconciler_active` is already `true`:

```yaml
reconciler_active: true
```

```sh
aq restart --no-dashboard
```

```sh
aq integration engine-transfer REPOSITORY_ID --engine reconciler \
  --expected-subject SUBJECT_ID:VERSION \
  --reason "approved shadow evidence sha256:<report digest>" \
  --evidence "shadow-comparison:sha256:<report digest>" \
  --evidence "root-scenarios:<pushed commit>" \
  --apply
```

`engine-transfer` requires a reason, exact subject versions and evidence for a
cutover; it takes the repository's exclusive xact lock, and the development
publisher shares that exclusion, so it cannot enter afterwards. A stale visit
that was already in flight cannot act on the transferred subject.

The report digest is the artifact's identity: it covers the window, the pinned
artifact, every row, the unexplained batches and the unacknowledged unknowns. A
different window produces a different digest, so an approval cannot be reused
for evidence the operator did not read.

## Rollback

Feature-off rollback is the reverse order, and it keeps the journal: an
audited transfer back to `legacy` retains every `mode='active'` entry.

```sh
# 1. Stop the active loop first.
#    reconciler_active: false in ~/.agent-queue/config.yaml
aq restart --no-dashboard

# 2. Return exclusive ownership to legacy, one subject at a time.
aq integration engine-transfer REPOSITORY_ID --engine legacy \
  --expected-subject SUBJECT_ID:VERSION --reason "feature-off rollback" --apply
```

The report renders both command sets verbatim in its `## Rollback` section, and
lists every subject the window covers. `aq restart --no-dashboard` — never plain
`aq stop` then `aq start`, which kills every agent tmux session.

## What this comparison does not prove

The report carries these in its own `coverage_limits`, in every artifact:

- Only the declared `LEGACY_LEDGER` sources are the old arm. A legacy decision
  recorded outside them is neither compared nor counted as agreement.
- Agreement is per action class over a window, not per primitive, and not a
  replay of legacy's internal rule selection.
- A shadow observation is a decision the pinned table *would have* made while
  legacy still held exclusive ownership. It proves nothing about the remote
  outcome of that action.
- `observe`/`wait` bookkeeping, human gates and ejections have no legacy ledger
  counterpart and are reported without blocking.
- The report is evidence for a human approval. It never transfers a subject,
  enables the active loop, or asserts an elapsed period it did not read from
  recorded timestamps.

## Commands

| Command | Purpose |
|---|---|
| `aq integration shadow-report --project P --since S [--until U]` | Read-only comparison; `--output PATH` writes the Markdown artifact, `--acknowledge-unknown SEQ` names reviewed unknowns. |
| `aq integration engine-transfer R --engine reconciler ...` | Preview (default) or `--apply` the transfer of exact root subjects. |
| `aq integration engine-transfer R --engine legacy --apply` | Audited rollback to legacy ownership. |
| `aq restart --no-dashboard` | Install a config change without killing agent sessions. |
| `aq integration status PROJECT_ID --control-only` | Read the durable rollout control state; see [Hierarchical integration trains](hierarchical-integration-trains.md). |

The JSON envelope carries the full artifact — `report`, `digest`, `markdown`,
`blocking_reasons`, `operator_commands` and `rollback_commands` — so the evidence
can be archived without parsing Markdown.