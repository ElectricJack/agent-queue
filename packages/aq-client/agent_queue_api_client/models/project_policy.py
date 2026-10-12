from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.project_settings import ProjectSettings


T = TypeVar("T", bound="ProjectPolicy")


@_attrs_define
class ProjectPolicy:
    """
    Attributes:
        settings (ProjectSettings):
        type_ (Literal['project_settings'] | Unset):  Default: 'project_settings'.
    """

    settings: ProjectSettings
    type_: Literal["project_settings"] | Unset = "project_settings"

    def to_dict(self) -> dict[str, Any]:
        settings = self.settings.to_dict()

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "settings": settings,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.project_settings import ProjectSettings

        d = dict(src_dict)
        settings = ProjectSettings.from_dict(d.pop("settings"))

        type_ = cast(Literal["project_settings"] | Unset, d.pop("type", UNSET))
        if type_ != "project_settings" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'project_settings', got '{type_}'")

        project_policy = cls(
            settings=settings,
            type_=type_,
        )

        return project_policy
