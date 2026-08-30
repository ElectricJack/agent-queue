
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


def test_structured_errors_shape():
    """Compile errors must be structured dicts {node, field, message}."""
    bad = VALID.replace('"gate_create"', '"run_arbitrary_shell"')
    r = compile_pipeline(bad)
    assert not r.success
    assert r.structured_errors, "structured_errors must be populated on failure"
    for rec in r.structured_errors:
        assert set(rec.keys()) >= {"node", "field", "message"}
        assert isinstance(rec["message"], str) and rec["message"]
    # command error is node-scoped
    cmd_errs = [e for e in r.structured_errors if e["field"] == "command"]
    assert cmd_errs and cmd_errs[0]["node"] == "attach_gate"


def test_rejects_unreachable_node():
    bad = VALID.replace(
        '"done": {"terminal": true}',
        '"done": {"terminal": true}, "orphan": {"command": "list_tasks", "on_success": "done"}',
    )
    r = compile_pipeline(bad)
    assert not r.success
    assert any("nreachable" in e for e in r.errors), r.errors


def test_rejects_cycle_without_terminal_exit():
    """A→B→A cycle with a separate terminal must still fail because
    the cycle nodes cannot reach the terminal."""
    cycle = """---
id: cyc
kind: pipeline
role: cyc
scope: system
triggers: [task.created]
---

```json
{
  "entry": "a",
  "nodes": {
    "a": {"command": "list_tasks", "on_success": "b"},
    "b": {"command": "list_tasks", "on_success": "a"},
    "done": {"terminal": true}
  }
}
```
"""
    r = compile_pipeline(cycle)
    assert not r.success
    # Either unreachable ('done' can't be reached from entry) or trapped
    # (a,b in cycle) — both are compile errors here. We accept either.
    assert any(
        "reachable" in e or "cycle" in e or "path to a terminal" in e for e in r.errors
    ), r.errors


def test_rejects_node_claiming_entry():
    bad = VALID.replace(
        '"attach_gate": {',
        '"attach_gate": {"entry": true, ',
    )
    r = compile_pipeline(bad)
    assert not r.success
    assert any("entry" in e.lower() for e in r.errors)


def test_store_roundtrip_preserves_pipeline_fields(tmp_path):
    """Pipeline metadata (kind, role, per-node action) must survive
    CompiledPlaybookStore.save → load, and validate() must accept the
    reloaded playbook."""
    from types import SimpleNamespace

    from src.playbooks.store import CompiledPlaybookStore

    r = compile_pipeline(VALID)
    assert r.success, r.errors
    pb = r.playbook

    vm = SimpleNamespace(compiled_root=str(tmp_path))
    store = CompiledPlaybookStore(vm)
    path = store.save(pb, scope="system")
    assert path

    loaded = store.load(pb.id, scope="system")
    assert loaded is not None
    assert loaded.kind == "pipeline"
    assert loaded.role == "default-pipeline"

    # Per-node action dicts survive intact
    assert loaded.nodes["attach_gate"].action is not None
    assert loaded.nodes["attach_gate"].action["command"] == "gate_create"
    assert loaded.nodes["attach_gate"].action["on_success"] == "ensure_triage"
    assert loaded.nodes["ensure_triage"].action["command"] == "ensure_task"
    assert loaded.nodes["done"].action is None
    assert loaded.nodes["done"].terminal is True
    assert loaded.nodes["attach_gate"].entry is True

    # validate() accepts the reloaded pipeline
    errs = loaded.validate()
    assert errs == [], errs
