from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.message_model_pane_open_type_0 import MessageModelPaneOpenType0


T = TypeVar("T", bound="MessageModel")


@_attrs_define
class MessageModel:
    """Rendered message dict (see ``src/commands/message_commands.py::message_to_dict``).

    Attributes:
        id (str):
        from_kind (str):
        from_id (str):
        to_kind (str):
        to_id (str):
        body (str):
        project_id (None | str | Unset):
        from_ (None | str | Unset):
        to (None | str | Unset):
        thread_id (None | str | Unset):
        subject (None | str | Unset):
        priority (int | Unset):  Default: 100.
        created_at (float | None | Unset):
        delivered_at (float | None | Unset):
        read_at (float | None | Unset):
        read (bool | Unset):  Default: False.
        delivered (bool | Unset):  Default: False.
        archive_after_inject (bool | Unset):  Default: False.
        archived_at (float | None | Unset):
        reply_to_id (None | str | Unset):
        via (None | str | Unset):
        body_kind (None | str | Unset):
        pane_open (MessageModelPaneOpenType0 | None | Unset):
    """

    id: str
    from_kind: str
    from_id: str
    to_kind: str
    to_id: str
    body: str
    project_id: None | str | Unset = UNSET
    from_: None | str | Unset = UNSET
    to: None | str | Unset = UNSET
    thread_id: None | str | Unset = UNSET
    subject: None | str | Unset = UNSET
    priority: int | Unset = 100
    created_at: float | None | Unset = UNSET
    delivered_at: float | None | Unset = UNSET
    read_at: float | None | Unset = UNSET
    read: bool | Unset = False
    delivered: bool | Unset = False
    archive_after_inject: bool | Unset = False
    archived_at: float | None | Unset = UNSET
    reply_to_id: None | str | Unset = UNSET
    via: None | str | Unset = UNSET
    body_kind: None | str | Unset = UNSET
    pane_open: MessageModelPaneOpenType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.message_model_pane_open_type_0 import MessageModelPaneOpenType0  # noqa: PLC0415

        id = self.id

        from_kind = self.from_kind

        from_id = self.from_id

        to_kind = self.to_kind

        to_id = self.to_id

        body = self.body

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        from_: None | str | Unset
        if isinstance(self.from_, Unset):
            from_ = UNSET
        else:
            from_ = self.from_

        to: None | str | Unset
        if isinstance(self.to, Unset):
            to = UNSET
        else:
            to = self.to

        thread_id: None | str | Unset
        if isinstance(self.thread_id, Unset):
            thread_id = UNSET
        else:
            thread_id = self.thread_id

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        priority = self.priority

        created_at: float | None | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        delivered_at: float | None | Unset
        if isinstance(self.delivered_at, Unset):
            delivered_at = UNSET
        else:
            delivered_at = self.delivered_at

        read_at: float | None | Unset
        if isinstance(self.read_at, Unset):
            read_at = UNSET
        else:
            read_at = self.read_at

        read = self.read

        delivered = self.delivered

        archive_after_inject = self.archive_after_inject

        archived_at: float | None | Unset
        if isinstance(self.archived_at, Unset):
            archived_at = UNSET
        else:
            archived_at = self.archived_at

        reply_to_id: None | str | Unset
        if isinstance(self.reply_to_id, Unset):
            reply_to_id = UNSET
        else:
            reply_to_id = self.reply_to_id

        via: None | str | Unset
        if isinstance(self.via, Unset):
            via = UNSET
        else:
            via = self.via

        body_kind: None | str | Unset
        if isinstance(self.body_kind, Unset):
            body_kind = UNSET
        else:
            body_kind = self.body_kind

        pane_open: dict[str, Any] | None | Unset
        if isinstance(self.pane_open, Unset):
            pane_open = UNSET
        elif isinstance(self.pane_open, MessageModelPaneOpenType0):
            pane_open = self.pane_open.to_dict()
        else:
            pane_open = self.pane_open

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "from_kind": from_kind,
                "from_id": from_id,
                "to_kind": to_kind,
                "to_id": to_id,
                "body": body,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if from_ is not UNSET:
            field_dict["from"] = from_
        if to is not UNSET:
            field_dict["to"] = to
        if thread_id is not UNSET:
            field_dict["thread_id"] = thread_id
        if subject is not UNSET:
            field_dict["subject"] = subject
        if priority is not UNSET:
            field_dict["priority"] = priority
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if delivered_at is not UNSET:
            field_dict["delivered_at"] = delivered_at
        if read_at is not UNSET:
            field_dict["read_at"] = read_at
        if read is not UNSET:
            field_dict["read"] = read
        if delivered is not UNSET:
            field_dict["delivered"] = delivered
        if archive_after_inject is not UNSET:
            field_dict["archive_after_inject"] = archive_after_inject
        if archived_at is not UNSET:
            field_dict["archived_at"] = archived_at
        if reply_to_id is not UNSET:
            field_dict["reply_to_id"] = reply_to_id
        if via is not UNSET:
            field_dict["via"] = via
        if body_kind is not UNSET:
            field_dict["body_kind"] = body_kind
        if pane_open is not UNSET:
            field_dict["pane_open"] = pane_open

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_model_pane_open_type_0 import MessageModelPaneOpenType0  # noqa: PLC0415

        d = dict(src_dict)
        id = d.pop("id")

        from_kind = d.pop("from_kind")

        from_id = d.pop("from_id")

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

        def _parse_from_(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_ = _parse_from_(d.pop("from", UNSET))

        def _parse_to(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to = _parse_to(d.pop("to", UNSET))

        def _parse_thread_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        thread_id = _parse_thread_id(d.pop("thread_id", UNSET))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        priority = d.pop("priority", UNSET)

        def _parse_created_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        def _parse_delivered_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        delivered_at = _parse_delivered_at(d.pop("delivered_at", UNSET))

        def _parse_read_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        read_at = _parse_read_at(d.pop("read_at", UNSET))

        read = d.pop("read", UNSET)

        delivered = d.pop("delivered", UNSET)

        archive_after_inject = d.pop("archive_after_inject", UNSET)

        def _parse_archived_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        archived_at = _parse_archived_at(d.pop("archived_at", UNSET))

        def _parse_reply_to_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reply_to_id = _parse_reply_to_id(d.pop("reply_to_id", UNSET))

        def _parse_via(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        via = _parse_via(d.pop("via", UNSET))

        def _parse_body_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        body_kind = _parse_body_kind(d.pop("body_kind", UNSET))

        def _parse_pane_open(data: object) -> MessageModelPaneOpenType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                pane_open_type_0 = MessageModelPaneOpenType0.from_dict(data)

                return pane_open_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MessageModelPaneOpenType0 | None | Unset, data)

        pane_open = _parse_pane_open(d.pop("pane_open", UNSET))

        message_model = cls(
            id=id,
            from_kind=from_kind,
            from_id=from_id,
            to_kind=to_kind,
            to_id=to_id,
            body=body,
            project_id=project_id,
            from_=from_,
            to=to,
            thread_id=thread_id,
            subject=subject,
            priority=priority,
            created_at=created_at,
            delivered_at=delivered_at,
            read_at=read_at,
            read=read,
            delivered=delivered,
            archive_after_inject=archive_after_inject,
            archived_at=archived_at,
            reply_to_id=reply_to_id,
            via=via,
            body_kind=body_kind,
            pane_open=pane_open,
        )

        message_model.additional_properties = d
        return message_model

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
