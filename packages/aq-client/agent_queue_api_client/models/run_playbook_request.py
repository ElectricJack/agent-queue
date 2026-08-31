from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.run_playbook_request_event_type_0 import RunPlaybookRequestEventType0


T = TypeVar("T", bound="RunPlaybookRequest")


@_attrs_define
class RunPlaybookRequest:
    """
    Attributes:
        playbook_id (str): The compiled playbook ID to execute
        event (None | RunPlaybookRequestEventType0 | Unset): Trigger event data to seed the run. Defaults to {"type":
            "manual"} if not provided. Include fields your playbook expects (e.g. project_id, task_id) for context.
    """

    playbook_id: str
    event: None | RunPlaybookRequestEventType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.run_playbook_request_event_type_0 import RunPlaybookRequestEventType0  # noqa: PLC0415

        playbook_id = self.playbook_id

        event: dict[str, Any] | None | Unset
        if isinstance(self.event, Unset):
            event = UNSET
        elif isinstance(self.event, RunPlaybookRequestEventType0):
            event = self.event.to_dict()
        else:
            event = self.event

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
            }
        )
        if event is not UNSET:
            field_dict["event"] = event

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_playbook_request_event_type_0 import RunPlaybookRequestEventType0  # noqa: PLC0415

        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        def _parse_event(data: object) -> None | RunPlaybookRequestEventType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                event_type_0 = RunPlaybookRequestEventType0.from_dict(data)

                return event_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunPlaybookRequestEventType0 | Unset, data)

        event = _parse_event(d.pop("event", UNSET))

        run_playbook_request = cls(
            playbook_id=playbook_id,
            event=event,
        )

        run_playbook_request.additional_properties = d
        return run_playbook_request

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
