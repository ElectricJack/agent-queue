from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CompilePlaybookRequest")


@_attrs_define
class CompilePlaybookRequest:
    """
    Attributes:
        markdown (None | str | Unset): Full playbook markdown content including YAML frontmatter. Frontmatter must
            include: id, triggers (list), scope (system|project|agent-type:xxx). One of 'markdown', 'path', or 'playbook_id'
            is required.
        path (None | str | Unset): Absolute path to a playbook .md file on disk. If provided, the file is read and used
            as the markdown.
        playbook_id (None | str | Unset): ID of an already-compiled playbook. Resolves to its source path via the
            playbook manager and recompiles it. Use this to recompile by ID without remembering the vault path. One of
            'markdown', 'path', or 'playbook_id' is required.
        force (bool | Unset): Force recompilation even if source is unchanged. Defaults to true for manual compilation.
            Default: True.
    """

    markdown: None | str | Unset = UNSET
    path: None | str | Unset = UNSET
    playbook_id: None | str | Unset = UNSET
    force: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        markdown: None | str | Unset
        if isinstance(self.markdown, Unset):
            markdown = UNSET
        else:
            markdown = self.markdown

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        force = self.force

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if markdown is not UNSET:
            field_dict["markdown"] = markdown
        if path is not UNSET:
            field_dict["path"] = path
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_markdown(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        markdown = _parse_markdown(d.pop("markdown", UNSET))

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        force = d.pop("force", UNSET)

        compile_playbook_request = cls(
            markdown=markdown,
            path=path,
            playbook_id=playbook_id,
            force=force,
        )

        compile_playbook_request.additional_properties = d
        return compile_playbook_request

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
