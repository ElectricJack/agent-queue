from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..models.item_original_scope import ItemOriginalScope
from ..models.item_type import ItemType

if TYPE_CHECKING:
    from ..models.ci_test_policy import CITestPolicy
    from ..models.playbook_policy import PlaybookPolicy
    from ..models.policy_agent_settings import PolicyAgentSettings
    from ..models.promotion_policy import PromotionPolicy
    from ..models.routing_preferences import RoutingPreferences
    from ..models.template_policy import TemplatePolicy


T = TypeVar("T", bound="Item")


@_attrs_define
class Item:
    """
    Attributes:
        id (str):
        name (str):
        type_ (ItemType):
        original_scope (ItemOriginalScope):
        checksum (str):
        payload (CITestPolicy | PlaybookPolicy | PolicyAgentSettings | PromotionPolicy | RoutingPreferences |
            TemplatePolicy):
    """

    id: str
    name: str
    type_: ItemType
    original_scope: ItemOriginalScope
    checksum: str
    payload: CITestPolicy | PlaybookPolicy | PolicyAgentSettings | PromotionPolicy | RoutingPreferences | TemplatePolicy

    def to_dict(self) -> dict[str, Any]:
        from ..models.ci_test_policy import CITestPolicy
        from ..models.playbook_policy import PlaybookPolicy
        from ..models.policy_agent_settings import PolicyAgentSettings
        from ..models.promotion_policy import PromotionPolicy
        from ..models.routing_preferences import RoutingPreferences

        id = self.id

        name = self.name

        type_ = self.type_.value

        original_scope = self.original_scope.value

        checksum = self.checksum

        payload: dict[str, Any]
        if isinstance(self.payload, PlaybookPolicy):
            payload = self.payload.to_dict()
        elif isinstance(self.payload, PolicyAgentSettings):
            payload = self.payload.to_dict()
        elif isinstance(self.payload, PromotionPolicy):
            payload = self.payload.to_dict()
        elif isinstance(self.payload, CITestPolicy):
            payload = self.payload.to_dict()
        elif isinstance(self.payload, RoutingPreferences):
            payload = self.payload.to_dict()
        else:
            payload = self.payload.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "name": name,
                "type": type_,
                "original_scope": original_scope,
                "checksum": checksum,
                "payload": payload,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ci_test_policy import CITestPolicy
        from ..models.playbook_policy import PlaybookPolicy
        from ..models.policy_agent_settings import PolicyAgentSettings
        from ..models.promotion_policy import PromotionPolicy
        from ..models.routing_preferences import RoutingPreferences
        from ..models.template_policy import TemplatePolicy

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        type_ = ItemType(d.pop("type"))

        original_scope = ItemOriginalScope(d.pop("original_scope"))

        checksum = d.pop("checksum")

        def _parse_payload(
            data: object,
        ) -> (
            CITestPolicy | PlaybookPolicy | PolicyAgentSettings | PromotionPolicy | RoutingPreferences | TemplatePolicy
        ):
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                payload_type_0 = PlaybookPolicy.from_dict(data)

                return payload_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                payload_type_1 = PolicyAgentSettings.from_dict(data)

                return payload_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                payload_type_2 = PromotionPolicy.from_dict(data)

                return payload_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                payload_type_3 = CITestPolicy.from_dict(data)

                return payload_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                payload_type_4 = RoutingPreferences.from_dict(data)

                return payload_type_4
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            payload_type_5 = TemplatePolicy.from_dict(data)

            return payload_type_5

        payload = _parse_payload(d.pop("payload"))

        item = cls(
            id=id,
            name=name,
            type_=type_,
            original_scope=original_scope,
            checksum=checksum,
            payload=payload,
        )

        return item
