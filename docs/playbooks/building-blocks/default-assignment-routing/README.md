# Default assignment routing: building-block port

This directory is the **installed-policy draft** for `grand-quest-20`. It is
source plus a compiled artifact for review, not an installation or activation.
The supervisor supplied the installed bundle on 2026-10-23 in
`.aq/tmp/installed-default-assignment-routing/` after the worker's live-source
read was denied. Its original artifact digest is
`sha256:49ab81f7a2f52b863cf11d001b719ea2b507c2b013dd618220ca1632cd362a9b`.
The supplied checksums were verified before porting.

The installed source is newer than the repository default. It has no ordinary
implementation-work Codex preference and breaks balance ties Claude first.
Those rules remain verbatim here. The same capability/evidence port is applied
separately to `src/prompts/default_playbooks/default-assignment-routing.md` and
its generated bundles, preserving that version's Codex preference and existing
regression expectations. These two policy variants must not be substituted for
each other. Only the installed-policy draft belongs in a review of the live
router; neither bundle was installed by this task.

The shared-fleet documentation alias is generated from the shipped recording
through `tests/fixtures/generated_sources/playbooks/index.json`, so rebuilding
the shipped router keeps that alias byte-identical as its import test requires.

## Changed and retained

The router already uses the reusable typed `task_route_plan` and
`task_route_apply` command boundary. Explicit `required_capabilities` replaces
implicit source capability requirements and opts into source-policy alignment
and run-bound effect authority. The declared command set equals the old graph's
granted command set. The source now states the exact hydrated guard predicates,
classifier input paths and existing limits so the compiler can check them.

The executable graph is retained: every transition, binding, command argument,
policy byte, classifier prompt, schema, budget and tool restriction is unchanged.
The three plan/apply branches are necessary for single-assignment bindings. The
bounded classifier remains an LLM step because classification needs judgment.
The installed classifier's existing prose about Codex preference is also left
unchanged; resolving that discrepancy would change its prompt and is outside
this equivalence port.

No hand-written loop, fleet scan, notification or script exists to replace.
Adding collections, snapshot pages, state/notify, doctor scans or scripts would
change observation timing or add side effects. Capacity/quota observation and
fresh apply-time selection remain inside the typed commands. Therefore the
source adds neither `state_schema_version` nor `scripts`.

## Evidence and reproduction

`aq playbook commands --prefix task_route_ --include-schema --limit 10 --json`
confirmed both commands are available with typed results. The compiled command
fingerprints remain identical to the installed baseline. The installed draft's
`playbook-compiler` profile fingerprint comes from this checkout's shipped
profile, not the live vault; the supervisor must reconcile that profile pin
before any future live installation. Source hashes, source locations, compile
time and the trusted-execution metadata also change. The new authority metadata
records the run's caller; byte equality applies to routing behavior, not those
artifact/run provenance fields.

Frozen installed and shipped source/artifact baselines, original checksums and
representative events live in
`tests/fixtures/playbooks/routing-building-blocks/`. The equivalence test compares
the entire executable graph, then runs both artifacts through the real engine
with the same typed command results and classifier responses. It requires byte
equality of command arguments, bindings, classifier inputs/prompts, relevant
command authority, selected steps and terminal results. Scenarios cover all
declared plan/apply outcomes on each path, failed/invalid classification, legacy
admission, stale events and another project's router. Separate real-handler
tests verify the installed Claude fallback before and after; the existing
shipped routing tests are unchanged.

Rebuild the installed draft without writing the shipped bundles:

```bash
python scripts/rebuild-reviewed-playbook-artifacts.py \
  --source docs/playbooks/building-blocks/default-assignment-routing/source.md \
  --out docs/playbooks/building-blocks/default-assignment-routing \
  default-assignment-routing
```

Add `--check` to check deterministic compiled output. The normal shipped bundle
uses `python scripts/rebuild-reviewed-playbook-artifacts.py default-assignment-routing`.
Both compilation paths produce an empty diagnostics list. Focused verification
(with `POSTGRES_TEST_DSN` set to the disposable test service):

```bash
aq test tests/test_assignment_routing_building_blocks.py \
  tests/test_default_assignment_routing_playbook.py
```
