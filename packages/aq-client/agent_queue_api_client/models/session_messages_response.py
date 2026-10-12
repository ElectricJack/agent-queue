from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.message_model import MessageModel
    from ..models.session_delivery_diagnostic import SessionDeliveryDiagnostic


T = TypeVar("T", bound="SessionMessagesResponse")


@_attrs_define
class SessionMessagesResponse:
    """
    Attributes:
        session (str):
        count (int):
        messages (list[MessageModel]):
        success (bool | Unset):  Default: True.
        project_id (None | str | Unset):
        delivery_failure (None | SessionDeliveryDiagnostic | Unset):
    """

    session: str
    count: int
    messages: list[MessageModel]
    success: bool | Unset = True
    project_id: None | str | Unset = UNSET
    delivery_failure: None | SessionDeliveryDiagnostic | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.session_delivery_diagnostic import SessionDeliveryDiagnostic

        session = self.session

        count = self.count

        messages = []
        for messages_item_data in self.messages:
            messages_item = messages_item_data.to_dict()
            messages.append(messages_item)

        success = self.success

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        delivery_failure: dict[str, Any] | None | Unset
        if isinstance(self.delivery_failure, Unset):
            delivery_failure = UNSET
        elif isinstance(self.delivery_failure, SessionDeliveryDiagnostic):
            delivery_failure = self.delivery_failure.to_dict()
        else:
            delivery_failure = self.delivery_failure

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session": session,
                "count": count,
                "messages": messages,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if delivery_failure is not UNSET:
            field_dict["delivery_failure"] = delivery_failure

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_model import MessageModel
        from ..models.session_delivery_diagnostic import SessionDeliveryDiagnostic

        d = dict(src_dict)
        session = d.pop("session")

        count = d.pop("count")

        messages = []
        _messages = d.pop("messages")
        for messages_item_data in _messages:
            messages_item = MessageModel.from_dict(messages_item_data)

            messages.append(messages_item)

        success = d.pop("success", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_delivery_failure(data: object) -> None | SessionDeliveryDiagnostic | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                delivery_failure_type_0 = SessionDeliveryDiagnostic.from_dict(data)

                return delivery_failure_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SessionDeliveryDiagnostic | Unset, data)

        delivery_failure = _parse_delivery_failure(d.pop("delivery_failure", UNSET))

        session_messages_response = cls(
            session=session,
            count=count,
            messages=messages,
            success=success,
            project_id=project_id,
            delivery_failure=delivery_failure,
        )

        session_messages_response.additional_properties = d
        return session_messages_response

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
