from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.link_mutation_response_links_item import LinkMutationResponseLinksItem


T = TypeVar("T", bound="LinkMutationResponse")


@_attrs_define
class LinkMutationResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        record_id (None | str | Unset):
        replay (bool | Unset):  Default: False.
        links (list[LinkMutationResponseLinksItem] | Unset):
    """

    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    record_id: None | str | Unset = UNSET
    replay: bool | Unset = False
    links: list[LinkMutationResponseLinksItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        record_id: None | str | Unset
        if isinstance(self.record_id, Unset):
            record_id = UNSET
        else:
            record_id = self.record_id

        replay = self.replay

        links: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.links, Unset):
            links = []
            for links_item_data in self.links:
                links_item = links_item_data.to_dict()
                links.append(links_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if record_id is not UNSET:
            field_dict["record_id"] = record_id
        if replay is not UNSET:
            field_dict["replay"] = replay
        if links is not UNSET:
            field_dict["links"] = links

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.link_mutation_response_links_item import LinkMutationResponseLinksItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_record_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        record_id = _parse_record_id(d.pop("record_id", UNSET))

        replay = d.pop("replay", UNSET)

        _links = d.pop("links", UNSET)
        links: list[LinkMutationResponseLinksItem] | Unset = UNSET
        if _links is not UNSET:
            links = []
            for links_item_data in _links:
                links_item = LinkMutationResponseLinksItem.from_dict(links_item_data)

                links.append(links_item)

        link_mutation_response = cls(
            success=success,
            outcome=outcome,
            record_id=record_id,
            replay=replay,
            links=links,
        )

        link_mutation_response.additional_properties = d
        return link_mutation_response

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
