# Routing preferences

Two kinds of preference shape a route, and they are separate mechanisms. The
**policy preference** is the one below: a rule in the bound routing playbook
that applies to every task it routes. The **per-task preference** is a
`--prefer` on one task, set by whoever files it. Neither is a profile pin: the
router writes every route.

## The policy preference (routine hosted Codex)

The reviewed default router now prefers compatible Codex capacity for feature,
bugfix, refactor, test, docs, chore and sync work. This preference is an optional
`prefer_harnesses: [codex]` kind rule, not a profile pin or a project default.
An origin may clear the list. Classification still receives only task facts and
answers kind, class and verification flags; it cannot select a provider.

Verified narrow OpenCode lanes are considered first. Design retains its Claude
lane, art retains its Codex hold, research retains pressure balancing, and
allowlisted benchmark arms retain their exact class/harness. Explicit provider
intent, exclusions, reserved cells and workspace/class fit take precedence.

For a preferred hosted candidate, availability must be healthy, its own usage
must not exceed the existing soft limit (80% in the shipped policy), and spare
capacity must remain after live load, routed backlog and observed admission
constraints. Unknown usage stays unknown. Each provider's fresh quota windows
are evaluated against that provider's soft limit; stale/reset windows are kept
as labelled evidence, never fresh capacity inputs. No cross-provider or
unequal-window percentage comparison is made.

If preferred Codex capacity cannot take the work, free compatible alternatives
are considered by pressure. If all candidates are full, pressure still assigns
queued work; an unavailable set is held. Routing never reserves capacity,
changes fleet limits, migrates held work or raises intelligence for congestion.
Apply refreshes the snapshot under the existing fleet route lock and records
the actual selected profile/provider/class, pressure terms, headroom, quota
freshness/age, snapshot collection age and bypass reasons in `route.decision`.

## A per-task preference (`--prefer`)

The preference above belongs to the *policy*: every routine task in every
project bound to the default router gets it. A per-task preference belongs to
one task, is set by whoever files or re-routes it, and names a harness
(`--prefer codex`) or one worker profile (`--prefer deep-high-claude`):

```bash
aq task create --project demo --title "Draft the ADR" --prefer claude
aq task route --task-id demo.7 --prefer codex --prefer-mode strict
aq task route --task-id demo.7 --prefer ""          # clears it
```

It is an input to the router, not a bypass of it. The project's bound routing
playbook still writes the route, still picks the profile, and still applies
every lane, reservation, exclusion and class ceiling; `--prefer` only decides
*which of the candidates the router was already going to consider* come first,
or which of them may be considered at all. Two modes, `--prefer-mode`:

| Mode | The router | When the target is busy, out of usage or absent |
|---|---|---|
| `soft` (default) | Routes to the target whenever it has headroom | Routes normally; records that the preference was not honoured and why |
| `strict` | Admits only candidates that serve the target | Holds the task (`held`), or reports `no_candidates`; never falls back to another harness |

Filing validates the name: it must be an installed harness or an enabled worker
profile, and the mode must be `soft` or `strict`. A disabled profile, a stage,
named, read-only or non-worker profile and an unknown name are all refused, and
nothing is written. Supervisor and worker filings may set it exactly as the
operator does — it is a hint to the router, not a route, so it is not a
`routing.choice_forbidden` argument. On `aq task route` either half carries the
other forward — naming a target keeps the stored mode, naming a mode keeps the
stored target — so a re-route never silently relaxes a `strict` pin, and only
`--prefer ""` clears the pair.

What the router does with it, in order:

1. A **strict** preference narrows the candidate list to the profiles that
   serve the target before anything is scored. If none does, the plan is
   `no_candidates` with reason `prefer_target_unavailable`, and the task waits
   for its target. A full profile is not a refusal: the task queues for it.
2. A **soft** preference keeps the whole list and flags what serves the target.
   A **hold lane's** preferred tier still wins — a hold pins the provider, and a
   preference does not unpick it. Otherwise a free flagged candidate wins over
   the policy's ordinary tiers and over the ordinary hosted preference; the
   lowest-pressure rule is untouched when nothing is flagged.
3. Every route decision records the answer, whether or not the preference
   decided it:

   ```json
   "preference": {"target": "codex", "mode": "soft", "kind": "harness",
                  "honoured": false, "fallback_reason": "no_headroom"}
   ```

   `tasks.route.preference` and `tasks.route.decision.preference` both carry it,
   `aq task show` prints it as *Routing preference*, and the reason sentence
   says `preferred codex (soft) not honoured (no_headroom)`. The
   `fallback_reason` vocabulary is `no_candidate_serves_the_target`,
   `provider_unavailable`, `no_headroom` and `benchmark_arm_pins_its_model` —
   an allowlisted benchmark arm pins its model, so a preference never moves it.
4. `task_route_apply` re-reads the preference from the task, not from the plan,
   so a preference set between the plan and the apply is applied, and a plan
   that cannot serve a strict preference is refused (`stale`) rather than
   routed elsewhere.

A task that names no preference routes exactly as it did before the columns
existed: both are nullable, `NULL` means no preference, and the recorded
decision carries `"preference": null`.

## Replay evidence

The frozen historical input is
[`routine-routing-history-2026-10-02.json`](../reports/routine-routing-history-2026-10-02.json).
It is the route for the held task `azure-vault-92.6`, read through its authorized
task surface before this policy was activated. Its recorded policy is
`sha256:1cd0b0e3c3c259f0618ce5dfb48f2884adad1fc81bb4085a5d433a423442e71e`.
The original route already selected Codex despite degraded provider status and
88% own usage: its recorded pressure was 0.3125 versus Claude's 0.375.

Reproduce the counterfactual without database or daemon access:

```bash
python -m src.routing.replay \
  --input docs/reports/routine-routing-history-2026-10-02.json \
  --policy-source src/prompts/reviewed_playbooks/default-assignment-routing/source.md \
  --output /tmp/routine-routing-replay.json
```

The [recorded result](../reports/routine-routing-replay-2026-10-02.json) has one
replayed route, Codex 1 before and 1 after, Claude 0 before and after, and zero
changes. Historical headroom, quota-window identities and observation ages are
missing and explicitly reported. This single row cannot estimate fleet-wide
distribution or quota savings. A broader authorized export was requested from
the supervisor; use the same tool on it before drawing operational conclusions.
The tool skips override/role/benchmark routes and incomplete candidate evidence.

Controlled fixtures additionally show that the old policy sends two ordinary
tasks to idle Claude; the new policy sends the healthy-capacity task to Codex
and keeps the saturated-Codex task on Claude. Handler coverage verifies that
concurrent applies fill the two available Codex profile slots and send the next
task to Claude, while ignoring caller-supplied headroom.

## Reviewed activation

The source, compiled policy literals, classifier prompt and manifests are
shipped together in `src/prompts/reviewed_playbooks/default-assignment-routing/`.
The candidate artifact is
`sha256:2af4b14832ef7665ffd32979db32d770d0467b9192452854ddef64a9c09a787d`;
the policy is
`sha256:1054012d5b8c9ece77d158ac5121b45398cc1d527da67b090f1b0f0d25530694`.
Rebuilding the artifact changes its compile timestamp and hash; use the hash in
the rebuilt manifest if further source changes are made.

After delivery of the code, an operator stages the bundle in a fresh directory
inside the vault, then imports and reviews the exact candidate before activation:

```bash
mkdir -p ~/.agent-queue/vault/reviewed-playbooks/routine-routing-candidate-2026-10-02
cp src/prompts/reviewed_playbooks/default-assignment-routing/* \
  ~/.agent-queue/vault/reviewed-playbooks/routine-routing-candidate-2026-10-02/
aq playbook import --path reviewed-playbooks/routine-routing-candidate-2026-10-02
aq playbook artifact-diff --playbook-id default-assignment-routing \
  --target-sha256 sha256:2af4b14832ef7665ffd32979db32d770d0467b9192452854ddef64a9c09a787d
aq playbook activate --playbook-id default-assignment-routing \
  --artifact-sha256 sha256:2af4b14832ef7665ffd32979db32d770d0467b9192452854ddef64a9c09a787d
```

Record the prior active hash before activation for rollback through the same
`activate` command. This system artifact affects every project bound to the
default router. Inspect the intended projects' existing bindings and review a
project-scoped copy instead when a narrower activation is required. A project
already bound to a custom router must have that reviewed policy updated through
its own pipeline; changing the shipped default does not affect it. Rebinding
requires the local operator and an enabled validated router artifact.

Workers ship this candidate and verification evidence; they cannot activate a
system artifact or rebind a project. Installation seeding is write-if-absent, so
editing the shipped prompt alone does not update an installed router. Human,
identity, verification and mandatory-router gates remain authoritative.
