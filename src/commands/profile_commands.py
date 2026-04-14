"""Profile commands mixin — agent profile CRUD, export/import."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


class ProfileCommandsMixin:
    """Profile command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # Agent Profile commands -- CRUD for capability bundles that configure
    # agents with specific tools, MCP servers, and system prompt overrides.
    #
    # All mutations write to the vault markdown file first; the vault
    # watcher syncs changes to the database.  For immediate feedback, an
    # explicit sync_profile_text_to_db call is made after the file write.
    # -----------------------------------------------------------------------

    def _vault_profile_path(self, profile_id: str) -> str:
        """Return the vault file path for a profile's markdown definition."""
        return os.path.join(self.config.data_dir, "vault", "agent-types", profile_id, "profile.md")

    async def _cmd_list_profiles(self, args: dict) -> dict:
        profiles = await self.db.list_profiles()
        if not profiles:
            return {"profiles": [], "count": 0}
        return {
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "model": p.model or "(default)",
                    "allowed_tools": p.allowed_tools,
                    "mcp_servers": list(p.mcp_servers.keys()) if p.mcp_servers else [],
                    "has_system_prompt": bool(p.system_prompt_suffix),
                }
                for p in profiles
            ],
            "count": len(profiles),
        }

    async def _cmd_create_profile(self, args: dict) -> dict:
        from pathlib import Path

        from src.profiles.parser import agent_profile_to_markdown
        from src.profiles.sync import sync_profile_text_to_db
        from src.vault import copy_starter_knowledge, ensure_vault_profile_dirs

        profile_id = args.get("id", "").strip()
        name = args.get("name", "").strip()
        if not profile_id:
            return {"error": "Profile id is required"}
        if not name:
            return {"error": "Profile name is required"}

        # Check for duplicates in both vault and DB.
        vault_path = self._vault_profile_path(profile_id)
        if os.path.isfile(vault_path):
            return {"error": f"Profile '{profile_id}' already exists"}
        existing = await self.db.get_profile(profile_id)
        if existing:
            return {"error": f"Profile '{profile_id}' already exists"}

        # Create vault subdirectories for the new profile (vault spec §2).
        ensure_vault_profile_dirs(self.config.data_dir, profile_id)

        # Copy starter knowledge pack if one exists for this profile type
        # (profiles spec §4, roadmap 4.3.4).
        starter_result = copy_starter_knowledge(self.config.data_dir, profile_id)

        # Render and write the markdown profile to the vault.
        markdown = agent_profile_to_markdown(
            id=profile_id,
            name=name,
            description=args.get("description", ""),
            model=args.get("model", ""),
            permission_mode=args.get("permission_mode", ""),
            allowed_tools=args.get("allowed_tools", []),
            mcp_servers=args.get("mcp_servers", {}),
            system_prompt_suffix=args.get("system_prompt_suffix", ""),
            install=args.get("install", {}),
        )
        Path(vault_path).write_text(markdown, encoding="utf-8")

        # Immediate sync: parse the file and upsert to DB so the caller
        # gets instant confirmation.  The vault watcher will also pick up
        # the change (idempotent upsert).
        sync_result = await sync_profile_text_to_db(
            markdown, self.db, source_path=vault_path, fallback_id=profile_id
        )
        if not sync_result.success:
            return {
                "error": (f"Profile file written to vault but DB sync failed: {sync_result.errors}")
            }

        # V1 ensure_agent_type_collection removed (roadmap 8.6).
        # Agent-type collections are now managed by MemoryV2Plugin.

        result: dict = {"created": profile_id, "name": name}
        if starter_result["copied"]:
            result["starter_knowledge"] = starter_result["copied"]
        if sync_result.warnings:
            result["warnings"] = sync_result.warnings
        return result

    async def _cmd_get_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}
        return {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "model": profile.model or "(default)",
            "permission_mode": profile.permission_mode or "(default)",
            "allowed_tools": profile.allowed_tools,
            "mcp_servers": profile.mcp_servers,
            "system_prompt_suffix": profile.system_prompt_suffix or "(none)",
            "install": profile.install,
        }

    async def _cmd_edit_profile(self, args: dict) -> dict:
        from pathlib import Path

        from src.profiles.parser import (
            agent_profile_to_markdown,
            parse_profile,
            parsed_profile_to_agent_profile,
        )
        from src.profiles.sync import sync_profile_text_to_db
        from src.vault import ensure_vault_profile_dirs

        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        updates: dict = {}
        for fld in (
            "name",
            "description",
            "model",
            "permission_mode",
            "allowed_tools",
            "mcp_servers",
            "system_prompt_suffix",
            "install",
        ):
            if fld in args:
                updates[fld] = args[fld]
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, description, model, "
                    "permission_mode, allowed_tools, mcp_servers, "
                    "system_prompt_suffix, or install."
                )
            }

        # Load current state: prefer vault file, fall back to DB.
        vault_path = self._vault_profile_path(profile_id)
        current: dict = {}
        role = ""
        rules = ""
        reflection = ""

        if os.path.isfile(vault_path):
            text = Path(vault_path).read_text(encoding="utf-8")
            parsed = parse_profile(text)
            current = parsed_profile_to_agent_profile(parsed)
            role = parsed.role
            rules = parsed.rules
            reflection = parsed.reflection
        else:
            # No vault file yet — reconstruct from DB.
            profile = await self.db.get_profile(profile_id)
            if not profile:
                return {"error": f"Profile '{profile_id}' not found"}
            current = {
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "model": profile.model,
                "permission_mode": profile.permission_mode,
                "allowed_tools": profile.allowed_tools,
                "mcp_servers": profile.mcp_servers,
                "system_prompt_suffix": profile.system_prompt_suffix,
                "install": profile.install,
            }

        # Apply updates on top of the current state.
        merged = {**current, **updates}

        # If the user is updating system_prompt_suffix, clear the
        # individual prompt section overrides so the new suffix is used.
        if "system_prompt_suffix" in updates:
            role = ""
            rules = ""
            reflection = ""

        # Ensure vault dirs exist (handles DB-only → vault migration).
        ensure_vault_profile_dirs(self.config.data_dir, profile_id)

        # Re-render and write the updated markdown.
        markdown = agent_profile_to_markdown(
            id=merged.get("id", profile_id),
            name=merged.get("name", profile_id),
            description=merged.get("description", ""),
            model=merged.get("model", ""),
            permission_mode=merged.get("permission_mode", ""),
            allowed_tools=merged.get("allowed_tools", []),
            mcp_servers=merged.get("mcp_servers", {}),
            system_prompt_suffix=merged.get("system_prompt_suffix", ""),
            install=merged.get("install", {}),
            role=role,
            rules=rules,
            reflection=reflection,
        )
        Path(vault_path).write_text(markdown, encoding="utf-8")

        # Immediate sync to DB.
        sync_result = await sync_profile_text_to_db(
            markdown, self.db, source_path=vault_path, fallback_id=profile_id
        )

        result: dict = {"updated": profile_id, "fields": list(updates.keys())}
        if sync_result.warnings:
            result["warnings"] = sync_result.warnings
        if not sync_result.success:
            result["sync_errors"] = sync_result.errors
        return result

    async def _cmd_delete_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        # Check both vault file and DB for the profile.
        vault_path = self._vault_profile_path(profile_id)
        vault_exists = os.path.isfile(vault_path)
        profile = await self.db.get_profile(profile_id)

        if not profile and not vault_exists:
            return {"error": f"Profile '{profile_id}' not found"}

        name = profile.name if profile else profile_id

        # Remove the vault markdown file (the watcher retains DB rows on
        # file deletion, so we explicitly delete from DB as well).
        if vault_exists:
            os.remove(vault_path)

        # Delete from DB (cascade-clears task/project FK references).
        if profile:
            await self.db.delete_profile(profile_id)

        return {"deleted": profile_id, "name": name}

    # --- Discovery commands ------------------------------------------------

    async def _cmd_list_available_tools(self, args: dict) -> dict:
        from src.known_tools import CLAUDE_CODE_TOOLS, KNOWN_MCP_SERVERS

        tools = [
            {"name": name, "description": desc} for name, desc in sorted(CLAUDE_CODE_TOOLS.items())
        ]
        mcp_servers = [
            {
                "name": name,
                "description": info["description"],
                "npm_package": info.get("npm_package", ""),
            }
            for name, info in sorted(KNOWN_MCP_SERVERS.items())
        ]
        return {"tools": tools, "mcp_servers": mcp_servers}

    # --- Install manifest commands -----------------------------------------

    async def _cmd_check_profile(self, args: dict) -> dict:
        import shutil
        from src.known_tools import InstallManifest

        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        manifest = InstallManifest.from_dict(profile.install)
        issues: list[str] = []

        # Check commands via shutil.which
        for cmd in manifest.commands:
            if not shutil.which(cmd):
                issues.append(f"Command not found: {cmd}")

        # Check npm packages
        for pkg in manifest.npm:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "list",
                    "-g",
                    pkg,
                    "--depth=0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode != 0:
                    issues.append(f"npm package not installed: {pkg}")
            except FileNotFoundError:
                issues.append(f"npm not available — cannot check: {pkg}")

        # Check pip packages
        for pkg in manifest.pip:
            try:
                import importlib.metadata

                importlib.metadata.version(pkg)
            except Exception:
                issues.append(f"pip package not installed: {pkg}")

        return {
            "profile_id": profile_id,
            "valid": len(issues) == 0,
            "issues": issues,
            "manifest": manifest.to_dict(),
        }

    async def _cmd_install_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        # Run check first
        check = await self._cmd_check_profile({"profile_id": profile_id})
        if "error" in check:
            return check

        profile = await self.db.get_profile(profile_id)
        from src.known_tools import InstallManifest

        manifest = InstallManifest.from_dict(profile.install)

        if manifest.is_empty:
            return {
                "profile_id": profile_id,
                "installed": [],
                "already_present": [],
                "manual": [],
                "ready": True,
            }

        return await self._install_manifest(profile_id, manifest)

    async def _install_manifest(
        self,
        profile_id: str,
        manifest: "Any",  # noqa: F821 — type was removed, keeping signature stable
    ) -> dict:
        """Shared logic for installing an InstallManifest's dependencies."""
        import shutil

        installed: list[str] = []
        already_present: list[str] = []
        manual: list[str] = []

        # Install npm packages
        for pkg in manifest.npm:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "list",
                    "-g",
                    pkg,
                    "--depth=0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    already_present.append(f"npm:{pkg}")
                    continue
            except FileNotFoundError:
                manual.append(f"npm not available — install manually: {pkg}")
                continue

            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "install",
                    "-g",
                    pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    installed.append(f"npm:{pkg}")
                else:
                    stderr = await proc.stderr.read()
                    manual.append(f"npm install failed for {pkg}: {stderr.decode().strip()}")
            except Exception as e:
                manual.append(f"npm install failed for {pkg}: {e}")

        # Install pip packages
        for pkg in manifest.pip:
            try:
                import importlib.metadata

                importlib.metadata.version(pkg)
                already_present.append(f"pip:{pkg}")
                continue
            except Exception:
                pass

            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip",
                    "install",
                    pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    installed.append(f"pip:{pkg}")
                else:
                    stderr = await proc.stderr.read()
                    manual.append(f"pip install failed for {pkg}: {stderr.decode().strip()}")
            except Exception as e:
                manual.append(f"pip install failed for {pkg}: {e}")

        # Check commands — can't auto-install system binaries
        for cmd in manifest.commands:
            if shutil.which(cmd):
                already_present.append(f"cmd:{cmd}")
            else:
                manual.append(f"Command not found (install manually): {cmd}")

        ready = len(manual) == 0
        return {
            "profile_id": profile_id,
            "installed": installed,
            "already_present": already_present,
            "manual": manual,
            "ready": ready,
        }

    # --- Export / import commands ------------------------------------------

    async def _cmd_export_profile(self, args: dict) -> dict:
        import yaml as _yaml

        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        data: dict = {
            "id": profile.id,
            "name": profile.name,
        }
        if profile.description:
            data["description"] = profile.description
        if profile.model:
            data["model"] = profile.model
        if profile.permission_mode:
            data["permission_mode"] = profile.permission_mode
        if profile.allowed_tools:
            data["allowed_tools"] = profile.allowed_tools
        if profile.mcp_servers:
            data["mcp_servers"] = profile.mcp_servers
        if profile.system_prompt_suffix:
            data["system_prompt_suffix"] = profile.system_prompt_suffix
        if profile.install:
            data["install"] = profile.install

        yaml_text = f"# Agent Profile: {profile.name}\n"
        yaml_text += _yaml.dump(
            {"agent_profile": data},
            default_flow_style=False,
            sort_keys=False,
        )

        result: dict = {"yaml": yaml_text}

        # Optionally create a GitHub gist
        if args.get("create_gist"):
            import tempfile

            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".yaml",
                    delete=False,
                    prefix=f"agent-profile-{profile_id}-",
                ) as f:
                    f.write(yaml_text)
                    tmp_path = f.name

                env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    "gh",
                    "gist",
                    "create",
                    "--public",
                    "--desc",
                    f"Agent Profile: {profile.name}",
                    tmp_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    result["gist_url"] = stdout.decode().strip()
                else:
                    result["gist_error"] = stderr.decode().strip()
            except FileNotFoundError:
                result["gist_error"] = "gh CLI not found — install GitHub CLI"
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        return result

    async def _cmd_import_profile(self, args: dict) -> dict:
        from pathlib import Path

        import yaml as _yaml

        from src.known_tools import InstallManifest
        from src.profiles.parser import agent_profile_to_markdown
        from src.profiles.sync import sync_profile_text_to_db
        from src.vault import ensure_vault_profile_dirs

        source = args.get("source", "").strip()
        if not source:
            return {"error": "source is required (YAML text or gist URL)"}

        # If source looks like a URL, fetch via gh gist
        yaml_text = source
        if source.startswith("http://") or source.startswith("https://"):
            gist_id = source.rstrip("/").split("/")[-1]
            try:
                env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    "gh",
                    "gist",
                    "view",
                    gist_id,
                    "--raw",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    return {"error": f"Failed to fetch gist: {stderr.decode().strip()}"}
                yaml_text = stdout.decode()
            except FileNotFoundError:
                return {"error": "gh CLI not found — install GitHub CLI to import from URLs"}

        try:
            data = _yaml.safe_load(yaml_text)
        except Exception as e:
            return {"error": f"Invalid YAML: {e}"}

        if not isinstance(data, dict) or "agent_profile" not in data:
            return {"error": "YAML must contain an 'agent_profile' key"}

        pdata = data["agent_profile"]
        if not isinstance(pdata, dict):
            return {"error": "agent_profile must be a mapping"}

        profile_id = args.get("id") or pdata.get("id", "")
        if not profile_id:
            return {"error": "Profile must have an 'id' field"}

        overwrite = args.get("overwrite", False)
        vault_path = self._vault_profile_path(profile_id)
        vault_exists = os.path.isfile(vault_path)
        existing = await self.db.get_profile(profile_id)
        if (existing or vault_exists) and not overwrite:
            return {
                "error": f"Profile '{profile_id}' already exists (use overwrite=true to replace)"
            }

        profile_name = args.get("name") or pdata.get("name", profile_id)

        # Ensure vault directories.
        ensure_vault_profile_dirs(self.config.data_dir, profile_id)

        # Render imported data as vault markdown.
        markdown = agent_profile_to_markdown(
            id=profile_id,
            name=profile_name,
            description=pdata.get("description", ""),
            model=pdata.get("model", ""),
            permission_mode=pdata.get("permission_mode", ""),
            allowed_tools=pdata.get("allowed_tools", []),
            mcp_servers=pdata.get("mcp_servers", {}),
            system_prompt_suffix=pdata.get("system_prompt_suffix", ""),
            install=pdata.get("install", {}),
        )
        Path(vault_path).write_text(markdown, encoding="utf-8")

        # Immediate sync to DB.
        sync_result = await sync_profile_text_to_db(
            markdown, self.db, source_path=vault_path, fallback_id=profile_id
        )

        result: dict = {"imported": True, "name": profile_name, "id": profile_id}
        if sync_result.warnings:
            result["warnings"] = sync_result.warnings

        # Auto-install dependencies if manifest is non-empty
        install_data = pdata.get("install", {})
        manifest = InstallManifest.from_dict(install_data)
        if not manifest.is_empty:
            install_result = await self._install_manifest(profile_id, manifest)
            result["installed"] = install_result["installed"]
            result["already_present"] = install_result["already_present"]
            result["manual"] = install_result["manual"]
            result["ready"] = install_result["ready"]
        else:
            result["ready"] = True

        return result

    async def _cmd_migrate_profiles(self, args: dict) -> dict:
        """Migrate DB-only profiles to vault markdown files.

        Reads all profiles from the agent_profiles table and writes them
        as hybrid-format markdown files in the vault.  Profiles that already
        have a vault file are skipped (unless force=True).

        Args:
            dry_run (bool): Preview what would be migrated without writing.
            verify (bool): Verify round-trip fidelity after writing (default True).
            force (bool): Overwrite existing vault files (default False).

        Returns:
            Migration report dict with written/skipped/error counts.
        """
        from src.profiles.migration import migrate_db_profiles_to_vault

        dry_run = args.get("dry_run", False)
        verify = args.get("verify", True)
        force = args.get("force", False)

        report = await migrate_db_profiles_to_vault(
            self.db,
            self.config.data_dir,
            dry_run=dry_run,
            verify=verify,
            force=force,
        )
        return report.to_dict()
