"""Operator and supervisor command boundary for shared decisions."""
from pydantic import ValidationError

from src.commands.contracts.decisions import DecisionListArgs, DecisionRecordArgs
from src.commands.supervisor_authority import operator_or_supervisor
from src.operator_decisions import OperatorDecisions, object_project_on


class DecisionCommandsMixin:
    async def _decision_access(self, request):
        async with self.db._engine.connect() as conn:
            project = await object_project_on(conn, request.object_kind, request.object_id)
        return await operator_or_supervisor(self.db, project, subject="operator decision")

    async def _cmd_decision_record(self, args):
        try:
            request = DecisionRecordArgs.model_validate(args)
            recorder, error = await self._decision_access(request)
            if error:
                return {"success": False, "error": error}
            row = await OperatorDecisions(self.db).record(
                request.model_dump(), recorded_by=recorder,
            )
            return {"success": True, "decision": row}
        except (ValueError, ValidationError) as exc:
            return {"success": False, "error": str(exc)}

    async def _cmd_decision_list(self, args):
        try:
            request = DecisionListArgs.model_validate(args)
            _, error = await self._decision_access(request)
            if error:
                return {"success": False, "error": error}
            rows = await OperatorDecisions(self.db).history(request.object_kind, request.object_id)
            return {"success": True, "operator_decisions": rows}
        except (ValueError, ValidationError) as exc:
            return {"success": False, "error": str(exc)}
