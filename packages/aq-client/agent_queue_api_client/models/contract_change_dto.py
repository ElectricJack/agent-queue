from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.contract_change_dto_change import ContractChangeDTOChange
from ..types import UNSET, Unset

T = TypeVar("T", bound="ContractChangeDTO")


@_attrs_define
class ContractChangeDTO:
    """
    Attributes:
        command (str):
        change (ContractChangeDTOChange):
        fingerprint_before (None | str | Unset):
        fingerprint_after (None | str | Unset):
    """

    command: str
    change: ContractChangeDTOChange
    fingerprint_before: None | str | Unset = UNSET
    fingerprint_after: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        command = self.command

        change = self.change.value

        fingerprint_before: None | str | Unset
        if isinstance(self.fingerprint_before, Unset):
            fingerprint_before = UNSET
        else:
            fingerprint_before = self.fingerprint_before

        fingerprint_after: None | str | Unset
        if isinstance(self.fingerprint_after, Unset):
            fingerprint_after = UNSET
        else:
            fingerprint_after = self.fingerprint_after

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "command": command,
                "change": change,
            }
        )
        if fingerprint_before is not UNSET:
            field_dict["fingerprint_before"] = fingerprint_before
        if fingerprint_after is not UNSET:
            field_dict["fingerprint_after"] = fingerprint_after

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        command = d.pop("command")

        change = ContractChangeDTOChange(d.pop("change"))

        def _parse_fingerprint_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        fingerprint_before = _parse_fingerprint_before(d.pop("fingerprint_before", UNSET))

        def _parse_fingerprint_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        fingerprint_after = _parse_fingerprint_after(d.pop("fingerprint_after", UNSET))

        contract_change_dto = cls(
            command=command,
            change=change,
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
        )

        return contract_change_dto
