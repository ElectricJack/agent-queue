from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord
    from ..models.job_response_job import JobResponseJob


T = TypeVar("T", bound="JobResponse")


@_attrs_define
class JobResponse:
    """
    Attributes:
        job (JobResponseJob):
        wait (AgentWaitRecord | None | Unset):
        next_step (None | str | Unset):
        success (bool | Unset):  Default: True.
    """

    job: JobResponseJob
    wait: AgentWaitRecord | None | Unset = UNSET
    next_step: None | str | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_wait_record import AgentWaitRecord

        job = self.job.to_dict()

        wait: dict[str, Any] | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        elif isinstance(self.wait, AgentWaitRecord):
            wait = self.wait.to_dict()
        else:
            wait = self.wait

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
        if next_step is not UNSET:
            field_dict["next_step"] = next_step
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record import AgentWaitRecord
        from ..models.job_response_job import JobResponseJob

        d = dict(src_dict)
        job = JobResponseJob.from_dict(d.pop("job"))

        def _parse_wait(data: object) -> AgentWaitRecord | None | Unset:
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
            return cast(AgentWaitRecord | None | Unset, data)

        wait = _parse_wait(d.pop("wait", UNSET))

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
            next_step=next_step,
            success=success,
        )

        return job_response
