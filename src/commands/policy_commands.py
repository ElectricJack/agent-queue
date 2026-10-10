"""Policy-profile operator commands. Every mutation stays in CommandHandler."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from src.commands.principal import PrincipalKind, current_principal
from src.policy_profiles.archive import files, pack, read_bundle, write_bundle
from src.policy_profiles.models import (
    Bundle,
    ExportRequest,
    ImportRequest,
    PolicyAgentSettings,
    PlaybookPolicy,
    PromotionPolicy,
    RoutingPreferences,
    checksum,
)
from src.policy_profiles.service import PolicyFiles, apply_files, item


class PolicyCommandsMixin:
    def _policy_authority(self) -> dict | None:
        principal = current_principal()
        if principal is not None and principal.kind is not PrincipalKind.LOCAL:
            return {"success": False, "error": "Policy profiles require the local operator"}
        return None

    async def _policy_files(self, project_id: str) -> PolicyFiles:
        project = await self.db.get_project(project_id)
        if project is None:
            raise ValueError(f"Project {project_id!r} not found")
        workspace = await self.db.get_project_workspace_path(project_id)
        return PolicyFiles(Path(self.config.vault_root), project, workspace)

    async def _policy_export_bundle(self, adapter: PolicyFiles, name: str | None) -> Bundle:
        from sqlalchemy import select
        from src.database.tables import projects
        from src.playbooks.artifact_store import ArtifactStore
        from src.playbooks.definition import referenced_profile_ids
        from src.policy_profiles.service import contained, read_profile

        entries = []
        activations = await self.db.list_playbook_activations()
        # Enabled system routines form the project's default pipeline. Project
        # activations and the bound router are also dependencies by construction.
        all_rows = {
            row["playbook_id"]: row
            for row in activations
            if row["scope"] == "system"
            or row["scope"] == "project"
            and row["scope_identifier"] == adapter.project.id
        }
        relevant = {identifier: row for identifier, row in all_rows.items() if row["enabled"]}
        bound = adapter.project.assignment_playbook_id
        if bound not in relevant and bound in all_rows:
            relevant[bound] = all_rows[bound]
        store = ArtifactStore(self.config.compiled_root)
        profiles = set()
        visited = set()
        queue = list(relevant.values())
        while queue:
            row = queue.pop(0)
            if row["playbook_id"] in visited:
                continue
            visited.add(row["playbook_id"])
            sha = row["active_artifact_sha256"]
            if not sha:
                raise ValueError(
                    f"Required playbook {row['playbook_id']!r} has no reviewed artifact"
                )
            definition = await asyncio.to_thread(store.load, sha)
            source = await asyncio.to_thread(store.load_source, sha)
            dependencies = set()

            def collect(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "playbook_id":
                            reference = (
                                child.get("value")
                                if isinstance(child, dict) and child.get("type") == "literal"
                                else child
                            )
                            if isinstance(reference, str):
                                dependencies.add(reference)
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            artifact = definition.model_dump(mode="json", exclude_none=True)
            collect(artifact["steps"])
            for dependency in dependencies - visited:
                if dependency not in all_rows:
                    raise ValueError(
                        f"Required playbook {dependency!r} has no reviewed active artifact"
                    )
                queue.append(all_rows[dependency])
            entry_name = (
                definition.id.removeprefix(adapter.project.id + ".")
                if row["scope"] == "project"
                else definition.id
            )
            entries.append(
                item(
                    entry_name,
                    "project" if row["scope"] == "project" else "system",
                    PlaybookPolicy(
                        source=source, artifact=artifact, dependencies=sorted(dependencies)
                    ),
                )
            )
            profiles.update(referenced_profile_ids(definition))
        routing = None
        for entry in entries:
            if entry.type == "playbook" and entry.payload.artifact["id"] == bound:
                for step in entry.payload.artifact["steps"].values():
                    if step.get("command") == "task_route_plan":
                        import yaml

                        policy = step.get("inputs", {}).get("policy")
                        if isinstance(policy, dict) and policy.get("type") == "literal":
                            policy = policy.get("value")
                        if isinstance(policy, str):
                            routing = yaml.safe_load(policy)
                            break
        entries.append(
            item(
                "preferences",
                "project",
                RoutingPreferences(assignment_playbook_id=bound, policy=routing),
            )
        )
        async with self.db._engine.connect() as conn:
            flow = (
                await conn.execute(
                    select(projects.c.promotion_flow).where(projects.c.id == adapter.project.id)
                )
            ).scalar_one_or_none()
        if flow:
            # Human/account identities are not promotion policy. Gate semantics
            # are preserved; the importing operator supplies local identities.
            portable_flow = json.loads(json.dumps(flow))
            for step in portable_flow:
                step.get("gate", {}).pop("operator_logins", None)
            entries.append(item("flow", "project", PromotionPolicy(flow=portable_flow)))
        local = await asyncio.to_thread(adapter.read_local)
        profiles.update(entry.name for entry in local if entry.type == "agent_settings")
        pending_profiles = sorted(profiles)
        visited_profiles = set()
        while pending_profiles:
            profile_id = pending_profiles.pop(0)
            if profile_id in visited_profiles:
                continue
            visited_profiles.add(profile_id)
            path = contained(adapter.vault, f"agent-types/{profile_id}/profile.md")
            if path.is_file():
                from src.profiles.parser import parse_profile

                parsed = await asyncio.to_thread(lambda: parse_profile(path.read_text()))
                payload = await asyncio.to_thread(read_profile, path)
                # Derived class/harness rungs are generated from templates.
                # Export the authorable ancestor, never a rung snapshot.
                if "derived" not in parsed.frontmatter.tags:
                    entries.append(item(profile_id, "system", payload))
                if payload.extends:
                    pending_profiles.append(payload.extends)
        # Saved drafts and actual local policy take precedence over inherited
        # defaults; one exported item per identity, never history.
        entries.extend(local)
        unique = {entry.id: entry for entry in entries}
        return await asyncio.to_thread(
            adapter.portable, sorted(unique.values(), key=lambda entry: entry.id), name
        )

    async def _cmd_policy_export(self, args: dict) -> dict:
        if refusal := self._policy_authority():
            return refusal
        try:
            request = ExportRequest.model_validate(args)
            adapter = await self._policy_files(request.project_id)
            bundle = await self._policy_export_bundle(adapter, request.name)
            digest = checksum(bundle.model_dump(mode="json"))
            preview_files = await asyncio.to_thread(files, bundle)
            written = []
            if request.expected_checksum is not None:
                if not request.path or request.expected_checksum != digest:
                    raise ValueError("Export preview changed; preview again before writing")
                written = await asyncio.to_thread(write_bundle, request.path, bundle)
            return {
                "success": True,
                "bundle": bundle.model_dump(mode="json"),
                "checksum": digest,
                "files": [
                    {"path": path, "content": data.decode()}
                    for path, data in sorted(preview_files.items())
                ],
                "archive": base64.b64encode(await asyncio.to_thread(pack, bundle)).decode(),
                "written": written,
            }
        except (ValueError, OSError, KeyError) as exc:
            return {"success": False, "error": str(exc)}

    async def _policy_import_plan(self, args: dict, *, applying: bool = False):
        request = ImportRequest.model_validate(args)
        adapter = await self._policy_files(request.project_id)
        source = (
            base64.b64decode(request.archive, validate=True)
            if request.archive is not None
            else request.path
        )
        bundle = request.bundle or await asyncio.to_thread(read_bundle, source)
        await asyncio.to_thread(files, bundle)
        materialized = await asyncio.to_thread(
            adapter.materialize, bundle, request.values, require_values=applying
        )
        # A copied dependency follows the placement chosen for that playbook.
        # Skipped dependencies retain their destination-local identifiers.
        from src.policy_profiles.models import PAYLOAD
        from src.policy_profiles.service import transform

        remapping = {}
        for entry in materialized:
            placement = request.selections.get(entry.id)
            if entry.type == "playbook" and placement is not None and placement.scope != "skip":
                remapping[entry.payload.artifact["id"]] = (
                    entry.name
                    if placement.scope == "global"
                    else f"{adapter.project.id}.{entry.name}"
                )
            elif (
                entry.type == "playbook" and entry.original_scope == "project" and placement is None
            ):
                remapping[entry.payload.artifact["id"]] = f"{adapter.project.id}.{entry.name}"
        if remapping:
            from src.playbooks.definition import source_digest

            remapped = []
            for entry in materialized:
                data = transform(entry.payload.model_dump(mode="json"), remapping)
                if entry.type == "playbook":
                    data["artifact"]["source_hash"] = source_digest(data["source"])
                remapped.append(
                    item(entry.name, entry.original_scope, PAYLOAD.validate_python(data))
                )
            materialized = remapped
        rows, planned = await asyncio.to_thread(
            adapter.diff,
            materialized,
            request.selections,
            request.only,
            request.skip,
            request.no_overwrite,
            applying=applying,
        )
        return request, adapter, bundle, materialized, rows, planned

    async def _cmd_policy_diff(self, args: dict) -> dict:
        if refusal := self._policy_authority():
            return refusal
        try:
            request, adapter, bundle, _items, rows, _planned = await self._policy_import_plan(args)
            return {
                "success": True,
                "name": bundle.name,
                "project_id": request.project_id,
                "items": [row.model_dump(mode="json") for row in rows],
                "placeholders": [p.model_dump() for p in bundle.placeholders],
                "values": {
                    key: request.values.get(key, adapter.defaults().get(key, ""))
                    for key in (p.name for p in bundle.placeholders)
                },
            }
        except (ValueError, OSError, KeyError) as exc:
            return {"success": False, "error": str(exc)}

    async def _cmd_policy_apply(self, args: dict) -> dict:
        if refusal := self._policy_authority():
            return refusal
        # One operator import at a time keeps the preview/CAS/write critical
        # section coherent even while filesystem IO yields to a thread.
        lock = getattr(self, "_policy_apply_lock", None)
        if lock is None:
            self._policy_apply_lock = lock = asyncio.Lock()
        async with lock:
            try:
                request, adapter, _bundle, items, rows, planned = await self._policy_import_plan(
                    args, applying=True
                )
                from src.profiles.inheritance import resolve_inheritance
                from src.profiles.parser import parse_profile
                from src.profiles.sync import sync_profile_text_to_db

                global_profiles = {}
                selected_global = {row.id for row in rows if row.selected and row.scope == "global"}
                for entry in items:
                    if (
                        entry.id in selected_global
                        and isinstance(entry.payload, PolicyAgentSettings)
                        and not entry.payload.override
                    ):
                        writes = planned.get(entry.id)
                        if writes is None:
                            writes = await asyncio.to_thread(adapter.writes, entry, "global")
                        for path, content in writes.items():
                            if path.name == "profile.md":
                                global_profiles[path] = content.decode()

                def validate_profiles():
                    def load_template(identifier):
                        from src.policy_profiles.service import contained

                        path = contained(adapter.vault, f"agent-types/{identifier}/profile.md")
                        text = global_profiles.get(path)
                        if text is None:
                            text = path.read_text() if path.is_file() else None
                        return parse_profile(text) if text is not None else None

                    for path, text in global_profiles.items():
                        if (
                            path.is_file()
                            and "derived" in parse_profile(path.read_text()).frontmatter.tags
                        ):
                            raise ValueError("Edit the worker template, not a derived profile")
                        parsed = parse_profile(text)
                        _, errors = resolve_inheritance(parsed, load_template)
                        if not parsed.is_valid or errors:
                            raise ValueError(
                                "invalid global profile: " + "; ".join(parsed.errors + errors)
                            )

                await asyncio.to_thread(validate_profiles)
                await asyncio.to_thread(apply_files, planned)
                for path, text in global_profiles.items():
                    synced = await sync_profile_text_to_db(
                        text, self.db, source_path=str(path), fallback_id=path.parent.name
                    )
                    if not synced.success:
                        return {
                            "success": False,
                            "error": "Imported profile draft retained; profile sync failed: "
                            + "; ".join(synced.errors),
                            "applied": list(planned),
                            "items": [row.model_dump(mode="json") for row in rows],
                        }
                reviews = []
                pending = []
                by_id = {entry.id: entry for entry in items}
                for row in rows:
                    if not row.selected:
                        continue
                    if (
                        row.type in {"promotion_flow", "routing"}
                        or row.type == "agent_settings"
                        and row.scope == "project"
                    ):
                        pending.append(row.id)
                    if row.type == "playbook":
                        entry = by_id[row.id]
                        identifier = (
                            entry.name
                            if row.scope == "global"
                            else f"{adapter.project.id}.{entry.name}"
                        )
                        from src.policy_profiles.service import contained

                        draft = contained(
                            adapter.root(row.scope), f"policy/pending-playbooks/{identifier}"
                        )
                        source = await asyncio.to_thread(contained(draft, "source.md").read_text)
                        from src.playbooks.definition import source_digest

                        receipt_path = contained(draft, "review.json")
                        if receipt_path.is_file():
                            receipt = json.loads(await asyncio.to_thread(receipt_path.read_text))
                            if receipt.get("source_hash") == source_digest(source):
                                reviews.append(
                                    {"item_id": row.id, "review_id": receipt["review_id"]}
                                )
                                continue
                        semantic_body = await asyncio.to_thread(
                            contained(draft, "semantic-body.json").read_text
                        )
                        result = await self.execute(
                            "review_submit",
                            {
                                "project_id": request.project_id,
                                "kind": "other",
                                "title": f"Imported policy playbook: {identifier}",
                                "content": f"# Imported policy: {identifier}\n\nReview before activation.\n\n{source}\n\n## Compiled policy\n```json\n{semantic_body}\n```\n",
                                "playbook_id": identifier,
                                "activate_on_approval": False,
                                "semantic_body": semantic_body,
                            },
                        )
                        if not result.get("success"):
                            return {
                                "success": False,
                                "error": f"Imported files are retained; playbook review failed: {result.get('error')}",
                                "applied": list(planned),
                                "reviews": reviews,
                                "pending_configuration": pending,
                                "items": [item.model_dump(mode="json") for item in rows],
                            }
                        reviews.append({"item_id": row.id, "review_id": result["review_id"]})
                        from src.policy_profiles.models import encoded

                        await asyncio.to_thread(
                            receipt_path.write_bytes,
                            encoded(
                                {
                                    "source_hash": source_digest(source),
                                    "review_id": result["review_id"],
                                }
                            ),
                        )
                return {
                    "success": True,
                    "applied": list(planned),
                    "reviews": reviews,
                    "pending_configuration": pending,
                    "activated": False,
                    "items": [row.model_dump(mode="json") for row in rows],
                }
            except (ValueError, OSError, KeyError) as exc:
                return {"success": False, "error": str(exc)}
