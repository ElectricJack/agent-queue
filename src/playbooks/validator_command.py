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
                # "Node 'x': message" — drop the separator colon so the bare
                # message is not misread as an empty field name.
                if rest.startswith(":"):
                    rest = rest[1:].lstrip()
                if ":" in rest:
                    field_part, msg = rest.split(":", 1)
                    field = field_part.strip() or None
                    msg = msg.strip()
                else:
                    msg = rest
        out.append({"node": node, "field": field, "message": msg})
    return out


def _vault_bounded(self, path_arg: str) -> tuple[Path | None, str | None]:
    """Resolve *path_arg* and require that it live under the vault root.

    Mirrors the ``spec_approve`` guard: symlink escapes are caught because
    ``resolve()`` dereferences symlinks BEFORE the ``relative_to`` check.
    Returns ``(resolved_path, None)`` on success, or ``(None, error)`` on
    an out-of-vault path.
    """
    vault_root = Path(self.config.vault_root).resolve()
    try:
        resolved = Path(path_arg).resolve()
    except OSError as exc:
        return None, f"could not resolve path: {exc}"
    try:
        resolved.relative_to(vault_root)
    except ValueError:
        return None, f"path is outside vault root {vault_root}: {resolved}"
    return resolved, None


async def _resolve_source_authority(self, pm, playbook_id: str, data: dict):
    """Merge the vault source's authority into a submitted compiled artifact.

    Returns ``(merged_dict, diagnostics, error_dict_or_None)``.  The error is
    already in the command's structured-error shape.
    """
    from src.playbooks.compiler import PlaybookCompiler, apply_source_authority
    from src.playbooks.manager import AmbiguousPlaybookSource

    vault_root = getattr(self.config, "vault_root", None)
    try:
        found = pm.find_source_for_id(playbook_id, vault_root)
    except AmbiguousPlaybookSource as exc:
        logger.error("playbook_install_ambiguous_source id=%s paths=%s", playbook_id, exc.rel_paths)
        return {}, [], {"node": None, "field": "playbook_id", "message": str(exc)}

    if found is None:
        logger.error(
            "playbook_install_no_source id=%s vault_root=%s", playbook_id, vault_root
        )
        return (
            {},
            [],
            {
                "node": None,
                "field": "playbook_id",
                "message": (
                    "no source of authority: no .md under the vault declares "
                    f"id '{playbook_id}'"
                ),
            },
        )

    abs_path, rel_path = found
    try:
        markdown = Path(abs_path).read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [], {"node": None, "field": "playbook_id", "message": f"source unreadable: {exc}"}

    frontmatter, _ = PlaybookCompiler._parse_frontmatter(markdown)
    source_hash = PlaybookCompiler._compute_source_hash(markdown)

    existing = pm.get_playbook(playbook_id)
    existing_enabled = existing.enabled if existing is not None else None
    version = (existing.version if existing is not None else 0) + 1

    merged, diagnostics = apply_source_authority(
        data,
        frontmatter=frontmatter,
        rel_path=rel_path,
        source_hash=source_hash,
        version=version,
        existing_enabled=existing_enabled,
    )
    return merged, diagnostics, None


class PlaybookValidateInstallMixin:
    """Mixin adding ``playbook_validate`` and ``playbook_install`` commands."""

    async def _cmd_playbook_validate(self, args: dict) -> dict:
        path_arg = args.get("path")
        if not path_arg:
            return {
                "success": False,
                "errors": [{"node": None, "field": "path", "message": "path is required"}],
            }
        resolved, err = _vault_bounded(self, str(path_arg))
        if err is not None:
            return {
                "success": False,
                "errors": [{"node": None, "field": "path", "message": err}],
            }
        path = resolved
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
                "errors": [{"node": None, "field": None, "message": f"invalid JSON: {exc}"}],
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
        resolved, err = _vault_bounded(self, str(compiled_path))
        if err is not None:
            return {
                "success": False,
                "errors": [{"node": None, "field": "compiled_path", "message": err}],
            }
        compiled_path = str(resolved)
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
                        "message": ("compiled_path must be a JSON artifact, not a markdown source"),
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
                        "message": (f"artifact id '{pb.id}' != requested '{playbook_id}'"),
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

        # --- Source authority (Playbook V2 Package 0 §3.8) ------------------
        # The artifact is model output derived from untrusted prose, so it
        # owns ``nodes``/``rules`` and nothing else.  Everything the runtime
        # trusts — scope, triggers, profile_id, enabled, budgets — comes from
        # the operator's vault file, the vault path, or the server.  Without a
        # source there is no authority, so the install is refused.
        merged, diagnostics, source_error = await _resolve_source_authority(
            self, pm, playbook_id, data
        )
        if source_error is not None:
            return {"success": False, "errors": [source_error]}

        try:
            pb = CompiledPlaybook.from_dict(merged)
        except Exception as exc:
            return {
                "success": False,
                "errors": [
                    {"node": None, "field": None, "message": f"schema-shape error: {exc}"}
                ],
            }
        errs = pb.validate()
        if errs:
            return {"success": False, "errors": _structure_errors(errs)}

        try:
            await pm.install_compiled(pb)
        except Exception as exc:
            # install_compiled fails loudly (and rolls back) on store-save
            # failure (PB-5); keep the command's structured-error convention
            # so the compiler agent sees an actionable failure.
            logger.error("playbook_install failed for '%s'", playbook_id, exc_info=True)
            return {
                "success": False,
                "errors": [{"node": None, "field": None, "message": f"install failed: {exc}"}],
            }
        return {
            "success": True,
            "warnings": [
                {
                    "field": d.field,
                    "authored": d.authored,
                    "proposed": d.proposed,
                    "message": d.message,
                }
                for d in diagnostics
            ],
        }
