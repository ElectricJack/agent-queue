from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewSubmitRequest")


@_attrs_define
class ReviewSubmitRequest:
    """
    Attributes:
        content (str):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        review_id (None | str | Unset):
        kind (None | str | Unset):
        title (None | str | Unset):
        changes (None | str | Unset):
        resolves (list[Any] | None | Unset):
        playbook_id (None | str | Unset): The playbook this review asks approval for; its Markdown source must already
            be in the vault.
        semantic_body (None | str | Unset): JSON text with exactly the proposal's rules and steps. Omitted on a
            revision, the previous artifact's are reused.
        semantic_body_path (None | str | Unset): Vault path of the semantic body, instead of semantic_body.
        activate_on_approval (bool | None | Unset): Activate the pinned artifact when the review is approved (default:
            store it and tell the supervisor).
    """

    content: str
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    review_id: None | str | Unset = UNSET
    kind: None | str | Unset = UNSET
    title: None | str | Unset = UNSET
    changes: None | str | Unset = UNSET
    resolves: list[Any] | None | Unset = UNSET
    playbook_id: None | str | Unset = UNSET
    semantic_body: None | str | Unset = UNSET
    semantic_body_path: None | str | Unset = UNSET
    activate_on_approval: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        content = self.content

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        review_id: None | str | Unset
        if isinstance(self.review_id, Unset):
            review_id = UNSET
        else:
            review_id = self.review_id

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        else:
            kind = self.kind

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        changes: None | str | Unset
        if isinstance(self.changes, Unset):
            changes = UNSET
        else:
            changes = self.changes

        resolves: list[Any] | None | Unset
        if isinstance(self.resolves, Unset):
            resolves = UNSET
        elif isinstance(self.resolves, list):
            resolves = self.resolves

        else:
            resolves = self.resolves

        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        semantic_body: None | str | Unset
        if isinstance(self.semantic_body, Unset):
            semantic_body = UNSET
        else:
            semantic_body = self.semantic_body

        semantic_body_path: None | str | Unset
        if isinstance(self.semantic_body_path, Unset):
            semantic_body_path = UNSET
        else:
            semantic_body_path = self.semantic_body_path

        activate_on_approval: bool | None | Unset
        if isinstance(self.activate_on_approval, Unset):
            activate_on_approval = UNSET
        else:
            activate_on_approval = self.activate_on_approval

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "content": content,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if review_id is not UNSET:
            field_dict["review_id"] = review_id
        if kind is not UNSET:
            field_dict["kind"] = kind
        if title is not UNSET:
            field_dict["title"] = title
        if changes is not UNSET:
            field_dict["changes"] = changes
        if resolves is not UNSET:
            field_dict["resolves"] = resolves
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if semantic_body is not UNSET:
            field_dict["semantic_body"] = semantic_body
        if semantic_body_path is not UNSET:
            field_dict["semantic_body_path"] = semantic_body_path
        if activate_on_approval is not UNSET:
            field_dict["activate_on_approval"] = activate_on_approval

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content = d.pop("content")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_review_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        review_id = _parse_review_id(d.pop("review_id", UNSET))

        def _parse_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_changes(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        changes = _parse_changes(d.pop("changes", UNSET))

        def _parse_resolves(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                resolves_type_0 = cast(list[Any], data)

                return resolves_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        resolves = _parse_resolves(d.pop("resolves", UNSET))

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_semantic_body(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        semantic_body = _parse_semantic_body(d.pop("semantic_body", UNSET))

        def _parse_semantic_body_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        semantic_body_path = _parse_semantic_body_path(d.pop("semantic_body_path", UNSET))

        def _parse_activate_on_approval(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        activate_on_approval = _parse_activate_on_approval(d.pop("activate_on_approval", UNSET))

        review_submit_request = cls(
            content=content,
            project_id=project_id,
            task_id=task_id,
            review_id=review_id,
            kind=kind,
            title=title,
            changes=changes,
            resolves=resolves,
            playbook_id=playbook_id,
            semantic_body=semantic_body,
            semantic_body_path=semantic_body_path,
            activate_on_approval=activate_on_approval,
        )

        review_submit_request.additional_properties = d
        return review_submit_request

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
