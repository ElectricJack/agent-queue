from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..models.template_policy_kind import TemplatePolicyKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="TemplatePolicy")


@_attrs_define
class TemplatePolicy:
    """
    Attributes:
        kind (TemplatePolicyKind):
        content (str):
        type_ (Literal['template'] | Unset):  Default: 'template'.
    """

    kind: TemplatePolicyKind
    content: str
    type_: Literal["template"] | Unset = "template"

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        content = self.content

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
                "content": content,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = TemplatePolicyKind(d.pop("kind"))

        content = d.pop("content")

        type_ = cast(Literal["template"] | Unset, d.pop("type", UNSET))
        if type_ != "template" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'template', got '{type_}'")

        template_policy = cls(
            kind=kind,
            content=content,
            type_=type_,
        )

        return template_policy
