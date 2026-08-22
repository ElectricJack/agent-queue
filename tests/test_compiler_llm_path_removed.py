"""Phase 6 T10 — the LLM compile path is gone from src/playbooks/compiler.py."""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from src.playbooks.compiler import PlaybookCompiler


def test_no_chat_provider_import():
    src = Path("src/playbooks/compiler.py").read_text(encoding="utf-8")
    assert "create_chat_provider" not in src
    assert "ChatProvider" not in src
    assert "_provider" not in src


def test_no_llm_compile_method():
    tree = ast.parse(Path("src/playbooks/compiler.py").read_text(encoding="utf-8"))
    classes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "PlaybookCompiler"
    ]
    assert classes, "PlaybookCompiler class not found"
    methods = {n.name for n in classes[0].body if isinstance(n, ast.AsyncFunctionDef)}
    assert "compile" not in methods


def test_compile_pipeline_deterministic_parse():
    md = textwrap.dedent("""\
        ---
        id: pipe
        kind: pipeline
        role: t
        scope: system
        triggers: [task.completed]
        ---
        ```json
        {"entry": "n0", "nodes": {"n0": {"terminal": true}}}
        ```
    """)
    c = PlaybookCompiler(config=None)
    r = c.compile_pipeline(md, existing_version=0)
    assert r.success is True, r.errors
    assert r.playbook is not None
    assert r.playbook.id == "pipe"


def test_playbooks_module_has_no_chat_provider_ref():
    """No file under src/playbooks/ references create_chat_provider."""
    root = Path("src/playbooks")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "create_chat_provider" in text:
            offenders.append(str(path))
    assert offenders == [], f"unexpected LLM factory refs: {offenders}"
