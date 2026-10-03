from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_authority_grant_request_review_type_0 import KnowledgeAuthorityGrantRequestReviewType0


T = TypeVar("T", bound="KnowledgeAuthorityGrantRequest")


@_attrs_define
class KnowledgeAuthorityGrantRequest:
    """
    Attributes:
        identity (str):
        reason (str):
        idempotency_key (str):
        project_id (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
        if_revision (None | str | Unset):
        review (KnowledgeAuthorityGrantRequestReviewType0 | None | Unset):
    """

    identity: str
    reason: str
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    if_revision: None | str | Unset = UNSET
    review: KnowledgeAuthorityGrantRequestReviewType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_authority_grant_request_review_type_0 import KnowledgeAuthorityGrantRequestReviewType0

        identity = self.identity

        reason = self.reason

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        global_scope = self.global_scope

        if_revision: None | str | Unset
        if isinstance(self.if_revision, Unset):
            if_revision = UNSET
        else:
            if_revision = self.if_revision

        review: dict[str, Any] | None | Unset
        if isinstance(self.review, Unset):
            review = UNSET
        elif isinstance(self.review, KnowledgeAuthorityGrantRequestReviewType0):
            review = self.review.to_dict()
        else:
            review = self.review

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "identity": identity,
                "reason": reason,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope
        if if_revision is not UNSET:
            field_dict["if_revision"] = if_revision
        if review is not UNSET:
            field_dict["review"] = review

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_authority_grant_request_review_type_0 import KnowledgeAuthorityGrantRequestReviewType0

        d = dict(src_dict)
        identity = d.pop("identity")

        reason = d.pop("reason")

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        def _parse_if_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_revision = _parse_if_revision(d.pop("if_revision", UNSET))

        def _parse_review(data: object) -> KnowledgeAuthorityGrantRequestReviewType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                review_type_0 = KnowledgeAuthorityGrantRequestReviewType0.from_dict(data)

                return review_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeAuthorityGrantRequestReviewType0 | None | Unset, data)

        review = _parse_review(d.pop("review", UNSET))

        knowledge_authority_grant_request = cls(
            identity=identity,
            reason=reason,
            idempotency_key=idempotency_key,
            project_id=project_id,
            global_scope=global_scope,
            if_revision=if_revision,
            review=review,
        )

        knowledge_authority_grant_request.additional_properties = d
        return knowledge_authority_grant_request

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
