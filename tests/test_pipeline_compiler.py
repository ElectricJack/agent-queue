import pytest

from src.playbooks.pipeline_compiler import compile_pipeline


VALID = """---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers: [task.created]
---

```json
{
  "entry": "attach_gate",
  "nodes": {
    "attach_gate": {
      "command": "gate_create",
      "args": {"project_id": "{{event.project_id}}", "gate_type": "routing", "title": "Route task", "waiter_task_ids": ["{{event.task_id}}"]},
      "on_success": "ensure_triage"
    },
    "ensure_triage": {
      "command": "ensure_task",
      "args": {"project_id": "{{event.project_id}}", "dedup_key": "triage-open", "title": "Triage new tasks"},
      "on_success": "done"
    },
    "done": {"terminal": true}
  }
}
```
"""


def test_valid_pipeline_compiles():
    r = compile_pipeline(VALID)
    assert r.success, r.errors
    pb = r.playbook
    assert pb.id == "default-pipeline"
    # role and kind survive on to_dict
    d = pb.to_dict()
    assert d["kind"] == "pipeline"
    assert d["role"] == "default-pipeline"


def test_rejects_unknown_command():
    bad = VALID.replace('"gate_create"', '"run_arbitrary_shell"')
    r = compile_pipeline(bad)
    assert not r.success
    assert any("run_arbitrary_shell" in e for e in r.errors)


def test_rejects_prompt_nodes():
    bad = VALID.replace('"terminal": true', '"terminal": true, "prompt": "hi"')
    r = compile_pipeline(bad)
    assert not r.success
    assert any("prompt" in e.lower() for e in r.errors)


def test_rejects_llm_transitions():
    bad = VALID.replace(
        '"on_success": "done"',
        '"transitions": [{"goto": "done", "when": "the vibe is right"}]',
    )
    r = compile_pipeline(bad)
    assert not r.success
    assert any("transition" in e.lower() or "when" in e.lower() for e in r.errors)


def test_missing_kind_pipeline_is_rejected():
    bad = VALID.replace("kind: pipeline\n", "")
    r = compile_pipeline(bad)
    assert not r.success
