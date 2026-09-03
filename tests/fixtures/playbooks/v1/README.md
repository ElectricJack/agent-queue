# Frozen V1 playbook sources

`default-pipeline.md` here is the byte-for-byte shipped
`src/prompts/default_playbooks/default-pipeline.md` as it stood at the last
commit before Package 6 rewrote it as a prose authoring source
(`ac353270`, the tip of the Package 6 inventory slice).

It exists because the shipped Markdown no longer carries an embedded JSON
action graph, and three things still need one:

- **the V1 arm of the shadow-parity harness** (child plan §3.5) compares V1 and
  V2 decisions over the same events, and V1 decisions come from
  `compile_pipeline` over a machine graph;
- **`src/playbooks/pipeline_lowering.py`'s deterministic lowering tests**
  (`tests/test_playbook_v2_compiler.py`) assert rule counts, loop lowering and
  closed transition sets over a real graph;
- **the reviewed V2 artifact** in `tests/fixtures/playbooks/v2/default-pipeline/`
  is the lowering of *this* graph, which is what makes "the V2 artifact behaves
  the way its V1 predecessor behaved" a mechanical fact rather than a claim.

**This file is frozen.** It is not installed, not compiled at runtime, and must
not be edited to track changes to the shipped prose. When the shipped prose and
its reviewed artifact change, the artifact's semantic diff against this snapshot
is the evidence a reviewer reads — an edit here would erase the baseline.
