"""Git commit identity commands: the installation default and its resolution.

``get_git_identity`` reports the installation default (``git_identity:`` in
``config.yaml``), the documented fallback used while it is unset, and — given
a project — the identity that project's AQ-authored commits resolve to.
``set_git_identity`` writes or clears the default through the same validated,
hot-reloaded path as ``update_config``.  Project overrides live on the project
row and are edited with ``edit_project`` (``git_identity_name`` /
``git_identity_email``).  Spec: ``docs/specs/git-identity.md``.
"""

from __future__ import annotations

from src.git.identity import (
    FALLBACK_IDENTITY,
    GitIdentityError,
    installation_identity,
    resolve_git_identity,
    validate_git_email,
    validate_git_name,
)


def agent_identity_refusal() -> dict | None:
    """Refuse an identity edit from an agent session or playbook step.

    Attribution is operator policy: a worker that could edit it could also
    launder its own commits past the publishing check.
    """
    from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal

    principal = current_principal() or TRUSTED_LOCAL
    if principal.kind in (PrincipalKind.SESSION, PrincipalKind.PLAYBOOK):
        return {
            "success": False,
            "error_code": "operator_only",
            "error": "Git identity is operator configuration; agent sessions cannot change it",
        }
    return None


class GitIdentityCommandsMixin:
    """``get_git_identity`` / ``set_git_identity``."""

    async def _cmd_get_git_identity(self, args: dict) -> dict:
        project = None
        project_id = str(args.get("project_id") or "").strip() or None
        if project_id:
            project = await self.db.get_project(project_id)
            if project is None:
                return {"success": False, "error": f"Project '{project_id}' not found"}
        installation = installation_identity(self.config)
        source = getattr(getattr(self.config, "git_identity", None), "source", "") or None
        result = {
            "success": True,
            "configured": installation is not None,
            "installation": installation.as_dict() if installation else None,
            "installation_source": source if installation else None,
            "fallback": FALLBACK_IDENTITY.as_dict(),
            "project_id": project_id,
            "effective": None,
        }
        if project is not None:
            result["effective"] = resolve_git_identity(self.config, project).as_dict()
        return result

    async def _cmd_set_git_identity(self, args: dict) -> dict:
        refusal = agent_identity_refusal()
        if refusal is not None:
            return refusal
        previous = installation_identity(self.config)
        if args.get("clear"):
            if args.get("name") or args.get("email"):
                return {"success": False, "error": "clear cannot be combined with name/email"}
            data = None
        else:
            try:
                name = validate_git_name(args.get("name"))
                email = validate_git_email(args.get("email"))
            except GitIdentityError as exc:
                return {"success": False, "error_code": "invalid_git_identity",
                        "field": exc.field, "error": str(exc)}
            source = str(args.get("source") or "manual").strip()[:120]
            data = {"name": name, "email": email, "source": source}
        written = await self._cmd_update_config({"section": "git_identity", "data": data})
        if written.get("error") or written.get("validation_errors"):
            errors = written.get("validation_errors") or [written.get("error")]
            return {"success": False, "error": "; ".join(str(e) for e in errors if e)}
        # The watcher applies the section in place.  Without one (no watcher
        # running) the live config is set here, so resolution never lags the
        # file it was just written to.
        if "git_identity" not in (written.get("applied_sections") or []):
            from src.config import GitIdentityConfig

            self.config.git_identity = GitIdentityConfig(**(data or {}))
        current = installation_identity(self.config)
        return {
            "success": True,
            "configured": current is not None,
            "installation": current.as_dict() if current else None,
            "previous": previous.as_dict() if previous else None,
            "changed": previous != current,
            "applies_to": (
                "Commits from sessions launched after this change; running pool "
                "sessions launched under another identity retire at their next claim."
            ),
        }
