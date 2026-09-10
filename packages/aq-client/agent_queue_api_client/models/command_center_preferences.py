from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.command_center_preferences_density import CommandCenterPreferencesDensity
from ..types import UNSET, Unset

T = TypeVar("T", bound="CommandCenterPreferences")


@_attrs_define
class CommandCenterPreferences:
    """
    Attributes:
        density (CommandCenterPreferencesDensity | Unset):  Default: CommandCenterPreferencesDensity.COMFORTABLE.
    """

    density: CommandCenterPreferencesDensity | Unset = CommandCenterPreferencesDensity.COMFORTABLE

    def to_dict(self) -> dict[str, Any]:
        density: str | Unset = UNSET
        if not isinstance(self.density, Unset):
            density = self.density.value

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if density is not UNSET:
            field_dict["density"] = density

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _density = d.pop("density", UNSET)
        density: CommandCenterPreferencesDensity | Unset
        if isinstance(_density, Unset):
            density = UNSET
        else:
            density = CommandCenterPreferencesDensity(_density)

        command_center_preferences = cls(
            density=density,
        )

        return command_center_preferences
