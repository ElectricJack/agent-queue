"""Scoped question commands; actor identity always comes from server scope."""

from __future__ import annotations


class QuestionCommandsMixin:
    async def _question_identity(self):
        scope = self._current_scope
        if not isinstance(scope, dict):
            return {"error": "out of scope: explicit server caller identity is required"}
        if scope.get("kind") == "local":
            return {"human": True, "actor": "local", "project_id": None}
        if scope.get("kind") != "session" or not scope.get("elevated"):
            return {"error": "out of scope: questions require a human or elevated supervisor"}
        row = await self.db.get_session(scope.get("session_id"))
        if (
            row is None
            or row.profile_id != "supervisor"
            or row.lifecycle != "named"
            or row.state not in ("starting", "running", "draining")
            or row.desired_state != "running"
            or row.project_id != scope.get("project_id")
        ):
            return {"error": "out of scope: a live supervisor session is required"}
        return {"human": False, "actor": "session:" + row.id, "project_id": row.project_id}

    async def _question_for_caller(self, question_id, identity):
        if not isinstance(question_id, str) or not question_id:
            return {"error": "question_id is required"}
        question = await self.db.get_agent_question(question_id)
        if question is None:
            return {"error": "question not found"}
        project_id = identity["project_id"]
        if project_id is not None and question["project_id"] != project_id:
            return {"error": "out of scope: question belongs to another project"}
        return question

    async def _cmd_question_list(self, args):
        identity = await self._question_identity()
        if "error" in identity:
            return identity
        project_id = args.get("project_id") or identity["project_id"]
        if identity["project_id"] is not None and project_id != identity["project_id"]:
            return {"error": "out of scope: question belongs to another project"}
        questions = await self.db.list_agent_questions(project_id=project_id)
        return {"questions": questions, "count": len(questions)}

    async def _cmd_question_answer(self, args):
        identity = await self._question_identity()
        if "error" in identity:
            return identity
        question = await self._question_for_caller(args.get("question_id"), identity)
        if "error" in question:
            return question
        return await self.orchestrator.agent_questions.answer(
            question["id"], args.get("body"), actor=identity["actor"], human=identity["human"]
        )

    async def _cmd_question_escalate(self, args):
        identity = await self._question_identity()
        if "error" in identity:
            return identity
        question = await self._question_for_caller(args.get("question_id"), identity)
        if "error" in question:
            return question
        return await self.orchestrator.agent_questions.escalate(question["id"], args.get("reason"))
