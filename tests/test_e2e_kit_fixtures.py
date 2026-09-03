"""The swarm e2e kit's Tier-1 stand-ins must close the way a real agent would.

Under ``sessions.provider: fake`` nothing is spawned: ``scripts/e2e/smoke.py``
*is* the session, and it produces no commits.  The e2e remote is a bare
local repository, so no pull request can ever exist for a task branch there.
Two things follow, and each is pinned here because losing either one turns
S4 into "close refused: No open PR found" (task solid-forge-63):

* the runner closes a formula child with ``--work-outcome no-op`` — the
  pipeline's own word for "this task produced no code" — not ``shipped``,
  which under the ``pull_request`` default demands an open PR;
* the generated ``reviewer`` fixture is ``read_only`` like the shipped
  reviewer profile, so a review is a no-code task by declaration and never
  has to answer the PR gate at all.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.profiles.parser import parse_profile

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "scripts" / "e2e" / "smoke.py"
E2E_ENV = REPO_ROOT / "scripts" / "e2e-env.sh"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("e2e_smoke", SMOKE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolve ``from __future__ import annotations`` through
    # ``sys.modules[cls.__module__]``, so the module must be registered
    # before its body runs.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_s4_child_close_reports_a_no_op_work_outcome(monkeypatch):
    smoke = _load_smoke()
    calls: list[tuple] = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("task", "children"):
            return {"children": [{"id": "child-1", "status": "IN_PROGRESS"}]}
        if args[:2] == ("session", "list"):
            return {"sessions": [{"id": "sess-1", "task_id": "child-1", "state": "running"}]}
        if args[:2] == ("task", "close"):
            return {"success": True}
        raise AssertionError(f"unexpected aq call: {args}")

    monkeypatch.setattr(smoke, "aq", fake_aq)
    monkeypatch.setattr(smoke, "session_token", lambda session_id: f"tok-{session_id}")

    smoke._close_next_child("container-1")

    closes = [(a, kw) for a, kw in calls if a[:2] == ("task", "close")]
    assert len(closes) == 1
    args, kwargs = closes[0]
    assert args[2] == "child-1"
    assert kwargs == {"token": "tok-sess-1", "session_id": "sess-1"}
    outcome = args[args.index("--work-outcome") + 1]
    assert outcome == "no-op", (
        "the Tier-1 runner produces no commits; anything but no-op makes the "
        "pull_request default demand a PR the bare e2e remote cannot carry"
    )


def _vault_fixture_section() -> str:
    """The ``3. Vault fixtures`` block of e2e-env.sh, as bash source."""
    text = E2E_ENV.read_text()
    match = re.search(r"^# 3\. Vault fixtures\n.*?(?=^# -+\n# 4\. Config)", text, re.DOTALL | re.MULTILINE)
    assert match, "e2e-env.sh no longer has a '3. Vault fixtures' section"
    return match.group(0)


@pytest.fixture
def generated_profiles(tmp_path):
    vault = tmp_path / "vault"
    (vault / "agent-types").mkdir(parents=True)
    (vault / "formulas").mkdir()
    env = {**os.environ, "E2E_VAULT": str(vault), "REPO_ROOT": str(REPO_ROOT)}
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _vault_fixture_section()],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    def read(role: str):
        parsed = parse_profile((vault / "agent-types" / role / "profile.md").read_text())
        assert not parsed.errors, parsed.errors
        return parsed

    return read


def test_e2e_reviewer_fixture_is_read_only_like_the_shipped_reviewer(generated_profiles):
    reviewer = generated_profiles("reviewer")
    assert reviewer.config.get("read_only") is True
    allowed = set((reviewer.tools or {}).get("allowed") or [])
    assert not allowed & {"Write", "Edit"}, allowed


def test_e2e_coding_fixture_keeps_its_write_tools(generated_profiles):
    coding = generated_profiles("coding")
    assert not coding.config.get("read_only")
    allowed = set((coding.tools or {}).get("allowed") or [])
    assert {"Write", "Edit"} <= allowed, allowed


def test_pool_worker_close_reports_a_no_op_work_outcome(monkeypatch):
    smoke = _load_smoke()
    calls: list[tuple] = []

    def fake_aq(*args, **kwargs):
        calls.append((args, kwargs))
        return {"success": True, "next": {"result": "drain_requested"}}

    monkeypatch.setattr(smoke, "aq", fake_aq)
    worker = smoke.Worker(session_id="sess-1", token="tok", claim_epoch=3, task_id="t-1")

    worker.close(claim_next=True, summary="S2 task")

    (args, kwargs), = calls
    assert args[:2] == ("task", "close")
    assert args[args.index("--work-outcome") + 1] == "no-op"
    assert args[args.index("--claim-epoch") + 1] == "3"
    assert "--claim-next" in args
    assert kwargs == {"token": "tok", "session_id": "sess-1", "check_ok": True}
