# Development rollout and branch consolidation — 2026-09-09

The new development path is installed and enabled for Agent Queue and Matter Engine. Matter Engine remains paused. Strict hierarchy/train compatibility and old audit history are retained; intermediate verifier/CI/checkpoint requirements no longer govern development delivery.

## Verification

- Focused integration, service, command and prime checks: 93 passed.
- Development and generated API checks: 26 passed; final artifact check: 9 passed.
- Live follow-up (long commands, test artifacts): 18 passed.
- Remaining-feature backend subset: 203 passed.
- Playbook deletion dashboard subset: 39 passed.
- Exact terminal draft recovery subset: 55 passed.
- Disposable daemon/CLI: original 14 scenarios passed; new S15 passed after fixing the onboarding repository bridge. Separate runs, not a full repository test claim.
- Matter Engine: portable C++ agent protocol tests passed; four Python transport tests passed. No Windows editor build or full CI run.

## Live reconciliation

Public cancellation succeeded for CLI repair 81d0aaee, Discord repair 640ced19, and stalled root-batch repair 2c484c08. Attached stopped workspaces and refs were retained; delegate tasks paused. Public adoption closed keen-harbor, noble-ridge and noble-ridge.12 at main 70129d95 with source ancestry proof. No hosted CI evidence was fabricated.

AQ delivery task: wise-ridge. Matter Engine delivery task: brisk-summit. Existing Matter Engine parent/remaining feature tasks stay open; delivery of the protocol foundation does not complete the whole engine roadmap.

## Consolidation decisions

New changes retained: playbook deletion UI/typed response (wise-lantern), profile capability namespace round trips, idle-worker sleep intent (agile-crest), and whitespace-exact terminal draft recovery (smart-horizon). Main's editable playbook source view was retained across conflicts. Clients regenerated offline.

Superseded branches were merged as ancestry with the current tree retained: prime-ridge WIP (main already has the header/filter behavior plus time windows), fair-willow WIP (main already has combined requeue fixes and preserves stronger recovery), bright-journey (quiescent writer check already present), bright-vault (collector-only resume proposal superseded by broader recovery/development cancellation), old mandatory-triage (pre-V2/SQLite/Discord architecture retired), design branches and already cherry-picked CI/feature history fixes. The provider last_seen_at branch adds regression coverage; the existing monotone implementation and squashed migration were retained instead of adding an obsolete migration. Pool wake audit refs were merged normally.

No source branches were deleted. Historical darcyle upstream refs and Matter Engine's unrelated historical local branches are outside this delivery scope.

## Branch snapshot

Every listed tip must be reachable from the final AQ main before this delivery is complete.

| Branch | Tip |
|---|---|
| `aq/agile-crest` | `3bc4a41de28142d3f202aefb2ad107e304c78295` |
| `aq/bright-journey` | `330f5f2ec239dbaa9d25d136ff7a0c3bb04c4ca6` |
| `aq/bright-vault` | `ac15639c2227349ce8d1670792098faf248ad8ef` |
| `aq/integration/p-aeacda21cbc3fe134dede52ddb8a7a63/r-23902b8d33d997d489c44edb309aa723` | `50edaa1990c840a63efe3f7f3b9ca9c38e72fc10` |
| `aq/remaining-branch-delivery` | `cd5750c449ba96408d9f7d1343e46b8ef4ff6b56` |
| `aq/smart-horizon` | `d1e4d7f3cd4632308cb8e6dbe5e1150a40e9e21a` |
| `aq/sound-current.5` | `54aaca69793174cb1daaf6fea386e79e6453adb2` |
| `aq/streamline-integration` | `d6277c3355e6884857ff3045b32e62698ba3a308` |
| `aq/wise-lantern` | `fa94941f8ab151c80695ade3dd0b216b15ac63de` |
| `codex/fix-triage-routing-lifecycle` | `257bb840beb41bad0ba9b29f4c8b26dc5b469d57` |
| `codex/mandatory-triage` | `c0915c2a3baa3815ff419f7a4bd204c06ba5b241` |
| `design/global-worker-pools` | `c3afc8bbc0283508418f5c182b8843d6fb65b347` |
| `design/hierarchical-integration-trains` | `e3298a0207f5d8118bdb4d063217a3fbdaabe386` |
| `design/project-onboarding` | `3843e7a4bdfe65ac863085e923eb6eae535f2b92` |
| `fix/ci-integration-boundaries` | `171e798060f896d1d4ffbc42de4fbd808e4cd672` |
| `fix/feature-merge-model` | `8a0aea2301c38088c9ed6002601022249cc051f1` |
| `fix/profile-editor-capability-namespaces` | `9d52fa3bc97a4c403259062ce12fb9092b277aae` |
| `main` | `d6277c3355e6884857ff3045b32e62698ba3a308` |
| `safety/prime-beacon-before-prereq-rebuild` | `74c17a84e94bfb5e86e6677ecff47d07036abd78` |
| `origin` | `d6277c3355e6884857ff3045b32e62698ba3a308` |
| `origin/aq/agile-crest` | `3bc4a41de28142d3f202aefb2ad107e304c78295` |
| `origin/aq/bright-journey` | `330f5f2ec239dbaa9d25d136ff7a0c3bb04c4ca6` |
| `origin/aq/bright-vault` | `ac15639c2227349ce8d1670792098faf248ad8ef` |
| `origin/aq/fair-willow-wip` | `25d31d880b7391891a731d7e4e5a7afe407afd83` |
| `origin/aq/integration-repairs/d3b49b4e-3651-50cf-94ee-c6650ac6687f` | `50edaa1990c840a63efe3f7f3b9ca9c38e72fc10` |
| `origin/aq/integration/p-aeacda21cbc3fe134dede52ddb8a7a63/r-23902b8d33d997d489c44edb309aa723` | `9e8f06509b4af43ca9ba5a28a23adaf1c3a33b4d` |
| `origin/aq/prime-ridge-wip` | `653973dd1a7b50330bba746eb918cd38e71bba9b` |
| `origin/aq/smart-horizon` | `d1e4d7f3cd4632308cb8e6dbe5e1150a40e9e21a` |
| `origin/aq/sound-current.5` | `54aaca69793174cb1daaf6fea386e79e6453adb2` |
| `origin/aq/wise-lantern` | `fa94941f8ab151c80695ade3dd0b216b15ac63de` |
| `origin/main` | `d6277c3355e6884857ff3045b32e62698ba3a308` |
