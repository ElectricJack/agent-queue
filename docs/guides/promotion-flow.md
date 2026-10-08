# Configure and operate a promotion flow

A promotion flow moves one tested commit through an ordered branch chain,
using a pull request for each promotion and the integration train to publish it.

> The mechanisms below are implemented. Each project activates its own flow and
> reviewed policy bundles; shipping these files does not switch a live project.

## Why it exists

Ordinary work lands on the repository's default branch. A flow lets a project
use `dev` as that branch, then promote to `staging` or `main` under a separate
gate. The train remains the configured publisher. See the
[factory policy](../concepts/factory-policy.md) for ownership and the
[releases guide](releases.md) for preparation, tags and deployment.

## Vocabulary

- **Step:** one `source → target` branch pair in the flow.
- **S:** the exact source commit selected when requesting a promotion.
- **Intent:** the recorded request, its frozen step settings and private PR ref.
- **Gate:** the required checks and human approval for S.
- **Attestation:** the trusted App's check proving this step authorized publication.

The PR head is a private `aq/promote/<step>/<request-suffix>` branch pinned at S,
with the step's target as its base. Advancing `dev` after a request does not
change that PR's head. The logical promotion is `dev → main`; its GitHub head
is the private pinned ref. This is the fidelity spec's §7 Q2 model, implemented
by [src/commands/promote_commands.py](../../src/commands/promote_commands.py).

## A realistic example

Start with the read-only help command; it needs no configured project and
creates nothing:

```bash
aq promote request --help
```

```text
Usage: aq promote request [OPTIONS]

  Open a pinned step PR and an idempotent promotion intent.
```

The following configuration is a template for a disposable project `demo`
whose repository default branch is already `dev`. It does not switch the
default branch. Its committed trust manifest must allow the named promotion
attestation and check set before validation can pass.

For a configured flow, `aq integration trust-manifest` reads its stored
attestation names. App verification, functional preflight and
`aq doctor --check integration.trust` compare those same identities against
the committed manifest. Missing or different promotion names remain a trust
failure; projects without a flow keep the original manifest format.

```yaml
promotion_flow:
  - id: release
    source: dev
    target: main
    type: request
    gate:
      checks: inherit
      approval: operator
    versioning:
      kind: semver_tag
      source: pyproject
      tag_format: v{version}
    notes:
      kind: none
```

The operator validates `flow.yaml`, then activates it with
`aq project set demo promotion-flow flow.yaml --expected-integration-generation N`,
where N is the current project integration generation. Activation uses a policy
fence, creates missing targets at their source tip with create-only pushes,
and refuses incompatible changes while a step has a live intent. Removing this
disposable configuration uses `aq project set demo promotion-flow clear
--expected-integration-generation N` after its intents settle; protected branch
cleanup remains the operator's responsibility. Source:
[src/integration/records.py](../../src/integration/records.py) and
[src/cli/projects.py](../../src/cli/projects.py).

### Schema and chain rules

`aq promote schema` publishes the same schema as
[promotion-flow-schema.json](../reference/promotion-flow-schema.json).
`aq promote validate --project demo --file flow.yaml` checks a file;
omit `--file` to check the stored flow. Neither activates it.

| Property | Meaning and shipped constraint |
|---|---|
| `promotion_flow` | A list of steps; `null` or an empty list disables promotions. A bare list is also accepted as input. |
| `id`, `source`, `target` | Required. IDs are unique and match `[a-z0-9][a-z0-9-]{0,31}`. The first source equals the repository default; each later source equals the preceding target. Targets are unique and differ from the default. |
| `type` | `request` by default, or `continuous`. A request step cannot use `approval: none`. |
| `gate.checks` | `inherit` uses the trust manifest's required checks; `manifest:<name>` uses its named check set. |
| `gate.attestation` | Defaults to `Agent Queue Promotion Attestation (<id>)`; names must be distinct and trusted by the manifest. |
| `gate.approval` | `operator` by default, `requester`, or `none` for continuous steps. Optional `operator_logins` narrows eligible human repository administrators. |
| `gate.request_ttl` | Defaults to `7d`; status reports stale requests. Expiry is not permission to publish. |
| `versioning` | `none`, `semver_tag`, or `custom`. Version sources include `pyproject`, `package.json`, and `file:<path>:<regex>`. Tag placeholders are `{version}`, `{sha12}`, `{utc_date}`, `{step}`. Tag families cannot overlap each other or reserved manifest globs. |
| `notes` | `none`, `file_template`, or `changelog_heading`; nonempty notes require a path and version placeholder. Preparation records immutable source input; requests verify its digest at S. |
| `after` | Schema includes `backmerge`, `github_release`, `deploy_hook`. Back-merge work uses a daemon-owned source or promotion intent. Post-publication policy must be explicitly activated. |

Continuous steps cannot require a version bump: `semver_tag` and a custom
format containing `{version}` are refused. A custom tag using `{sha12}` is
allowed. Schema, chain and manifest errors carry a JSON pointer and validation
layer; fix the first failing layer before proceeding. Source:
[src/integration/promotion_steps.py](../../src/integration/promotion_steps.py)
(`FlowSchema`).

### Request, approve and cancel

These shipped command forms are templates for the configured project,
not commands executed as part of this documentation task:

| Command form | Result |
|---|---|
| `aq promote request --project demo --step release --from S --version V` | Opens the private PR and an idempotent intent. Replace S with a full commit OID and V with the version at that commit. Omit `--from` to pin the current source tip. |
| `aq promote approve REQUEST_ID --project demo` | The local human operator posts a GitHub review as their authenticated `gh` user. It does not write a second approval gate in AQ. |
| `aq promote cancel REQUEST_ID --project demo` | The operator or requester closes an unpublished PR and aborts its intent; publication already underway or proved delivered is refused. |
| `aq promote status --project demo --step release` | Reads intent state, cached evidence and request age. |
| `aq promote list --project demo --step release --limit 20` | Lists recorded promotion intents from the local cache. It is not a fresh enumeration of remote release tags. |

A repeat request returns the existing identity; a cancelled identity cannot be
reused. Under `approval: operator`, a supervisor's request waits for a human
repository administrator's review on S. `requester` requires the requester's
GitHub approval on S. A change request from an eligible approving reviewer
holds publication; other reviewers are advisory. `none` skips review checks.
A draft or closed PR, a changed head, or unavailable evidence hold publication.
The integration service observes the PR again before its fenced write. Sources:
[src/commands/promote_commands.py](../../src/commands/promote_commands.py) and
[src/integration/promotion_steps.py](../../src/integration/promotion_steps.py)
(`StepPullRequestGate`, `PromotionPublisher`).

The candidate is S itself. The target must be an ancestor of S; the lane creates
no merge commit. After the checks and PR gate pass, it emits the step's
attestation, fast-forwards the target with its expected old OID, and, when
versioned, creates an annotated tag that peels to S. Read-back proves delivery;
the PR then becomes merged through reachability. A crash between branch and tag
writes resumes the missing write, while a conflicting tag holds for an operator.

### Rulesets and workflow triggers

`aq promote rulesets --project demo [--file flow.yaml]` to print branch/tag
ruleset JSON and workflow requirements for a repository administrator to apply.
The command makes no GitHub configuration writes. It derives every chain branch,
uses a distinct App attestation on each promotion target, and emits a tag
creation rule with the App as its only bypass actor plus an update/deletion
rule with no bypass actors. Never give the App a bypass of branch checks.

`aq promote validate --project demo --remote`
reads chain protection, tag rulesets and workflows. It reports layer-four diagnostics. Remote warnings do not turn
schema validity into a failed result: inspect `warnings`, `protection` and
`workflow_triggers`. Hidden bypass actors or unreadable workflows are
unverifiable, not evidence of correct protection.

required test workflows cover `pull_request` to
the default and every chain target, including `ready_for_review`, and pushes to
`aq/promote/**` as well as the train's candidate refs. Branch-attestation
workflows cover the whole chain. Apply trigger changes before flipping the
default branch, and bring them into older source trees: updating main alone
does not update the workflow at pinned S. Sources:
[src/cli/promote_rulesets.py](../../src/cli/promote_rulesets.py) and
[src/integration/promotion_steps.py](../../src/integration/promotion_steps.py)
(`promotion_rulesets`, `promotion_workflow_triggers`, `validate_promotion_remote`).

### Hotfixes and policy automation

`aq promote hotfix --project demo --step release --title "Fix description"`
files work based on that step's target, with delivery routed to the target rather
than ordinary default-branch admission. After the task completes, `aq promote request --project demo --step release
--from-task TASK --version V --notes-reviewed` requests its pinned PR. A semantic
version hotfix must increment the target PATCH version exactly once. With `after.backmerge`, each lower branch receives
the published commit: a new ordinary source batch on the default, and
fast-forward intents on intermediate targets. Existing frozen membership stays
unchanged; divergence holds as `backmerge_not_fast_forward`, and a request
missing a required hotfix OID is refused as `backmerge_pending`.

Reviewed `promotion-request` and `promotion-continuous` bundles resolve from the
stored step type and require an activation scoped to that project; system
activations do not enable promotion policy for other projects.
They request or replace intents, wait and notify on holds, drive the existing PR
gate, and trigger backmerge after delivery. Both ship inactive; activate the
reviewed artifact hash for the intended project before starting delivery.

## Inputs and outputs

Input is a validated flow, committed trust, selected S and its version/notes.
Output is a pinned PR, a frozen intent, gate evidence, an attested target update
and an optional annotated tag. `aq integration status demo` also projects the
configured branch chain. A promotion target has no ordinary task frontier:
only an admitted step intent supplies its member.

## State ownership

The operator activates `projects.promotion_flow` through `CommandHandler`.
The daemon writes promotion intent and result metadata, batches and retained
sources. Git decides delivery; check/review rows are refreshable evidence.
Workers publish their task checkpoints and run their focused checks; the train
owns chain ref writes. Supervisors can coordinate requests, while approval,
deployment and restore retain their operator restrictions.

## Common failures and recovery

| Symptom | Recovery |
|---|---|
| `promotion_flow_empty`, `step_unknown` | Inspect the stored flow and the requested step ID. |
| Validation problem with a pointer | Correct that schema, chain or manifest field and validate again. |
| `promotion_not_fast_forward` | Inspect target/source ancestry; cancel and request a source that contains the target. |
| PR gate waiting or unknown | Inspect the exact pinned PR head, required checks and reviewer identity; retry observation after a provider outage. |
| `promotion_tag_conflict` | Inspect both tag-object OID and peeled commit; preserve the immutable tag and resolve with the operator. |
| Remote warnings | Have the repository administrator reconcile rulesets and triggers, then repeat `validate --remote`. |

## Related pages

- [Releases](releases.md) — preparation, versioned tags, deploy and restore.
- [Train runbook](git-first-train-runbook.md#supervisor-controls) — batch controls.
- [Promotion policy design entry](../specs/design/promotion-flow.md) — fidelity
  crosswalk and activation boundaries.

## Source and tests

[src/cli/promote.py](../../src/cli/promote.py),
[src/integration/promotion_steps.py](../../src/integration/promotion_steps.py),
[src/commands/promote_commands.py](../../src/commands/promote_commands.py).
Relevant implementation checks: `aq test tests/test_promotion_flow.py
tests/test_promote_commands.py tests/test_promotion_steps.py`.
Documentation validation: `python3 scripts/check-docs.py docs/guides/promotion-flow.md`.
