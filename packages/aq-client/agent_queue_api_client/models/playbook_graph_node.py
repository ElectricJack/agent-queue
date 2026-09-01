from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.compiled_playbook_node import CompiledPlaybookNode
    from ..models.playbook_graph_node_colors import PlaybookGraphNodeColors
    from ..models.playbook_graph_position import PlaybookGraphPosition


T = TypeVar("T", bound="PlaybookGraphNode")


@_attrs_define
class PlaybookGraphNode:
    """One positioned, classified node in the compiled graph.

    Attributes:
        id (str):
        type_ (str):
        colors (PlaybookGraphNodeColors):
        details (CompiledPlaybookNode): The serializable fields produced by ``PlaybookNode.to_dict()``.

            Each field is optional according to the compiled-node rules: a key is
            present only when the compiler set it.  This is what the dashboard node
            inspector renders, so the prompt here is the full untruncated text.
        symbol (str | Unset):  Default: ''.
        label (str | Unset):  Default: ''.
        position (PlaybookGraphPosition | Unset): A stable grid coordinate produced by the backend layout.
        entry (bool | Unset):  Default: False.
        terminal (bool | Unset):  Default: False.
        wait_for_human (bool | Unset):  Default: False.
        prompt_preview (None | str | Unset):
        timeout_seconds (int | None | Unset):
        on_timeout (None | str | Unset):
        out_degree (int | Unset):  Default: 0.
    """

    id: str
    type_: str
    colors: PlaybookGraphNodeColors
    details: CompiledPlaybookNode
    symbol: str | Unset = ""
    label: str | Unset = ""
    position: PlaybookGraphPosition | Unset = UNSET
    entry: bool | Unset = False
    terminal: bool | Unset = False
    wait_for_human: bool | Unset = False
    prompt_preview: None | str | Unset = UNSET
    timeout_seconds: int | None | Unset = UNSET
    on_timeout: None | str | Unset = UNSET
    out_degree: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        type_ = self.type_

        colors = self.colors.to_dict()

        details = self.details.to_dict()

        symbol = self.symbol

        label = self.label

        position: dict[str, Any] | Unset = UNSET
        if not isinstance(self.position, Unset):
            position = self.position.to_dict()

        entry = self.entry

        terminal = self.terminal

        wait_for_human = self.wait_for_human

        prompt_preview: None | str | Unset
        if isinstance(self.prompt_preview, Unset):
            prompt_preview = UNSET
        else:
            prompt_preview = self.prompt_preview

        timeout_seconds: int | None | Unset
        if isinstance(self.timeout_seconds, Unset):
            timeout_seconds = UNSET
        else:
            timeout_seconds = self.timeout_seconds

        on_timeout: None | str | Unset
        if isinstance(self.on_timeout, Unset):
            on_timeout = UNSET
        else:
            on_timeout = self.on_timeout

        out_degree = self.out_degree

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "type": type_,
                "colors": colors,
                "details": details,
            }
        )
        if symbol is not UNSET:
            field_dict["symbol"] = symbol
        if label is not UNSET:
            field_dict["label"] = label
        if position is not UNSET:
            field_dict["position"] = position
        if entry is not UNSET:
            field_dict["entry"] = entry
        if terminal is not UNSET:
            field_dict["terminal"] = terminal
        if wait_for_human is not UNSET:
            field_dict["wait_for_human"] = wait_for_human
        if prompt_preview is not UNSET:
            field_dict["prompt_preview"] = prompt_preview
        if timeout_seconds is not UNSET:
            field_dict["timeout_seconds"] = timeout_seconds
        if on_timeout is not UNSET:
            field_dict["on_timeout"] = on_timeout
        if out_degree is not UNSET:
            field_dict["out_degree"] = out_degree

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_playbook_node import CompiledPlaybookNode  # noqa: PLC0415
        from ..models.playbook_graph_node_colors import PlaybookGraphNodeColors  # noqa: PLC0415
        from ..models.playbook_graph_position import PlaybookGraphPosition  # noqa: PLC0415

        d = dict(src_dict)
        id = d.pop("id")

        type_ = d.pop("type")

        colors = PlaybookGraphNodeColors.from_dict(d.pop("colors"))

        details = CompiledPlaybookNode.from_dict(d.pop("details"))

        symbol = d.pop("symbol", UNSET)

        label = d.pop("label", UNSET)

        _position = d.pop("position", UNSET)
        position: PlaybookGraphPosition | Unset
        if isinstance(_position, Unset):
            position = UNSET
        else:
            position = PlaybookGraphPosition.from_dict(_position)

        entry = d.pop("entry", UNSET)

        terminal = d.pop("terminal", UNSET)

        wait_for_human = d.pop("wait_for_human", UNSET)

        def _parse_prompt_preview(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prompt_preview = _parse_prompt_preview(d.pop("prompt_preview", UNSET))

        def _parse_timeout_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        timeout_seconds = _parse_timeout_seconds(d.pop("timeout_seconds", UNSET))

        def _parse_on_timeout(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        on_timeout = _parse_on_timeout(d.pop("on_timeout", UNSET))

        out_degree = d.pop("out_degree", UNSET)

        playbook_graph_node = cls(
            id=id,
            type_=type_,
            colors=colors,
            details=details,
            symbol=symbol,
            label=label,
            position=position,
            entry=entry,
            terminal=terminal,
            wait_for_human=wait_for_human,
            prompt_preview=prompt_preview,
            timeout_seconds=timeout_seconds,
            on_timeout=on_timeout,
            out_degree=out_degree,
        )

        playbook_graph_node.additional_properties = d
        return playbook_graph_node

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
