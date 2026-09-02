from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ai_budget_dto import AiBudgetDTO
    from ..models.ai_node_detail_dto_output_schema_type_0 import AiNodeDetailDTOOutputSchemaType0
    from ..models.capability_namespaces_dto import CapabilityNamespacesDTO
    from ..models.delegation_policy_dto import DelegationPolicyDTO


T = TypeVar("T", bound="AiNodeDetailDTO")


@_attrs_define
class AiNodeDetailDTO:
    """Everything an operator needs about an AI state (design spec: "AI cards
    show the profile, resolved capability namespaces, capability fingerprint,
    budgets, and delegation policy").

        Attributes:
            profile_id (str):
            capabilities (CapabilityNamespacesDTO): ``CapabilityPolicy`` projected.  Sorted; empty list means deny-all.
            capability_fingerprint (str):
            budget (AiBudgetDTO):
            intelligence_class (None | str | Unset):
            provider (None | str | Unset):
            model (None | str | Unset):
            output_schema (AiNodeDetailDTOOutputSchemaType0 | None | Unset):
            tool_use_enabled (bool | Unset):  Default: False.
            delegation (DelegationPolicyDTO | None | Unset):
    """

    profile_id: str
    capabilities: CapabilityNamespacesDTO
    capability_fingerprint: str
    budget: AiBudgetDTO
    intelligence_class: None | str | Unset = UNSET
    provider: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    output_schema: AiNodeDetailDTOOutputSchemaType0 | None | Unset = UNSET
    tool_use_enabled: bool | Unset = False
    delegation: DelegationPolicyDTO | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.ai_node_detail_dto_output_schema_type_0 import AiNodeDetailDTOOutputSchemaType0
        from ..models.delegation_policy_dto import DelegationPolicyDTO

        profile_id = self.profile_id

        capabilities = self.capabilities.to_dict()

        capability_fingerprint = self.capability_fingerprint

        budget = self.budget.to_dict()

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        output_schema: dict[str, Any] | None | Unset
        if isinstance(self.output_schema, Unset):
            output_schema = UNSET
        elif isinstance(self.output_schema, AiNodeDetailDTOOutputSchemaType0):
            output_schema = self.output_schema.to_dict()
        else:
            output_schema = self.output_schema

        tool_use_enabled = self.tool_use_enabled

        delegation: dict[str, Any] | None | Unset
        if isinstance(self.delegation, Unset):
            delegation = UNSET
        elif isinstance(self.delegation, DelegationPolicyDTO):
            delegation = self.delegation.to_dict()
        else:
            delegation = self.delegation

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "profile_id": profile_id,
                "capabilities": capabilities,
                "capability_fingerprint": capability_fingerprint,
                "budget": budget,
            }
        )
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if provider is not UNSET:
            field_dict["provider"] = provider
        if model is not UNSET:
            field_dict["model"] = model
        if output_schema is not UNSET:
            field_dict["output_schema"] = output_schema
        if tool_use_enabled is not UNSET:
            field_dict["tool_use_enabled"] = tool_use_enabled
        if delegation is not UNSET:
            field_dict["delegation"] = delegation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ai_budget_dto import AiBudgetDTO
        from ..models.ai_node_detail_dto_output_schema_type_0 import AiNodeDetailDTOOutputSchemaType0
        from ..models.capability_namespaces_dto import CapabilityNamespacesDTO
        from ..models.delegation_policy_dto import DelegationPolicyDTO

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        capabilities = CapabilityNamespacesDTO.from_dict(d.pop("capabilities"))

        capability_fingerprint = d.pop("capability_fingerprint")

        budget = AiBudgetDTO.from_dict(d.pop("budget"))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_output_schema(data: object) -> AiNodeDetailDTOOutputSchemaType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                output_schema_type_0 = AiNodeDetailDTOOutputSchemaType0.from_dict(data)

                return output_schema_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AiNodeDetailDTOOutputSchemaType0 | None | Unset, data)

        output_schema = _parse_output_schema(d.pop("output_schema", UNSET))

        tool_use_enabled = d.pop("tool_use_enabled", UNSET)

        def _parse_delegation(data: object) -> DelegationPolicyDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                delegation_type_0 = DelegationPolicyDTO.from_dict(data)

                return delegation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DelegationPolicyDTO | None | Unset, data)

        delegation = _parse_delegation(d.pop("delegation", UNSET))

        ai_node_detail_dto = cls(
            profile_id=profile_id,
            capabilities=capabilities,
            capability_fingerprint=capability_fingerprint,
            budget=budget,
            intelligence_class=intelligence_class,
            provider=provider,
            model=model,
            output_schema=output_schema,
            tool_use_enabled=tool_use_enabled,
            delegation=delegation,
        )

        return ai_node_detail_dto
