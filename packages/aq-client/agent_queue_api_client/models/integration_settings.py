from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..models.integration_settings_engine import IntegrationSettingsEngine
from ..models.integration_settings_hierarchical_integration_mode import IntegrationSettingsHierarchicalIntegrationMode
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.integration_settings_policy_type_0 import IntegrationSettingsPolicyType0
    from ..models.integration_settings_promotion_flow_type_0_item import IntegrationSettingsPromotionFlowType0Item


T = TypeVar("T", bound="IntegrationSettings")


@_attrs_define
class IntegrationSettings:
    """Portable inputs, never a source installation's activation identities.

    Attributes:
        engine (IntegrationSettingsEngine):
        hierarchical_integration_mode (IntegrationSettingsHierarchicalIntegrationMode):
        default_branch (str):
        type_ (Literal['integration_settings'] | Unset):  Default: 'integration_settings'.
        version (Literal[1] | Unset):  Default: 1.
        integration_mode (Literal['pull_request'] | Unset):  Default: 'pull_request'.
        integration_repository_id (None | str | Unset):
        policy (IntegrationSettingsPolicyType0 | None | Unset):
        promotion_flow (list[IntegrationSettingsPromotionFlowType0Item] | None | Unset):
    """

    engine: IntegrationSettingsEngine
    hierarchical_integration_mode: IntegrationSettingsHierarchicalIntegrationMode
    default_branch: str
    type_: Literal["integration_settings"] | Unset = "integration_settings"
    version: Literal[1] | Unset = 1
    integration_mode: Literal["pull_request"] | Unset = "pull_request"
    integration_repository_id: None | str | Unset = UNSET
    policy: IntegrationSettingsPolicyType0 | None | Unset = UNSET
    promotion_flow: list[IntegrationSettingsPromotionFlowType0Item] | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.integration_settings_policy_type_0 import IntegrationSettingsPolicyType0

        engine = self.engine.value

        hierarchical_integration_mode = self.hierarchical_integration_mode.value

        default_branch = self.default_branch

        type_ = self.type_

        version = self.version

        integration_mode = self.integration_mode

        integration_repository_id: None | str | Unset
        if isinstance(self.integration_repository_id, Unset):
            integration_repository_id = UNSET
        else:
            integration_repository_id = self.integration_repository_id

        policy: dict[str, Any] | None | Unset
        if isinstance(self.policy, Unset):
            policy = UNSET
        elif isinstance(self.policy, IntegrationSettingsPolicyType0):
            policy = self.policy.to_dict()
        else:
            policy = self.policy

        promotion_flow: list[dict[str, Any]] | None | Unset
        if isinstance(self.promotion_flow, Unset):
            promotion_flow = UNSET
        elif isinstance(self.promotion_flow, list):
            promotion_flow = []
            for promotion_flow_type_0_item_data in self.promotion_flow:
                promotion_flow_type_0_item = promotion_flow_type_0_item_data.to_dict()
                promotion_flow.append(promotion_flow_type_0_item)

        else:
            promotion_flow = self.promotion_flow

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "engine": engine,
                "hierarchical_integration_mode": hierarchical_integration_mode,
                "default_branch": default_branch,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_
        if version is not UNSET:
            field_dict["version"] = version
        if integration_mode is not UNSET:
            field_dict["integration_mode"] = integration_mode
        if integration_repository_id is not UNSET:
            field_dict["integration_repository_id"] = integration_repository_id
        if policy is not UNSET:
            field_dict["policy"] = policy
        if promotion_flow is not UNSET:
            field_dict["promotion_flow"] = promotion_flow

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.integration_settings_policy_type_0 import IntegrationSettingsPolicyType0
        from ..models.integration_settings_promotion_flow_type_0_item import IntegrationSettingsPromotionFlowType0Item

        d = dict(src_dict)
        engine = IntegrationSettingsEngine(d.pop("engine"))

        hierarchical_integration_mode = IntegrationSettingsHierarchicalIntegrationMode(
            d.pop("hierarchical_integration_mode")
        )

        default_branch = d.pop("default_branch")

        type_ = cast(Literal["integration_settings"] | Unset, d.pop("type", UNSET))
        if type_ != "integration_settings" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'integration_settings', got '{type_}'")

        version = cast(Literal[1] | Unset, d.pop("version", UNSET))
        if version != 1 and not isinstance(version, Unset):
            raise ValueError(f"version must match const 1, got '{version}'")

        integration_mode = cast(Literal["pull_request"] | Unset, d.pop("integration_mode", UNSET))
        if integration_mode != "pull_request" and not isinstance(integration_mode, Unset):
            raise ValueError(f"integration_mode must match const 'pull_request', got '{integration_mode}'")

        def _parse_integration_repository_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        integration_repository_id = _parse_integration_repository_id(d.pop("integration_repository_id", UNSET))

        def _parse_policy(data: object) -> IntegrationSettingsPolicyType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                policy_type_0 = IntegrationSettingsPolicyType0.from_dict(data)

                return policy_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IntegrationSettingsPolicyType0 | None | Unset, data)

        policy = _parse_policy(d.pop("policy", UNSET))

        def _parse_promotion_flow(data: object) -> list[IntegrationSettingsPromotionFlowType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                promotion_flow_type_0 = []
                _promotion_flow_type_0 = data
                for promotion_flow_type_0_item_data in _promotion_flow_type_0:
                    promotion_flow_type_0_item = IntegrationSettingsPromotionFlowType0Item.from_dict(
                        promotion_flow_type_0_item_data
                    )

                    promotion_flow_type_0.append(promotion_flow_type_0_item)

                return promotion_flow_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[IntegrationSettingsPromotionFlowType0Item] | None | Unset, data)

        promotion_flow = _parse_promotion_flow(d.pop("promotion_flow", UNSET))

        integration_settings = cls(
            engine=engine,
            hierarchical_integration_mode=hierarchical_integration_mode,
            default_branch=default_branch,
            type_=type_,
            version=version,
            integration_mode=integration_mode,
            integration_repository_id=integration_repository_id,
            policy=policy,
            promotion_flow=promotion_flow,
        )

        return integration_settings
