import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

FRONTEND_ROOT = Path(__file__).parent.parent / "dashboard" / "src" / "panes"


def _read_frontend_manifest_ids() -> set[str]:
    ids: set[str] = set()
    for manifest_path in FRONTEND_ROOT.glob("*/manifest.ts"):
        text = manifest_path.read_text(encoding="utf-8")
        m = re.search(r'id\s*:\s*"([^"]+)"', text)
        if m:
            ids.add(m.group(1))
    return ids


def test_console_stream_is_agent_pushable():
    entry = SERVER_PANE_REGISTRY["console-stream"]
    assert entry.agent_pushable is True


def test_frontend_and_backend_registries_match():
    frontend = _read_frontend_manifest_ids()
    backend = set(SERVER_PANE_REGISTRY.keys())
    missing_in_backend = frontend - backend
    extra_in_backend = backend - frontend
    assert not missing_in_backend, (
        f"frontend manifests without backend entry: {missing_in_backend}"
    )
    assert not extra_in_backend, (
        f"backend entries without frontend manifest: {extra_in_backend}"
    )
