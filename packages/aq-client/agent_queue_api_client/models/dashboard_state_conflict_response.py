from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
    from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
    from ..models.nav_organization_document import NavOrganizationDocument
    from ..models.playbook_graph_view_document import PlaybookGraphViewDocument
    from ..models.shell_preferences_document import ShellPreferencesDocument


T = TypeVar("T", bound="DashboardStateConflictResponse")


@_attrs_define
class DashboardStateConflictResponse:
    """
    Attributes:
        error_code (Literal['revision_conflict']):
        error (str):
        current (CommandCenterPreferencesDocument | CommandCenterProjectViewDocument | NavOrganizationDocument |
            PlaybookGraphViewDocument | ShellPreferencesDocument):
        success (bool | Unset):  Default: False.
    """

    error_code: Literal["revision_conflict"]
    error: str
    current: (
        CommandCenterPreferencesDocument
        | CommandCenterProjectViewDocument
        | NavOrganizationDocument
        | PlaybookGraphViewDocument
        | ShellPreferencesDocument
    )
    success: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
        from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
        from ..models.nav_organization_document import NavOrganizationDocument
        from ..models.shell_preferences_document import ShellPreferencesDocument

        error_code = self.error_code

        error = self.error

        current: dict[str, Any]
        if isinstance(self.current, NavOrganizationDocument):
            current = self.current.to_dict()
        elif isinstance(self.current, ShellPreferencesDocument):
            current = self.current.to_dict()
        elif isinstance(self.current, CommandCenterPreferencesDocument):
            current = self.current.to_dict()
        elif isinstance(self.current, CommandCenterProjectViewDocument):
            current = self.current.to_dict()
        else:
            current = self.current.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error_code": error_code,
                "error": error,
                "current": current,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
        from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
        from ..models.nav_organization_document import NavOrganizationDocument
        from ..models.playbook_graph_view_document import PlaybookGraphViewDocument
        from ..models.shell_preferences_document import ShellPreferencesDocument

        d = dict(src_dict)
        error_code = cast(Literal["revision_conflict"], d.pop("error_code"))
        if error_code != "revision_conflict":
            raise ValueError(f"error_code must match const 'revision_conflict', got '{error_code}'")

        error = d.pop("error")

        def _parse_current(
            data: object,
        ) -> (
            CommandCenterPreferencesDocument
            | CommandCenterProjectViewDocument
            | NavOrganizationDocument
            | PlaybookGraphViewDocument
            | ShellPreferencesDocument
        ):
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                current_type_0 = NavOrganizationDocument.from_dict(data)

                return current_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                current_type_1 = ShellPreferencesDocument.from_dict(data)

                return current_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                current_type_2 = CommandCenterPreferencesDocument.from_dict(data)

                return current_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                current_type_3 = CommandCenterProjectViewDocument.from_dict(data)

                return current_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            current_type_4 = PlaybookGraphViewDocument.from_dict(data)

            return current_type_4

        current = _parse_current(d.pop("current"))

        success = d.pop("success", UNSET)

        dashboard_state_conflict_response = cls(
            error_code=error_code,
            error=error,
            current=current,
            success=success,
        )

        dashboard_state_conflict_response.additional_properties = d
        return dashboard_state_conflict_response

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
