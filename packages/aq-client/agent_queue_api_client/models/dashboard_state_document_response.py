from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
    from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
    from ..models.nav_organization_document import NavOrganizationDocument
    from ..models.playbook_graph_view_document import PlaybookGraphViewDocument
    from ..models.shell_preferences_document import ShellPreferencesDocument


T = TypeVar("T", bound="DashboardStateDocumentResponse")


@_attrs_define
class DashboardStateDocumentResponse:
    """
    Attributes:
        success (bool):
        document (CommandCenterPreferencesDocument | CommandCenterProjectViewDocument | NavOrganizationDocument |
            PlaybookGraphViewDocument | ShellPreferencesDocument):
    """

    success: bool
    document: (
        CommandCenterPreferencesDocument
        | CommandCenterProjectViewDocument
        | NavOrganizationDocument
        | PlaybookGraphViewDocument
        | ShellPreferencesDocument
    )
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
        from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
        from ..models.nav_organization_document import NavOrganizationDocument
        from ..models.shell_preferences_document import ShellPreferencesDocument

        success = self.success

        document: dict[str, Any]
        if isinstance(self.document, NavOrganizationDocument):
            document = self.document.to_dict()
        elif isinstance(self.document, ShellPreferencesDocument):
            document = self.document.to_dict()
        elif isinstance(self.document, CommandCenterPreferencesDocument):
            document = self.document.to_dict()
        elif isinstance(self.document, CommandCenterProjectViewDocument):
            document = self.document.to_dict()
        else:
            document = self.document.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "document": document,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
        from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
        from ..models.nav_organization_document import NavOrganizationDocument
        from ..models.playbook_graph_view_document import PlaybookGraphViewDocument
        from ..models.shell_preferences_document import ShellPreferencesDocument

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_document(
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
                document_type_0 = NavOrganizationDocument.from_dict(data)

                return document_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                document_type_1 = ShellPreferencesDocument.from_dict(data)

                return document_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                document_type_2 = CommandCenterPreferencesDocument.from_dict(data)

                return document_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                document_type_3 = CommandCenterProjectViewDocument.from_dict(data)

                return document_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            document_type_4 = PlaybookGraphViewDocument.from_dict(data)

            return document_type_4

        document = _parse_document(d.pop("document"))

        dashboard_state_document_response = cls(
            success=success,
            document=document,
        )

        dashboard_state_document_response.additional_properties = d
        return dashboard_state_document_response

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
