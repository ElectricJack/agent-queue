---
playbook_id: object-loop
artifact_sha256: sha256:e460ed23a681f061e0227e6d1722d7cb13b9c7ec3c596b9e68a510466eef3d13
source_sha256: sha256:19c8d15bc6482d98b95c57fb101b7e36b46252ce6ae6646a128c4302a11869fb
contract_fingerprint: sha256:af9dc2189778dc24f6c86e8351d7853c1399dc6329421536dacdff4ae253962c
questions_resolved: 0
traces_sha256: sha256:a9c804055a8b7cfa59c8a04d19a9205ce7d4b037197ee07904d338af1fa95e81
capabilities_granted:
  aq_commands:
  - object_checkpoint_read
  - object_loop_inputs
  - object_loop_reconcile
  - object_loop_start
  - object_score_record
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
activation: separate operator decision required
proposal_review: rev-amber-zenith revision 2
---

# Object-loop artifact manifest

This recording binds executable bytes, source, command contracts and exact grants.
It is shipped inactive for artifact review. Proposal approval does not activate
this bundle or authorize paid generation, asset publication or product integration.
See source.md for the finite policy and formula handoff.

The reusable-building-block port documents the existing five typed commands and
their ownership; executable rules, command inputs, predicates, retries
and bounded foreach traversal are unchanged. This new artifact still requires
separate review and an operator activation decision. Existing loops remain bound
to their previous artifact until explicitly migrated.

The `required_capabilities` declaration is deferred under `amber-crest-92`:
it changes the execution principal and currently refuses `object_checkpoint_read`.
Legacy authority metadata is preserved; no authorization checks are changed.

`tests/fixtures/playbooks/object-loop-port/` retains the installed pre-port artifact
and synthetic command-boundary scenarios. `test_object_loop_playbook.py` compares
old/new command arguments and traversal for every event trigger, alongside the
unchanged disposable-database acceptance tests. The existing `traces.json` is
historical evidence with its original artifact identity, preserved byte-for-byte;
it is not relabelled as a run of this new artifact.
