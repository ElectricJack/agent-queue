"""Tests for PlaybookManager — compilation error handling and version management.

Deprecated: dv2 Phase 6 (compiler-as-agent) removed the framework LLM
compile path.  This file exercised ``PlaybookCompiler.compile()`` with a
mock ``ChatProvider``, which no longer exists.  The module is skipped at
collect time.  Deterministic pipeline coverage lives in
``tests/test_pipeline_compiler.py``; the new agent-based flow is covered
by ``tests/test_playbook_validate_install_commands.py`` and
``tests/test_playbook_compile_task_enqueue.py``.
"""

import pytest

pytest.skip(
    "LLM manager compile path removed in dv2 Phase 6 T10 — see "
    "test_playbook_validate_install_commands.py and "
    "test_playbook_compile_task_enqueue.py for the compiler-as-agent flow.",
    allow_module_level=True,
)
