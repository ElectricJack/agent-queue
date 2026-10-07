# Train and promotion policy entry

This entry maps the approved Git-first train fidelity design to its operational
documentation and records the unfinished release-policy work.

**Status: documentation draft**, owned by `vivid-stone-39.6`; finalization is
**pending vivid-stone-39.5**. The normative authority remains
[software-factory policy](../../concepts/factory-policy.md). This entry does not
introduce another publisher or an approval surface.

## Design authority and baseline

The operator-vault spec
`projects/agent-queue/specs/2026-10-06-git-first-train-fidelity.md`, §§2.2 and 4,
restores the prior process while preserving Git as the code-state authority.
Its §7 Q2 default is a PR pinned at S for each promotion. The companion
`projects/agent-queue/specs/2026-10-05-dev-branch-releases-revision-3.md`
defines the branch-chain schema, notes and deploy behavior. Where the older
spec describes `approve` as a local intent mutation, fidelity replaces that
with a GitHub review on the pinned PR.

The draft describes captured `origin/main` at `ce7aca55f` (2026-10-07), with
explicit pending markers for mechanisms absent there. The working epic base
`ea23e82b3` additionally carries E1, which is not yet claimed as shipped main.

## Fidelity crosswalk

| Fidelity section or block | Policy and documentation |
|---|---|
| §2.2 completion and collection | Retained completion refs, Git containment, immutable members and `AQ-Source` merges stay in the train. A worker close is a checkpoint, not delivery. |
| §2.2 epic completion and PR gate | Train opens the epic PR; root admission requires its exact source head and the boundary's checks. `authorized` does not require a human review; `reviewed` does. |
| §2.2 cadence | Root batches wait for quiet after the latest admitted input, capped from the first admission; defaults are 300/1800 seconds. Epic batches freeze immediately. |
| §2.2 candidate and repair | Candidate checks, attestation and expected-old fast-forward remain. Repairs follow bounded primary/debug policy and escalate according to `on_exhausted`. |
| §2.2 cleanup and controls | Git-proved delivery triggers separate cleanup. Pause/resume, eject and seal-now use preview/apply commands; see the [supervisor runbook](../../guides/git-first-train-runbook.md#supervisor-controls). |
| B1–B5 (D1–D3) | Flow schema, activation, promotion targets and pinned-PR intents are shipped. See [promotion flows](../../guides/promotion-flow.md). |
| B6–B8 | Annotated-tag selection, deployment records, additive-migration tooling and custom-archive restore are shipped. See [releases](../../guides/releases.md). |
| B9 | **pending vivid-stone-39.2:** prepare tasks, daemon notes inputs, digest/version/additive-migration request checks. |
| B10 | **pending vivid-stone-39.3:** hotfix filing, default-branch back-merge sources, intermediate fast-forward intents and the per-target ledger guard. |
| B11 | **pending vivid-stone-39.1:** chain/tag ruleset output, remote warnings and workflow triggers; implemented on this epic's base, awaiting main delivery. |
| B12 | **pending vivid-stone-39.4:** reviewed request/continuous policy bundles and post-publication actions. |
| B13 | This first draft lives in final locations. **pending vivid-stone-39.5:** finalize in place after E2–E4. |

Sources for shipped train behavior:
[src/integration/train_sources.py](../../../src/integration/train_sources.py),
[models.py](../../../src/integration/models.py),
[cleanup.py](../../../src/integration/cleanup.py), and
[promotion_steps.py](../../../src/integration/promotion_steps.py).
The [repository map](../../contributing/repo-map.md#train-fidelity-and-release-modules)
locates the added modules and marks later deletion as planned.

## Finalization checklist

**pending vivid-stone-39.5:** resolve each task marker against delivery on main,
not task completion alone. Keep these pages as their single sources of truth:

1. Reconcile E1 command help, rule classifications and warning-only remote
   diagnostics after its delivery; remove its pending markers together.
2. Verify E2's final prepare syntax and both notes formats, full-history input,
   digest stability and actual request-time version/migration checks.
3. Verify E3 routing and every lower branch's back-merge evidence without
   changing frozen batch membership.
4. Verify E4 bundle names, reviewed digests, step-type binding and actual
   GitHub Release/deploy-hook behavior before documenting them as available.
5. Replace the draft baseline with the delivered revision; update the
   [guides](../../guides/promotion-flow.md),
   [release procedure](../../guides/releases.md) and repository map in place.
   Repeat link/anchor checks and documentation coverage checks.

This draft leaves the live default-branch switch and first-release approval to
the explicitly human-gated rollout in fidelity §5. It does not delete legacy
modules, migrate the operator database or alter a live project's policy.
