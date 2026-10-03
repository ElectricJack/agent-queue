from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeProposalShowRequest")


@_attrs_define
class KnowledgeProposalShowRequest:
    """
    Attributes:
        proposal_id (str):
        project_id (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
    """

    proposal_id: str
    project_id: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        proposal_id = self.proposal_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        global_scope = self.global_scope

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "proposal_id": proposal_id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        proposal_id = d.pop("proposal_id")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        knowledge_proposal_show_request = cls(
            proposal_id=proposal_id,
            project_id=project_id,
            global_scope=global_scope,
        )

        knowledge_proposal_show_request.additional_properties = d
        return knowledge_proposal_show_request

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
