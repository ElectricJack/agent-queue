from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.conversation_diagnostics_permissions_type_0 import ConversationDiagnosticsPermissionsType0


T = TypeVar("T", bound="ConversationDiagnostics")


@_attrs_define
class ConversationDiagnostics:
    """
    Attributes:
        message_content_intent (bool | None):
        permissions (ConversationDiagnosticsPermissionsType0 | None):
        outbox_bound (bool):
    """

    message_content_intent: bool | None
    permissions: ConversationDiagnosticsPermissionsType0 | None
    outbox_bound: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.conversation_diagnostics_permissions_type_0 import ConversationDiagnosticsPermissionsType0

        message_content_intent: bool | None
        message_content_intent = self.message_content_intent

        permissions: dict[str, Any] | None
        if isinstance(self.permissions, ConversationDiagnosticsPermissionsType0):
            permissions = self.permissions.to_dict()
        else:
            permissions = self.permissions

        outbox_bound = self.outbox_bound

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message_content_intent": message_content_intent,
                "permissions": permissions,
                "outbox_bound": outbox_bound,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_diagnostics_permissions_type_0 import ConversationDiagnosticsPermissionsType0

        d = dict(src_dict)

        def _parse_message_content_intent(data: object) -> bool | None:
            if data is None:
                return data
            return cast(bool | None, data)

        message_content_intent = _parse_message_content_intent(d.pop("message_content_intent"))

        def _parse_permissions(data: object) -> ConversationDiagnosticsPermissionsType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                permissions_type_0 = ConversationDiagnosticsPermissionsType0.from_dict(data)

                return permissions_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ConversationDiagnosticsPermissionsType0 | None, data)

        permissions = _parse_permissions(d.pop("permissions"))

        outbox_bound = d.pop("outbox_bound")

        conversation_diagnostics = cls(
            message_content_intent=message_content_intent,
            permissions=permissions,
            outbox_bound=outbox_bound,
        )

        conversation_diagnostics.additional_properties = d
        return conversation_diagnostics

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
