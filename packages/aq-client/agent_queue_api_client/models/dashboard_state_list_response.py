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


T = TypeVar("T", bound="DashboardStateListResponse")


@_attrs_define
class DashboardStateListResponse:
    """
    Attributes:
        success (bool):
        owner_id (str):
        documents (list[CommandCenterPreferencesDocument | CommandCenterProjectViewDocument | NavOrganizationDocument |
            PlaybookGraphViewDocument | ShellPreferencesDocument]):
    """

    success: bool
    owner_id: str
    documents: list[
        CommandCenterPreferencesDocument
        | CommandCenterProjectViewDocument
        | NavOrganizationDocument
        | PlaybookGraphViewDocument
        | ShellPreferencesDocument
    ]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.command_center_preferences_document import CommandCenterPreferencesDocument
        from ..models.command_center_project_view_document import CommandCenterProjectViewDocument
        from ..models.nav_organization_document import NavOrganizationDocument
        from ..models.shell_preferences_document import ShellPreferencesDocument

        success = self.success

        owner_id = self.owner_id

        documents = []
        for documents_item_data in self.documents:
            documents_item: dict[str, Any]
            if isinstance(documents_item_data, NavOrganizationDocument):
                documents_item = documents_item_data.to_dict()
            elif isinstance(documents_item_data, ShellPreferencesDocument):
                documents_item = documents_item_data.to_dict()
            elif isinstance(documents_item_data, CommandCenterPreferencesDocument):
                documents_item = documents_item_data.to_dict()
            elif isinstance(documents_item_data, CommandCenterProjectViewDocument):
                documents_item = documents_item_data.to_dict()
            else:
                documents_item = documents_item_data.to_dict()

            documents.append(documents_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "owner_id": owner_id,
                "documents": documents,
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

        owner_id = d.pop("owner_id")

        documents = []
        _documents = d.pop("documents")
        for documents_item_data in _documents:

            def _parse_documents_item(
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
                    documents_item_type_0 = NavOrganizationDocument.from_dict(data)

                    return documents_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    documents_item_type_1 = ShellPreferencesDocument.from_dict(data)

                    return documents_item_type_1
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    documents_item_type_2 = CommandCenterPreferencesDocument.from_dict(data)

                    return documents_item_type_2
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    documents_item_type_3 = CommandCenterProjectViewDocument.from_dict(data)

                    return documents_item_type_3
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                documents_item_type_4 = PlaybookGraphViewDocument.from_dict(data)

                return documents_item_type_4

            documents_item = _parse_documents_item(documents_item_data)

            documents.append(documents_item)

        dashboard_state_list_response = cls(
            success=success,
            owner_id=owner_id,
            documents=documents,
        )

        dashboard_state_list_response.additional_properties = d
        return dashboard_state_list_response

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
