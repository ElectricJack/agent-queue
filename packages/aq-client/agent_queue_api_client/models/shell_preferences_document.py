from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..models.shell_preferences_document_scope import ShellPreferencesDocumentScope

if TYPE_CHECKING:
    from ..models.shell_preferences import ShellPreferences


T = TypeVar("T", bound="ShellPreferencesDocument")


@_attrs_define
class ShellPreferencesDocument:
    """
    Attributes:
        scope (ShellPreferencesDocumentScope):
        owner_id (str):
        subject (None | str):
        revision (int):
        exists (bool):
        updated_at (float | None):
        namespace (Literal['shell_preferences']):
        value (ShellPreferences):
    """

    scope: ShellPreferencesDocumentScope
    owner_id: str
    subject: None | str
    revision: int
    exists: bool
    updated_at: float | None
    namespace: Literal["shell_preferences"]
    value: ShellPreferences

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
        from ..models.shell_preferences import ShellPreferences

        d = dict(src_dict)
        scope = ShellPreferencesDocumentScope(d.pop("scope"))

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

        namespace = cast(Literal["shell_preferences"], d.pop("namespace"))
        if namespace != "shell_preferences":
            raise ValueError(f"namespace must match const 'shell_preferences', got '{namespace}'")

        value = ShellPreferences.from_dict(d.pop("value"))

        shell_preferences_document = cls(
            scope=scope,
            owner_id=owner_id,
            subject=subject,
            revision=revision,
            exists=exists,
            updated_at=updated_at,
            namespace=namespace,
            value=value,
        )

        return shell_preferences_document
