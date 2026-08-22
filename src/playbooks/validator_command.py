"""playbook_validate + playbook_install commands (Phase 6).

Kept out of the crowded ``src/commands/handler.py`` and out of the LLM
compiler.  Two commands:

- ``playbook_validate(path)`` — validate a markdown source (frontmatter
  only; full compile is now an agent-produced task) or a compiled JSON
  artifact.
- ``playbook_install(playbook_id, compiled_path)`` — re-validate a JSON
  artifact and install it through ``PlaybookManager``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from src.playbooks.models import CompiledPlaybook

logger = logging.getLogger(__name__)


def _structure_errors(raw: list[str]) -> list[dict]:
    """Best-effort split of ``Node 'x' transition[0]: goto…`` strings into
    (node, field, message).  Everything else falls back to
    ``node=None, field=None, message=<raw>``.
    """
    out: list[dict] = []
    for e in raw:
        node: str | None = None
        field: str | None = None
        msg = e
        if e.startswith("Node '"):
            end = e.find("'", 6)
            if end > 0:
                node = e[6:end]
                rest = e[end + 1 :].lstrip()
                if ":" in rest:
                    field_part, msg = rest.split(":", 1)
                    field = field_part.strip()
                    msg = msg.strip()
                else:
                    msg = rest
        out.append({"node": node, "field": field, "message": msg})
    return out


class PlaybookValidateInstallMixin:
    """Mixin adding ``playbook_validate`` and ``playbook_install`` commands."""

    async def _cmd_playbook_validate(self, args: dict) -> dict:
        path_arg = args.get("path")
        if not path_arg:
            return {
                "success": False,
                "errors": [
                    {"node": None, "field": "path", "message": "path is required"}
                ],
            }
        path = Path(path_arg)
        if not path.is_file():
            return {
                "success": False,
                "errors": [
                    {
                        "node": None,
                        "field": "path",
                        "message": f"file not found: {path}",
                    }
                ],
            }
        # .md source: only validate frontmatter presence. Full compile is now
        # an agent-produced task, so we report requires_compile=True.
        if path.suffix == ".md":
            raw = path.read_text(encoding="utf-8")
            if not raw.startswith("---"):
                return {
                    "success": False,
                    "errors": [
                        {
                            "node": None,
                            "field": "frontmatter",
                            "message": "missing YAML frontmatter",
                        }
                    ],
                }
            parts = raw.split("---", 2)
            if len(parts) < 3:
                return {
                    "success": False,
                    "errors": [
                        {
                            "node": None,
                            "field": "frontmatter",
                            "message": "malformed YAML frontmatter",
                        }
                    ],
                }
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as exc:
                return {
                    "success": False,
                    "errors": [
                        {
                            "node": None,
                            "field": "frontmatter",
                            "message": f"YAML error: {exc}",
                        }
                    ],
                }
            missing = [k for k in ("id", "triggers", "scope") if not fm.get(k)]
            if missing:
                return {
                    "success": False,
                    "errors": [
                        {
                            "node": None,
                            "field": k,
                            "message": "required frontmatter field missing",
                        }
                        for k in missing
                    ],
                }
            return {"success": True, "errors": [], "requires_compile": True}

        # .json compiled artifact.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "success": False,
                "errors": [
                    {"node": None, "field": None, "message": f"invalid JSON: {exc}"}
                ],
            }
        try:
            pb = CompiledPlaybook.from_dict(data)
        except Exception as exc:
            return {
                "success": False,
                "errors": [
                    {
                        "node": None,
                        "field": None,
                        "message": f"schema-shape error: {exc}",
                    }
                ],
            }
        errs = pb.validate()
        if errs:
            return {"success": False, "errors": _structure_errors(errs)}
        return {"success": True, "errors": []}

    async def _cmd_playbook_install(self, args: dict) -> dict:
        playbook_id = args.get("playbook_id")
        compiled_path = args.get("compiled_path")
        if not playbook_id or not compiled_path:
            return {
                "success": False,
                "errors": [
                    {
                        "node": None,
                        "field": None,
                        "message": "playbook_id and compiled_path are required",
                    }
                ],
            }
        # Server-side re-validation.
        v = await self._cmd_playbook_validate({"path": compiled_path})
        if not v["success"]:
            return {"success": False, "errors": v["errors"]}
        if v.get("requires_compile"):
            return {
                "success": False,
                "errors": [
                    {
                        "node": None,
                        "field": "compiled_path",
                        "message": (
                            "compiled_path must be a JSON artifact, not a markdown source"
                        ),
                    }
                ],
            }
        data = json.loads(Path(compiled_path).read_text(encoding="utf-8"))
        pb = CompiledPlaybook.from_dict(data)
        if pb.id != playbook_id:
            return {
                "success": False,
                "errors": [
                    {
                        "node": None,
                        "field": "id",
                        "message": (
                            f"artifact id '{pb.id}' != requested '{playbook_id}'"
                        ),
                    }
                ],
            }
        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is None:
            return {
                "success": False,
                "errors": [
                    {
                        "node": None,
                        "field": None,
                        "message": "playbook_manager not available on orchestrator",
                    }
                ],
            }
        await pm.install_compiled(pb)
        return {"success": True}
