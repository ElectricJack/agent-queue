"""Tests for the PlaybookCompiler — LLM-powered markdown-to-JSON compilation.

Deprecated: dv2 Phase 6 (compiler-as-agent) deleted the framework LLM
compile path.  Non-pipeline playbooks are now compiled by the
``playbook-compiler`` agent-type via ``playbook_validate`` +
``playbook_install``.  The original test surface exercised
``PlaybookCompiler.compile()`` with a mocked ``ChatProvider`` — neither
exists any more, so the whole module is skipped at collect time.

For the deterministic pipeline path see
``tests/test_pipeline_compiler.py``.  For the new agent-based flow see
``tests/test_playbook_validate_install_commands.py`` and
``tests/test_playbook_compile_task_enqueue.py``.
"""

import pytest

pytest.skip(
    "LLM compiler path removed in dv2 Phase 6 T10 — non-pipeline playbooks "
    "are now compiled by the playbook-compiler agent, not the framework.",
    allow_module_level=True,
)
