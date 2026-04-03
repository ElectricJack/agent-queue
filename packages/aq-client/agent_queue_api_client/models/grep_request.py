from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GrepRequest")


@_attrs_define
class GrepRequest:
    """
    Attributes:
        pattern (str): Regex pattern to search for
        path (str): File or directory to search in (absolute or relative to workspaces root)
        context (int | Unset): Number of context lines before and after each match Default: 0.
        case_insensitive (bool | Unset): Case-insensitive search (default false) Default: False.
        glob (None | str | Unset): Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')
        output_mode (str | Unset): Output mode: 'content' shows matching lines, 'files_with_matches' shows file paths
            only, 'count' shows match counts (default 'content') Default: 'content'.
        max_results (int | Unset): Maximum number of result lines to return (default 100) Default: 100.
    """

    pattern: str
    path: str
    context: int | Unset = 0
    case_insensitive: bool | Unset = False
    glob: None | str | Unset = UNSET
    output_mode: str | Unset = "content"
    max_results: int | Unset = 100
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pattern = self.pattern

        path = self.path

        context = self.context

        case_insensitive = self.case_insensitive

        glob: None | str | Unset
        if isinstance(self.glob, Unset):
            glob = UNSET
        else:
            glob = self.glob

        output_mode = self.output_mode

        max_results = self.max_results

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "pattern": pattern,
                "path": path,
            }
        )
        if context is not UNSET:
            field_dict["context"] = context
        if case_insensitive is not UNSET:
            field_dict["case_insensitive"] = case_insensitive
        if glob is not UNSET:
            field_dict["glob"] = glob
        if output_mode is not UNSET:
            field_dict["output_mode"] = output_mode
        if max_results is not UNSET:
            field_dict["max_results"] = max_results

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        pattern = d.pop("pattern")

        path = d.pop("path")

        context = d.pop("context", UNSET)

        case_insensitive = d.pop("case_insensitive", UNSET)

        def _parse_glob(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        glob = _parse_glob(d.pop("glob", UNSET))

        output_mode = d.pop("output_mode", UNSET)

        max_results = d.pop("max_results", UNSET)

        grep_request = cls(
            pattern=pattern,
            path=path,
            context=context,
            case_insensitive=case_insensitive,
            glob=glob,
            output_mode=output_mode,
            max_results=max_results,
        )

        grep_request.additional_properties = d
        return grep_request

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
