from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..models.command_center_project_view_document_scope import CommandCenterProjectViewDocumentScope

if TYPE_CHECKING:
    from ..models.command_center_project_view import CommandCenterProjectView


T = TypeVar("T", bound="CommandCenterProjectViewDocument")


@_attrs_define
class CommandCenterProjectViewDocument:
    """
    Attributes:
        scope (CommandCenterProjectViewDocumentScope):
        owner_id (str):
        subject (None | str):
        revision (int):
        exists (bool):
        updated_at (float | None):
        namespace (Literal['command_center_project_view']):
        value (CommandCenterProjectView):
    """

    scope: CommandCenterProjectViewDocumentScope
    owner_id: str
    subject: None | str
    revision: int
    exists: bool
    updated_at: float | None
    namespace: Literal["command_center_project_view"]
    value: CommandCenterProjectView

    def to_dict(self) -> dict[str, Any]:
        scope = self.scope.value

        owner_id = self.owner_id

        subject: None | str
        subject = self.subject

        revision = self.revision

        exists = self.exists

        updated_at: float | None
        updated_at = self.updated_at

        namespace = self.namespace

        value = self.value.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "scope": scope,
                "owner_id": owner_id,
                "subject": subject,
                "revision": revision,
                "exists": exists,
                "updated_at": updated_at,
                "namespace": namespace,
                "value": value,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.command_center_project_view import CommandCenterProjectView

        d = dict(src_dict)
        scope = CommandCenterProjectViewDocumentScope(d.pop("scope"))

        owner_id = d.pop("owner_id")

        def _parse_subject(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        subject = _parse_subject(d.pop("subject"))

        revision = d.pop("revision")

        exists = d.pop("exists")

        def _parse_updated_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        updated_at = _parse_updated_at(d.pop("updated_at"))

        namespace = cast(Literal["command_center_project_view"], d.pop("namespace"))
        if namespace != "command_center_project_view":
            raise ValueError(f"namespace must match const 'command_center_project_view', got '{namespace}'")

        value = CommandCenterProjectView.from_dict(d.pop("value"))

        command_center_project_view_document = cls(
            scope=scope,
            owner_id=owner_id,
            subject=subject,
            revision=revision,
            exists=exists,
            updated_at=updated_at,
            namespace=namespace,
            value=value,
        )

        return command_center_project_view_document
