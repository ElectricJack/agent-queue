from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.omission import Omission


T = TypeVar("T", bound="SelectionPolicy")


@_attrs_define
class SelectionPolicy:
    """
    Attributes:
        question_schema_version (int):
        model (str):
        omission (Omission):
        version (Literal[1] | Unset):  Default: 1.
    """

    question_schema_version: int
    model: str
    omission: Omission
    version: Literal[1] | Unset = 1

    def to_dict(self) -> dict[str, Any]:
        question_schema_version = self.question_schema_version

        model = self.model

        omission = self.omission.to_dict()

        version = self.version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "question_schema_version": question_schema_version,
                "model": model,
                "omission": omission,
            }
        )
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.omission import Omission

        d = dict(src_dict)
        question_schema_version = d.pop("question_schema_version")

        model = d.pop("model")

        omission = Omission.from_dict(d.pop("omission"))

        version = cast(Literal[1] | Unset, d.pop("version", UNSET))
        if version != 1 and not isinstance(version, Unset):
            raise ValueError(f"version must match const 1, got '{version}'")

        selection_policy = cls(
            question_schema_version=question_schema_version,
            model=model,
            omission=omission,
            version=version,
        )

        return selection_policy
