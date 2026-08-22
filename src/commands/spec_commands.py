"""Spec lifecycle commands (design §8, Phase 6).

Registers ``spec_approve`` — flips a spec's YAML frontmatter ``status``
to ``approved`` and emits ``spec.approved`` on the event bus.  The
default pipeline picks that event up and enqueues a spec-ingest task.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class SpecCommandsMixin:
    """Registers ``spec_approve``."""

    async def _cmd_spec_approve(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._active_project_id
        spec_path_raw = args.get("spec_path")
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        if not spec_path_raw:
            return {"success": False, "error": "spec_path is required"}

        vault_root = Path(self.config.vault_root).resolve()
        allowed_root = (vault_root / "projects" / project_id / "specs").resolve()
        spec_path = Path(spec_path_raw).resolve()
        try:
            spec_path.relative_to(allowed_root)
        except ValueError:
            return {
                "success": False,
                "error": f"spec_path is outside {allowed_root}",
            }
        if not spec_path.is_file():
            return {"success": False, "error": f"spec not found: {spec_path}"}

        raw = spec_path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return {
                "success": False,
                "error": "spec is missing YAML frontmatter",
            }
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {"success": False, "error": "spec frontmatter is malformed"}
        try:
            fm = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            return {
                "success": False,
                "error": f"frontmatter YAML error: {exc}",
            }

        fm["status"] = "approved"
        new_fm = yaml.safe_dump(fm, sort_keys=True).strip()
        spec_path.write_text(f"---\n{new_fm}\n---{parts[2]}", encoding="utf-8")

        bus = getattr(self.orchestrator, "bus", None)
        if bus is not None:
            try:
                await bus.emit(
                    "spec.approved",
                    {"project_id": project_id, "spec_path": str(spec_path)},
                )
            except Exception:  # pragma: no cover — defensive
                logger.debug("bus.emit spec.approved failed", exc_info=True)
        return {"success": True}
