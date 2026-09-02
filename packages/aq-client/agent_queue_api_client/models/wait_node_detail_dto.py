from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_node_detail_dto_wait_kind import WaitNodeDetailDTOWaitKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_value_dto import ExplanationValueDTO


T = TypeVar("T", bound="WaitNodeDetailDTO")


@_attrs_define
class WaitNodeDetailDTO:
    """WaitStep only.

    Attributes:
        wait_kind (WaitNodeDetailDTOWaitKind):
        awaited (str):
        correlation_key (ExplanationValueDTO): One typed value, in both its human and canonical forms.

            ``display`` is always present and always safe to render.  ``canonical`` is
            the Advanced-view payload and is ``None`` whenever ``redacted`` is true.
        timeout_seconds (int | None | Unset):
        timeout_step_id (None | str | Unset):
    """

    wait_kind: WaitNodeDetailDTOWaitKind
    awaited: str
    correlation_key: ExplanationValueDTO
    timeout_seconds: int | None | Unset = UNSET
    timeout_step_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_kind = self.wait_kind.value

        awaited = self.awaited

        correlation_key = self.correlation_key.to_dict()

        timeout_seconds: int | None | Unset
        if isinstance(self.timeout_seconds, Unset):
            timeout_seconds = UNSET
        else:
            timeout_seconds = self.timeout_seconds

        timeout_step_id: None | str | Unset
        if isinstance(self.timeout_step_id, Unset):
            timeout_step_id = UNSET
        else:
            timeout_step_id = self.timeout_step_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_kind": wait_kind,
                "awaited": awaited,
                "correlation_key": correlation_key,
            }
        )
        if timeout_seconds is not UNSET:
            field_dict["timeout_seconds"] = timeout_seconds
        if timeout_step_id is not UNSET:
            field_dict["timeout_step_id"] = timeout_step_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_value_dto import ExplanationValueDTO

        d = dict(src_dict)
        wait_kind = WaitNodeDetailDTOWaitKind(d.pop("wait_kind"))

        awaited = d.pop("awaited")

        correlation_key = ExplanationValueDTO.from_dict(d.pop("correlation_key"))

        def _parse_timeout_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        timeout_seconds = _parse_timeout_seconds(d.pop("timeout_seconds", UNSET))

        def _parse_timeout_step_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        timeout_step_id = _parse_timeout_step_id(d.pop("timeout_step_id", UNSET))

        wait_node_detail_dto = cls(
            wait_kind=wait_kind,
            awaited=awaited,
            correlation_key=correlation_key,
            timeout_seconds=timeout_seconds,
            timeout_step_id=timeout_step_id,
        )

        return wait_node_detail_dto
