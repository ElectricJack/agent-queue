from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_policy_artifact import PlaybookPolicyArtifact


T = TypeVar("T", bound="PlaybookPolicy")


@_attrs_define
class PlaybookPolicy:
    """
    Attributes:
        source (str):
        artifact (PlaybookPolicyArtifact):
        type_ (Literal['playbook'] | Unset):  Default: 'playbook'.
        dependencies (list[str] | Unset):
    """

    source: str
    artifact: PlaybookPolicyArtifact
    type_: Literal["playbook"] | Unset = "playbook"
    dependencies: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        artifact = self.artifact.to_dict()

        type_ = self.type_

        dependencies: list[str] | Unset = UNSET
        if not isinstance(self.dependencies, Unset):
            dependencies = self.dependencies

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "source": source,
                "artifact": artifact,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_
        if dependencies is not UNSET:
            field_dict["dependencies"] = dependencies

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_policy_artifact import PlaybookPolicyArtifact

        d = dict(src_dict)
        source = d.pop("source")

        artifact = PlaybookPolicyArtifact.from_dict(d.pop("artifact"))

        type_ = cast(Literal["playbook"] | Unset, d.pop("type", UNSET))
        if type_ != "playbook" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'playbook', got '{type_}'")

        dependencies = cast(list[str], d.pop("dependencies", UNSET))

        playbook_policy = cls(
            source=source,
            artifact=artifact,
            type_=type_,
            dependencies=dependencies,
        )

        return playbook_policy
