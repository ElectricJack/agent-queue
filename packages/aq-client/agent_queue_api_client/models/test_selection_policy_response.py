from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_selection_policy_response_config import TestSelectionPolicyResponseConfig
    from ..models.test_selection_policy_response_latest_digests_type_0 import (
        TestSelectionPolicyResponseLatestDigestsType0,
    )
    from ..models.test_selection_policy_response_promotion_type_0 import TestSelectionPolicyResponsePromotionType0


T = TypeVar("T", bound="TestSelectionPolicyResponse")


@_attrs_define
class TestSelectionPolicyResponse:
    """
    Attributes:
        config (TestSelectionPolicyResponseConfig):
        promotion (None | TestSelectionPolicyResponsePromotionType0):
        latest_digests (None | TestSelectionPolicyResponseLatestDigestsType0):
        success (bool | Unset):  Default: True.
    """

    config: TestSelectionPolicyResponseConfig
    promotion: None | TestSelectionPolicyResponsePromotionType0
    latest_digests: None | TestSelectionPolicyResponseLatestDigestsType0
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.test_selection_policy_response_latest_digests_type_0 import (
            TestSelectionPolicyResponseLatestDigestsType0,
        )
        from ..models.test_selection_policy_response_promotion_type_0 import TestSelectionPolicyResponsePromotionType0

        config = self.config.to_dict()

        promotion: dict[str, Any] | None
        if isinstance(self.promotion, TestSelectionPolicyResponsePromotionType0):
            promotion = self.promotion.to_dict()
        else:
            promotion = self.promotion

        latest_digests: dict[str, Any] | None
        if isinstance(self.latest_digests, TestSelectionPolicyResponseLatestDigestsType0):
            latest_digests = self.latest_digests.to_dict()
        else:
            latest_digests = self.latest_digests

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "config": config,
                "promotion": promotion,
                "latest_digests": latest_digests,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_selection_policy_response_config import TestSelectionPolicyResponseConfig
        from ..models.test_selection_policy_response_latest_digests_type_0 import (
            TestSelectionPolicyResponseLatestDigestsType0,
        )
        from ..models.test_selection_policy_response_promotion_type_0 import TestSelectionPolicyResponsePromotionType0

        d = dict(src_dict)
        config = TestSelectionPolicyResponseConfig.from_dict(d.pop("config"))

        def _parse_promotion(data: object) -> None | TestSelectionPolicyResponsePromotionType0:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                promotion_type_0 = TestSelectionPolicyResponsePromotionType0.from_dict(data)

                return promotion_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TestSelectionPolicyResponsePromotionType0, data)

        promotion = _parse_promotion(d.pop("promotion"))

        def _parse_latest_digests(data: object) -> None | TestSelectionPolicyResponseLatestDigestsType0:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                latest_digests_type_0 = TestSelectionPolicyResponseLatestDigestsType0.from_dict(data)

                return latest_digests_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TestSelectionPolicyResponseLatestDigestsType0, data)

        latest_digests = _parse_latest_digests(d.pop("latest_digests"))

        success = d.pop("success", UNSET)

        test_selection_policy_response = cls(
            config=config,
            promotion=promotion,
            latest_digests=latest_digests,
            success=success,
        )

        test_selection_policy_response.additional_properties = d
        return test_selection_policy_response

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
