from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord


T = TypeVar("T", bound="WaitResponse")


@_attrs_define
class WaitResponse:
    """
    Attributes:
        wait (AgentWaitRecord): Persisted wait/result shape shared by contracts and generated clients.
        success (bool | Unset):  Default: True.
        next_step (None | str | Unset):
    """

    wait: AgentWaitRecord
    success: bool | Unset = True
    next_step: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait = self.wait.to_dict()

        success = self.success

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait": wait,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if next_step is not UNSET:
            field_dict["next_step"] = next_step

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record import AgentWaitRecord

        d = dict(src_dict)
        wait = AgentWaitRecord.from_dict(d.pop("wait"))

        success = d.pop("success", UNSET)

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        wait_response = cls(
            wait=wait,
            success=success,
            next_step=next_step,
        )

        return wait_response
