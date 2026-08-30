from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ShowPlaybookGraphRequest")


@_attrs_define
class ShowPlaybookGraphRequest:
    """
    Attributes:
        playbook_id (str): The playbook identifier to render
        format_ (str | Unset): Output format: 'ascii' for terminal/text output, 'mermaid' for Mermaid flowchart syntax.
            Default: ascii. Default: 'ascii'.
        direction (str | Unset): Mermaid flowchart direction: 'TD' (top-down) or 'LR' (left-right). Only used with
            mermaid format. Default: TD. Default: 'TD'.
        show_prompts (bool | Unset): Include truncated prompt previews in node labels. Default: true. Default: True.
    """

    playbook_id: str
    format_: str | Unset = "ascii"
    direction: str | Unset = "TD"
    show_prompts: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        format_ = self.format_

        direction = self.direction

        show_prompts = self.show_prompts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
            }
        )
        if format_ is not UNSET:
            field_dict["format"] = format_
        if direction is not UNSET:
            field_dict["direction"] = direction
        if show_prompts is not UNSET:
            field_dict["show_prompts"] = show_prompts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        format_ = d.pop("format", UNSET)

        direction = d.pop("direction", UNSET)

        show_prompts = d.pop("show_prompts", UNSET)

        show_playbook_graph_request = cls(
            playbook_id=playbook_id,
            format_=format_,
            direction=direction,
            show_prompts=show_prompts,
        )

        show_playbook_graph_request.additional_properties = d
        return show_playbook_graph_request

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
