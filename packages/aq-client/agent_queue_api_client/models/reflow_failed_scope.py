from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReflowFailedScope")


@_attrs_define
class ReflowFailedScope:
    """A single failed deferred reflow request, as reported by ``graph_reflow_status``.

    Attributes:
        project_id (str):
        variant (str):
        scope_key (str):
        generation (int | Unset):  Default: 1.
        attempts (int | Unset):  Default: 0.
        last_error (None | str | Unset):
    """

    project_id: str
    variant: str
    scope_key: str
    generation: int | Unset = 1
    attempts: int | Unset = 0
    last_error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        variant = self.variant

        scope_key = self.scope_key

        generation = self.generation

        attempts = self.attempts

        last_error: None | str | Unset
        if isinstance(self.last_error, Unset):
            last_error = UNSET
        else:
            last_error = self.last_error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "variant": variant,
                "scope_key": scope_key,
            }
        )
        if generation is not UNSET:
            field_dict["generation"] = generation
        if attempts is not UNSET:
            field_dict["attempts"] = attempts
        if last_error is not UNSET:
            field_dict["last_error"] = last_error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        variant = d.pop("variant")

        scope_key = d.pop("scope_key")

        generation = d.pop("generation", UNSET)

        attempts = d.pop("attempts", UNSET)

        def _parse_last_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_error = _parse_last_error(d.pop("last_error", UNSET))

        reflow_failed_scope = cls(
            project_id=project_id,
            variant=variant,
            scope_key=scope_key,
            generation=generation,
            attempts=attempts,
            last_error=last_error,
        )

        reflow_failed_scope.additional_properties = d
        return reflow_failed_scope

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
