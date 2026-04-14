"""Context mixin — task context assembly helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.models import Task

logger = logging.getLogger(__name__)


class ContextMixin:
    """Task context assembly methods mixed into Orchestrator."""

    def _get_execution_rules(
        self,
        *,
        task,
        branch_name: str,
        default_branch: str,
        has_remote: bool,
        is_final_subtask: bool,
        requires_approval: bool,
    ) -> str:
        """Return execution rules tailored to the task type and git context.

        Produces three prompt variants:
        A) Normal / final subtask (no approval) — branch, merge to default, push
        B) Requires approval — branch, push, create PR
        C) Intermediate subtask — branch, commit, stay on branch
        """
        # -- Non-git execution rules (shared) --
        if task.is_plan_subtask:
            behaviour = (
                "## Important: Execution Rules\n"
                "You are running autonomously — there is NO interactive user.\n"
                "Do NOT use plan mode or EnterPlanMode.\n"
                "Do NOT write implementation plans or plan files.\n"
                "Your task is one step of an existing implementation plan — "
                "write code, not plans.\n"
                "Implement the changes described below DIRECTLY.\n"
                "If you encounter ambiguity, make reasonable decisions and "
                "document in code comments."
            )
        else:
            behaviour = (
                "## Important: Execution Rules\n"
                "You are running autonomously — there is NO interactive user "
                "to approve plans.\n"
                "Do NOT use plan mode or EnterPlanMode. "
                "Implement the changes DIRECTLY.\n"
                "If the task description contains a plan, execute it "
                "immediately — do not re-plan."
            )

        # -- Git workflow rules (varies by scenario) --
        if not branch_name:
            # No branch assigned (e.g. no repo configured) — skip git rules
            git_rules = ""
        elif task.is_plan_subtask and not is_final_subtask:
            # Intermediate subtask: commit on shared branch, don't merge
            git_rules = (
                f"\n\n## Important: Git Workflow (Subtask — more steps follow)\n"
                f"Shared branch: `{branch_name}`. "
                f"Default branch: `{default_branch}`.\n"
                f"\nWhen you start:\n"
                f"1. Switch to the task branch: `git checkout {branch_name}`\n"
                f"   If it does not exist yet: "
                f"`git checkout -b {branch_name}`\n"
                f"\nWhen you finish:\n"
                f"1. `git add` the files you changed\n"
                f"2. `git commit` with a descriptive message\n"
                f"3. Stay on `{branch_name}` — do NOT merge to "
                f"`{default_branch}`. A later subtask handles the final merge."
            )
        elif requires_approval:
            # PR workflow: push branch, create PR, don't merge
            push_cmd = f"`git push origin {branch_name}`"
            pr_cmd = (
                f"`gh pr create --base {default_branch} "
                f"--head {branch_name} "
                f'--title "<descriptive title>" '
                f'--body "<summary of changes>"`'
            )
            git_rules = (
                f"\n\n## Important: Git Workflow (PR Required)\n"
                f"Default branch: `{default_branch}`. "
                f"Task branch: `{branch_name}`.\n"
                f"\nWhen you start:\n"
                f"1. `git checkout -b {branch_name}` "
                f"(or `git checkout {branch_name}` if it already exists)\n"
                f"\nWhen you finish (if you made code changes):\n"
                f"1. Commit all remaining changes on `{branch_name}`\n"
                f"2. Push your branch: {push_cmd}\n"
                f"3. Create a pull request: {pr_cmd}\n"
                f"4. Stay on `{branch_name}` — do NOT merge to "
                f"`{default_branch}`\n"
                f"\nIf this task requires NO code changes "
                f"(research, analysis, investigation):\n"
                f"- Do not create a branch. Stay on `{default_branch}`.\n"
                f"- Do not create a PR."
            )
        else:
            # Normal task / final subtask: merge to default + push
            push_line = f"4. `git push origin {default_branch}`\n" if has_remote else ""
            delete_step = "5" if has_remote else "4"
            git_rules = (
                f"\n\n## Important: Git Workflow\n"
                f"Default branch: `{default_branch}`. "
                f"Task branch: `{branch_name}`.\n"
                f"\nWhen you start:\n"
                f"1. `git checkout -b {branch_name}` "
                f"(or `git checkout {branch_name}` if it already exists)\n"
                f"\nWhen you finish (if you made code changes):\n"
                f"1. Commit all remaining changes on `{branch_name}`\n"
                f"2. `git checkout {default_branch}`\n"
                f"3. `git merge {branch_name}` — resolve any conflicts "
                f"during the merge\n"
                f"{push_line}"
                f"{delete_step}. "
                f"`git branch -d {branch_name}`\n"
                f"\nIf this task requires NO code changes "
                f"(research, analysis, investigation):\n"
                f"- Do not create a branch. Stay on `{default_branch}`.\n"
                f"\nIf you encounter merge conflicts:\n"
                f"- Resolve them during the merge step. You wrote the code — "
                f"you are best positioned to resolve conflicts.\n"
                f"- After resolving, complete the merge commit"
                f"{' and push' if has_remote else ''}."
            )

        # -- Plan-writing rules (root tasks only) --
        plan_rules = ""
        if not task.is_plan_subtask:
            plan_rules = (
                "\n\n## CRITICAL: Writing Implementation Plans\n"
                "Most tasks do NOT require writing a plan — just implement "
                "the changes directly.\n"
                "Only write a plan if the task explicitly asks you to create "
                "an implementation plan,\n"
                "investigate and propose changes, or produce a multi-step "
                "strategy for follow-up work.\n"
                "\n"
                "If you DO need to write a plan, you MUST follow these rules "
                "exactly:\n"
                "1. Write the plan to **`.claude/plan.md`** in the workspace "
                "root (preferred)\n"
                "   or `plan.md` — these are the ONLY locations the system "
                "checks first\n"
                "2. Do NOT write plans to `notes/`, `docs/`, or any other "
                "directory — plans\n"
                "   written elsewhere may not be detected for automatic task "
                "splitting\n"
                "3. Name each implementation phase clearly: "
                "`## Phase 1: <title>`,\n"
                "   `## Phase 2: <title>`, etc.\n"
                "4. Put ALL background/reference material (design specs, "
                "constraints,\n"
                "   architecture notes) BEFORE the phase headings, NOT as "
                "separate phases\n"
                "5. Keep each phase focused on a single actionable "
                "implementation step\n"
                "6. If you implement the plan yourself (i.e., you both plan "
                "AND execute the work\n"
                "   in a single task), DELETE the plan file before completing. "
                "Only leave a plan\n"
                "   file in the workspace if you want the system to create "
                "follow-up tasks from it.\n"
                "   Alternatively, add `auto_tasks: false` to the plan's YAML "
                "frontmatter.\n"
                "\n"
                "NOTE: Any plan file left in the workspace when your task "
                "completes will be\n"
                "automatically parsed and converted into follow-up subtasks. "
                "If you already\n"
                "did the work described in the plan, this creates "
                "duplicate/unnecessary tasks.\n"
                "\n"
                "This is required for the system to automatically split your "
                "plan into\n"
                "follow-up tasks. Plans that mix reference sections with "
                "implementation\n"
                "phases will produce low-quality task splits."
            )

        return behaviour + git_rules + plan_rules

    async def _build_task_context_with_prompt_builder(
        self,
        task,
        workspace: str,
        project,
        profile,
    ) -> str:
        """Build the task description using PromptBuilder.

        Replaces the inline string concatenation that was in _execute_task().
        """
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder(project_id=task.project_id)

        # System metadata
        sys_parts = [f"- Workspace directory: {workspace}"]
        if project:
            sys_parts.append(f"- Project: {project.name} (id: {project.id})")
            repo_url = project.repo_url
            # Auto-detect repo_url from git remote if not set
            if not repo_url:
                try:
                    detected = await self.git.aget_remote_url(workspace)
                    if detected:
                        repo_url = detected
                        await self.db.update_project(project.id, repo_url=repo_url)
                except Exception:
                    pass  # Non-fatal
            if repo_url:
                sys_parts.append(f"- Repository URL: {repo_url}")
            if project.repo_default_branch:
                sys_parts.append(f"- Default branch: {project.repo_default_branch}")
        if task.branch_name:
            sys_parts.append(f"- Git branch: {task.branch_name}")
        builder.add_context("system_context", "## System Context\n" + "\n".join(sys_parts))

        # Execution rules — parameterized by git context
        default_branch = await self._get_default_branch(project, workspace)
        has_remote = (
            await self.git.ahas_remote(workspace)
            if await self.git.avalidate_checkout(workspace)
            else False
        )
        is_final = (not task.is_plan_subtask) or await self._is_last_subtask(task)
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            needs_approval = parent.requires_approval if parent else task.requires_approval
        else:
            needs_approval = task.requires_approval
        builder.add_context(
            "execution_rules",
            self._get_execution_rules(
                task=task,
                branch_name=task.branch_name or "",
                default_branch=default_branch,
                has_remote=has_remote,
                is_final_subtask=is_final,
                requires_approval=needs_approval,
            ),
        )

        # Upstream dependency summaries
        dep_ids = await self.db.get_dependencies(task.id)
        if dep_ids:
            dep_sections = []
            for dep_id in sorted(dep_ids):
                dep_task = await self.db.get_task(dep_id)
                dep_result = await self.db.get_task_result(dep_id)
                if not dep_task or not dep_result:
                    continue
                title = dep_task.title or dep_id
                summary = dep_result.get("summary") or "(no summary recorded)"
                # Full summary preserved — token budgeting is handled by prompt_builder
                files = dep_result.get("files_changed") or []
                section = f"### {title}\n**Summary:** {summary}"
                if files:
                    file_list = "\n".join(f"  - `{f}`" for f in files)
                    section += f"\n**Files changed:**\n{file_list}"
                dep_sections.append(section)
            if dep_sections:
                builder.add_context(
                    "upstream_work",
                    "## Completed Upstream Work\n"
                    "The following tasks were direct dependencies of your task "
                    "and have already been completed:\n\n" + "\n\n".join(dep_sections),
                )

        # L0 Identity tier and L1 Critical Facts tier are now injected via
        # TaskContext fields (l0_role, l1_facts) and handled by the adapter's
        # prompt builder.  See Roadmap 3.3.5.
        #
        # Project-specific override — freeform English that supplements or tweaks
        # the base profile for this project.  Injected right after L0 role so the
        # LLM sees base profile + override together.
        # See docs/specs/design/memory-scoping.md §5.
        if profile and task.project_id:
            override_text = await self._load_project_override(task.project_id, profile.id)
            if override_text:
                builder.set_override_content(override_text)

        # Task description
        builder.add_context("task", f"## Task\n{task.description}")

        # Conversation thread context — if the supervisor stored the
        # conversation that led to this task's creation, include it so
        # the agent understands the user's intent and broader context.
        try:
            contexts = await self.db.get_task_contexts(task.id)
            conv_ctx = next((c for c in contexts if c["type"] == "conversation_context"), None)
            if conv_ctx and conv_ctx["content"]:
                builder.add_context(
                    "additional_context",
                    "## Additional Context\n"
                    "The following is the conversation thread between the user and "
                    "the supervisor that led to the creation of this task. Use it to "
                    "understand the user's intent, any clarifications, and the "
                    "broader goal behind the task:\n\n" + conv_ctx["content"],
                )
        except Exception:
            pass  # Non-fatal — proceed without conversation context

        return builder.build_task_prompt()

    async def _load_project_override(
        self,
        project_id: str,
        agent_type: str,
    ) -> str | None:
        """Load override ``.md`` file content for a project + agent type.

        Override files live at
        ``vault/projects/{project_id}/overrides/{agent_type}.md``.
        Returns the raw file content (including frontmatter — the caller
        is responsible for stripping it), or ``None`` if no override exists.

        See ``docs/specs/design/memory-scoping.md`` §5 for the override model.
        """
        override_path = os.path.join(
            self.config.vault_root,
            "projects",
            project_id,
            "overrides",
            f"{agent_type}.md",
        )
        if not os.path.isfile(override_path):
            return None
        try:
            with open(override_path, encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                logger.info(
                    "Loaded override for project=%s agent_type=%s from %s",
                    project_id,
                    agent_type,
                    override_path,
                )
                return content
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Failed to load override for project=%s agent_type=%s: %s",
                project_id,
                agent_type,
                exc,
            )
        return None
