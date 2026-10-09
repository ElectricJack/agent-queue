from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_config import AgentConfig
    from ..models.capabilities import Capabilities


T = TypeVar("T", bound="PolicyAgentSettings")


@_attrs_define
class PolicyAgentSettings:
    """
    Attributes:
        name (str):
        type_ (Literal['agent_settings'] | Unset):  Default: 'agent_settings'.
        extends (str | Unset):  Default: ''.
        template (bool | Unset):  Default: False.
        config (AgentConfig | Unset):
        capabilities (Capabilities | None | Unset):
        role (str | Unset):  Default: ''.
        rules (str | Unset):  Default: ''.
        override (bool | Unset):  Default: False.
        allowed_tools (list[str] | Unset):
    """

    name: str
    type_: Literal["agent_settings"] | Unset = "agent_settings"
    extends: str | Unset = ""
    template: bool | Unset = False
    config: AgentConfig | Unset = UNSET
    capabilities: Capabilities | None | Unset = UNSET
    role: str | Unset = ""
    rules: str | Unset = ""
    override: bool | Unset = False
    allowed_tools: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.capabilities import Capabilities

        name = self.name

        type_ = self.type_

        extends = self.extends

        template = self.template

        config: dict[str, Any] | Unset = UNSET
        if not isinstance(self.config, Unset):
            config = self.config.to_dict()

        capabilities: dict[str, Any] | None | Unset
        if isinstance(self.capabilities, Unset):
            capabilities = UNSET
        elif isinstance(self.capabilities, Capabilities):
            capabilities = self.capabilities.to_dict()
        else:
            capabilities = self.capabilities

        role = self.role

        rules = self.rules

        override = self.override

        allowed_tools: list[str] | Unset = UNSET
        if not isinstance(self.allowed_tools, Unset):
            allowed_tools = self.allowed_tools

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "name": name,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_
        if extends is not UNSET:
            field_dict["extends"] = extends
        if template is not UNSET:
            field_dict["template"] = template
        if config is not UNSET:
            field_dict["config"] = config
        if capabilities is not UNSET:
            field_dict["capabilities"] = capabilities
        if role is not UNSET:
            field_dict["role"] = role
        if rules is not UNSET:
            field_dict["rules"] = rules
        if override is not UNSET:
            field_dict["override"] = override
        if allowed_tools is not UNSET:
            field_dict["allowed_tools"] = allowed_tools

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_config import AgentConfig
        from ..models.capabilities import Capabilities

        d = dict(src_dict)
        name = d.pop("name")

        type_ = cast(Literal["agent_settings"] | Unset, d.pop("type", UNSET))
        if type_ != "agent_settings" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'agent_settings', got '{type_}'")

        extends = d.pop("extends", UNSET)

        template = d.pop("template", UNSET)

        _config = d.pop("config", UNSET)
        config: AgentConfig | Unset
        if isinstance(_config, Unset):
            config = UNSET
        else:
            config = AgentConfig.from_dict(_config)

        def _parse_capabilities(data: object) -> Capabilities | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                capabilities_type_0 = Capabilities.from_dict(data)

                return capabilities_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Capabilities | None | Unset, data)

        capabilities = _parse_capabilities(d.pop("capabilities", UNSET))

        role = d.pop("role", UNSET)

        rules = d.pop("rules", UNSET)

        override = d.pop("override", UNSET)

        allowed_tools = cast(list[str], d.pop("allowed_tools", UNSET))

        policy_agent_settings = cls(
            name=name,
            type_=type_,
            extends=extends,
            template=template,
            config=config,
            capabilities=capabilities,
            role=role,
            rules=rules,
            override=override,
            allowed_tools=allowed_tools,
        )

        return policy_agent_settings
