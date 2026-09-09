from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DiscordCutoverStatus")


@_attrs_define
class DiscordCutoverStatus:
    """
    Attributes:
        status (str):
        channel_id (str | Unset):  Default: ''.
        migrated_questions (int | Unset):  Default: 0.
        migrated_gates (int | Unset):  Default: 0.
        accepted_answers_preserved (int | Unset):  Default: 0.
        adopted_roots (int | Unset):  Default: 0.
        retired_task_threads (int | Unset):  Default: 0.
        inert_messages (int | Unset):  Default: 0.
        conflicts (list[str] | Unset):
    """

    status: str
    channel_id: str | Unset = ""
    migrated_questions: int | Unset = 0
    migrated_gates: int | Unset = 0
    accepted_answers_preserved: int | Unset = 0
    adopted_roots: int | Unset = 0
    retired_task_threads: int | Unset = 0
    inert_messages: int | Unset = 0
    conflicts: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        channel_id = self.channel_id

        migrated_questions = self.migrated_questions

        migrated_gates = self.migrated_gates

        accepted_answers_preserved = self.accepted_answers_preserved

        adopted_roots = self.adopted_roots

        retired_task_threads = self.retired_task_threads

        inert_messages = self.inert_messages

        conflicts: list[str] | Unset = UNSET
        if not isinstance(self.conflicts, Unset):
            conflicts = self.conflicts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "status": status,
            }
        )
        if channel_id is not UNSET:
            field_dict["channel_id"] = channel_id
        if migrated_questions is not UNSET:
            field_dict["migrated_questions"] = migrated_questions
        if migrated_gates is not UNSET:
            field_dict["migrated_gates"] = migrated_gates
        if accepted_answers_preserved is not UNSET:
            field_dict["accepted_answers_preserved"] = accepted_answers_preserved
        if adopted_roots is not UNSET:
            field_dict["adopted_roots"] = adopted_roots
        if retired_task_threads is not UNSET:
            field_dict["retired_task_threads"] = retired_task_threads
        if inert_messages is not UNSET:
            field_dict["inert_messages"] = inert_messages
        if conflicts is not UNSET:
            field_dict["conflicts"] = conflicts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status")

        channel_id = d.pop("channel_id", UNSET)

        migrated_questions = d.pop("migrated_questions", UNSET)

        migrated_gates = d.pop("migrated_gates", UNSET)

        accepted_answers_preserved = d.pop("accepted_answers_preserved", UNSET)

        adopted_roots = d.pop("adopted_roots", UNSET)

        retired_task_threads = d.pop("retired_task_threads", UNSET)

        inert_messages = d.pop("inert_messages", UNSET)

        conflicts = cast(list[str], d.pop("conflicts", UNSET))

        discord_cutover_status = cls(
            status=status,
            channel_id=channel_id,
            migrated_questions=migrated_questions,
            migrated_gates=migrated_gates,
            accepted_answers_preserved=accepted_answers_preserved,
            adopted_roots=adopted_roots,
            retired_task_threads=retired_task_threads,
            inert_messages=inert_messages,
            conflicts=conflicts,
        )

        discord_cutover_status.additional_properties = d
        return discord_cutover_status

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
