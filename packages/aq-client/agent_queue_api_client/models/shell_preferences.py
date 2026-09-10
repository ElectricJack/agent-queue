from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.shell_preferences_theme import ShellPreferencesTheme
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.right_surface import RightSurface
    from ..models.shell_preferences_pane_widths import ShellPreferencesPaneWidths


T = TypeVar("T", bound="ShellPreferences")


@_attrs_define
class ShellPreferences:
    """
    Attributes:
        theme (ShellPreferencesTheme | Unset):  Default: ShellPreferencesTheme.DARK.
        pane_widths (ShellPreferencesPaneWidths | Unset):
        right_surface (RightSurface | Unset):
        projects_section_open (bool | Unset):  Default: True.
        agent_flock_collapsed (bool | Unset):  Default: False.
        last_project_id (None | str | Unset):
    """

    theme: ShellPreferencesTheme | Unset = ShellPreferencesTheme.DARK
    pane_widths: ShellPreferencesPaneWidths | Unset = UNSET
    right_surface: RightSurface | Unset = UNSET
    projects_section_open: bool | Unset = True
    agent_flock_collapsed: bool | Unset = False
    last_project_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        theme: str | Unset = UNSET
        if not isinstance(self.theme, Unset):
            theme = self.theme.value

        pane_widths: dict[str, Any] | Unset = UNSET
        if not isinstance(self.pane_widths, Unset):
            pane_widths = self.pane_widths.to_dict()

        right_surface: dict[str, Any] | Unset = UNSET
        if not isinstance(self.right_surface, Unset):
            right_surface = self.right_surface.to_dict()

        projects_section_open = self.projects_section_open

        agent_flock_collapsed = self.agent_flock_collapsed

        last_project_id: None | str | Unset
        if isinstance(self.last_project_id, Unset):
            last_project_id = UNSET
        else:
            last_project_id = self.last_project_id

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if theme is not UNSET:
            field_dict["theme"] = theme
        if pane_widths is not UNSET:
            field_dict["pane_widths"] = pane_widths
        if right_surface is not UNSET:
            field_dict["right_surface"] = right_surface
        if projects_section_open is not UNSET:
            field_dict["projects_section_open"] = projects_section_open
        if agent_flock_collapsed is not UNSET:
            field_dict["agent_flock_collapsed"] = agent_flock_collapsed
        if last_project_id is not UNSET:
            field_dict["last_project_id"] = last_project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.right_surface import RightSurface
        from ..models.shell_preferences_pane_widths import ShellPreferencesPaneWidths

        d = dict(src_dict)
        _theme = d.pop("theme", UNSET)
        theme: ShellPreferencesTheme | Unset
        if isinstance(_theme, Unset):
            theme = UNSET
        else:
            theme = ShellPreferencesTheme(_theme)

        _pane_widths = d.pop("pane_widths", UNSET)
        pane_widths: ShellPreferencesPaneWidths | Unset
        if isinstance(_pane_widths, Unset):
            pane_widths = UNSET
        else:
            pane_widths = ShellPreferencesPaneWidths.from_dict(_pane_widths)

        _right_surface = d.pop("right_surface", UNSET)
        right_surface: RightSurface | Unset
        if isinstance(_right_surface, Unset):
            right_surface = UNSET
        else:
            right_surface = RightSurface.from_dict(_right_surface)

        projects_section_open = d.pop("projects_section_open", UNSET)

        agent_flock_collapsed = d.pop("agent_flock_collapsed", UNSET)

        def _parse_last_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_project_id = _parse_last_project_id(d.pop("last_project_id", UNSET))

        shell_preferences = cls(
            theme=theme,
            pane_widths=pane_widths,
            right_surface=right_surface,
            projects_section_open=projects_section_open,
            agent_flock_collapsed=agent_flock_collapsed,
            last_project_id=last_project_id,
        )

        return shell_preferences
