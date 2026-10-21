from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord
    from ..models.job_response_decision_type_0 import JobResponseDecisionType0
    from ..models.job_response_job import JobResponseJob
    from ..models.job_response_wait_type_1 import JobResponseWaitType1


T = TypeVar("T", bound="JobResponse")


@_attrs_define
class JobResponse:
    """
    Attributes:
        job (JobResponseJob):
        wait (AgentWaitRecord | JobResponseWaitType1 | None | Unset):
        wait_id (None | str | Unset):
        decision (JobResponseDecisionType0 | None | Unset):
        continuation_id (None | str | Unset):
        generation (int | None | Unset):
        next_step (None | str | Unset):
        success (bool | Unset):  Default: True.
    """

    job: JobResponseJob
    wait: AgentWaitRecord | JobResponseWaitType1 | None | Unset = UNSET
    wait_id: None | str | Unset = UNSET
    decision: JobResponseDecisionType0 | None | Unset = UNSET
    continuation_id: None | str | Unset = UNSET
    generation: int | None | Unset = UNSET
    next_step: None | str | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_wait_record import AgentWaitRecord
        from ..models.job_response_decision_type_0 import JobResponseDecisionType0
        from ..models.job_response_wait_type_1 import JobResponseWaitType1

        job = self.job.to_dict()

        wait: dict[str, Any] | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        elif isinstance(self.wait, AgentWaitRecord):
            wait = self.wait.to_dict()
        elif isinstance(self.wait, JobResponseWaitType1):
            wait = self.wait.to_dict()
        else:
            wait = self.wait

        wait_id: None | str | Unset
        if isinstance(self.wait_id, Unset):
            wait_id = UNSET
        else:
            wait_id = self.wait_id

        decision: dict[str, Any] | None | Unset
        if isinstance(self.decision, Unset):
            decision = UNSET
        elif isinstance(self.decision, JobResponseDecisionType0):
            decision = self.decision.to_dict()
        else:
            decision = self.decision

        continuation_id: None | str | Unset
        if isinstance(self.continuation_id, Unset):
            continuation_id = UNSET
        else:
            continuation_id = self.continuation_id

        generation: int | None | Unset
        if isinstance(self.generation, Unset):
            generation = UNSET
        else:
            generation = self.generation

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job": job,
            }
        )
        if wait is not UNSET:
            field_dict["wait"] = wait
        if wait_id is not UNSET:
            field_dict["wait_id"] = wait_id
        if decision is not UNSET:
            field_dict["decision"] = decision
        if continuation_id is not UNSET:
            field_dict["continuation_id"] = continuation_id
        if generation is not UNSET:
            field_dict["generation"] = generation
        if next_step is not UNSET:
            field_dict["next_step"] = next_step
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record import AgentWaitRecord
        from ..models.job_response_decision_type_0 import JobResponseDecisionType0
        from ..models.job_response_job import JobResponseJob
        from ..models.job_response_wait_type_1 import JobResponseWaitType1

        d = dict(src_dict)
        job = JobResponseJob.from_dict(d.pop("job"))

        def _parse_wait(data: object) -> AgentWaitRecord | JobResponseWaitType1 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_0 = AgentWaitRecord.from_dict(data)

                return wait_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_1 = JobResponseWaitType1.from_dict(data)

                return wait_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentWaitRecord | JobResponseWaitType1 | None | Unset, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        def _parse_wait_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        wait_id = _parse_wait_id(d.pop("wait_id", UNSET))

        def _parse_decision(data: object) -> JobResponseDecisionType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                decision_type_0 = JobResponseDecisionType0.from_dict(data)

                return decision_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobResponseDecisionType0 | None | Unset, data)

        decision = _parse_decision(d.pop("decision", UNSET))

        def _parse_continuation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        continuation_id = _parse_continuation_id(d.pop("continuation_id", UNSET))

        def _parse_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        generation = _parse_generation(d.pop("generation", UNSET))

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        success = d.pop("success", UNSET)

        job_response = cls(
            job=job,
            wait=wait,
            wait_id=wait_id,
            decision=decision,
            continuation_id=continuation_id,
            generation=generation,
            next_step=next_step,
            success=success,
        )

        return job_response
