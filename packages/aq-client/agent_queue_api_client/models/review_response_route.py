from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.review_response_route_class_summaries import ReviewResponseRouteClassSummaries


T = TypeVar("T", bound="ReviewResponseRoute")


@_attrs_define
class ReviewResponseRoute:
    """
    Attributes:
        kind (str):
        summary (str):
        class_id (None | str | Unset):
        profile_id (None | str | Unset):
        source (None | str | Unset):
        selected_class (None | str | Unset):
        selected_profile (None | str | Unset):
        class_summaries (ReviewResponseRouteClassSummaries | Unset):
    """

    kind: str
    summary: str
    class_id: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    source: None | str | Unset = UNSET
    selected_class: None | str | Unset = UNSET
    selected_profile: None | str | Unset = UNSET
    class_summaries: ReviewResponseRouteClassSummaries | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        summary = self.summary

        class_id: None | str | Unset
        if isinstance(self.class_id, Unset):
            class_id = UNSET
        else:
            class_id = self.class_id

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        source: None | str | Unset
        if isinstance(self.source, Unset):
            source = UNSET
        else:
            source = self.source

        selected_class: None | str | Unset
        if isinstance(self.selected_class, Unset):
            selected_class = UNSET
        else:
            selected_class = self.selected_class

        selected_profile: None | str | Unset
        if isinstance(self.selected_profile, Unset):
            selected_profile = UNSET
        else:
            selected_profile = self.selected_profile

        class_summaries: dict[str, Any] | Unset = UNSET
        if not isinstance(self.class_summaries, Unset):
            class_summaries = self.class_summaries.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "summary": summary,
            }
        )
        if class_id is not UNSET:
            field_dict["class_id"] = class_id
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if source is not UNSET:
            field_dict["source"] = source
        if selected_class is not UNSET:
            field_dict["selected_class"] = selected_class
        if selected_profile is not UNSET:
            field_dict["selected_profile"] = selected_profile
        if class_summaries is not UNSET:
            field_dict["class_summaries"] = class_summaries

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.review_response_route_class_summaries import ReviewResponseRouteClassSummaries

        d = dict(src_dict)
        kind = d.pop("kind")

        summary = d.pop("summary")

        def _parse_class_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        class_id = _parse_class_id(d.pop("class_id", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source = _parse_source(d.pop("source", UNSET))

        def _parse_selected_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        selected_class = _parse_selected_class(d.pop("selected_class", UNSET))

        def _parse_selected_profile(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        selected_profile = _parse_selected_profile(d.pop("selected_profile", UNSET))

        _class_summaries = d.pop("class_summaries", UNSET)
        class_summaries: ReviewResponseRouteClassSummaries | Unset
        if isinstance(_class_summaries, Unset):
            class_summaries = UNSET
        else:
            class_summaries = ReviewResponseRouteClassSummaries.from_dict(_class_summaries)

        review_response_route = cls(
            kind=kind,
            summary=summary,
            class_id=class_id,
            profile_id=profile_id,
            source=source,
            selected_class=selected_class,
            selected_profile=selected_profile,
            class_summaries=class_summaries,
        )

        review_response_route.additional_properties = d
        return review_response_route

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
