from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.message_send_request_pane_open_type_0 import MessageSendRequestPaneOpenType0


T = TypeVar("T", bound="MessageSendRequest")


@_attrs_define
class MessageSendRequest:
    """Body of POST /api/messages/send for non-session recipients.

    Attributes:
        to_kind (str): Recipient kind: session | task | profile | user
        to_id (str): Recipient id
        body (str): Markdown message body
        project_id (None | str | Unset): Owning project id
        from_id (str | Unset): Sender id Default: 'cli'.
        from_kind (str | Unset): Sender kind Default: 'user'.
        subject (None | str | Unset):
        thread_id (None | str | Unset):
        priority (int | Unset):  Default: 100.
        archive_after_inject (bool | Unset):  Default: False.
        pane_open (MessageSendRequestPaneOpenType0 | None | Unset):
        system_only (bool | Unset): Request projectless system scope Default: False.
    """

    to_kind: str
    to_id: str
    body: str
    project_id: None | str | Unset = UNSET
    from_id: str | Unset = "cli"
    from_kind: str | Unset = "user"
    subject: None | str | Unset = UNSET
    thread_id: None | str | Unset = UNSET
    priority: int | Unset = 100
    archive_after_inject: bool | Unset = False
    pane_open: MessageSendRequestPaneOpenType0 | None | Unset = UNSET
    system_only: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.message_send_request_pane_open_type_0 import MessageSendRequestPaneOpenType0

        to_kind = self.to_kind

        to_id = self.to_id

        body = self.body

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        from_id = self.from_id

        from_kind = self.from_kind

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        thread_id: None | str | Unset
        if isinstance(self.thread_id, Unset):
            thread_id = UNSET
        else:
            thread_id = self.thread_id

        priority = self.priority

        archive_after_inject = self.archive_after_inject

        pane_open: dict[str, Any] | None | Unset
        if isinstance(self.pane_open, Unset):
            pane_open = UNSET
        elif isinstance(self.pane_open, MessageSendRequestPaneOpenType0):
            pane_open = self.pane_open.to_dict()
        else:
            pane_open = self.pane_open

        system_only = self.system_only

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "to_kind": to_kind,
                "to_id": to_id,
                "body": body,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if from_id is not UNSET:
            field_dict["from_id"] = from_id
        if from_kind is not UNSET:
            field_dict["from_kind"] = from_kind
        if subject is not UNSET:
            field_dict["subject"] = subject
        if thread_id is not UNSET:
            field_dict["thread_id"] = thread_id
        if priority is not UNSET:
            field_dict["priority"] = priority
        if archive_after_inject is not UNSET:
            field_dict["archive_after_inject"] = archive_after_inject
        if pane_open is not UNSET:
            field_dict["pane_open"] = pane_open
        if system_only is not UNSET:
            field_dict["system_only"] = system_only

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_send_request_pane_open_type_0 import MessageSendRequestPaneOpenType0

        d = dict(src_dict)
        to_kind = d.pop("to_kind")

        to_id = d.pop("to_id")

        body = d.pop("body")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        from_id = d.pop("from_id", UNSET)

        from_kind = d.pop("from_kind", UNSET)

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        def _parse_thread_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        thread_id = _parse_thread_id(d.pop("thread_id", UNSET))

        priority = d.pop("priority", UNSET)

        archive_after_inject = d.pop("archive_after_inject", UNSET)

        def _parse_pane_open(data: object) -> MessageSendRequestPaneOpenType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                pane_open_type_0 = MessageSendRequestPaneOpenType0.from_dict(data)

                return pane_open_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MessageSendRequestPaneOpenType0 | None | Unset, data)

        pane_open = _parse_pane_open(d.pop("pane_open", UNSET))

        system_only = d.pop("system_only", UNSET)

        message_send_request = cls(
            to_kind=to_kind,
            to_id=to_id,
            body=body,
            project_id=project_id,
            from_id=from_id,
            from_kind=from_kind,
            subject=subject,
            thread_id=thread_id,
            priority=priority,
            archive_after_inject=archive_after_inject,
            pane_open=pane_open,
            system_only=system_only,
        )

        message_send_request.additional_properties = d
        return message_send_request

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
