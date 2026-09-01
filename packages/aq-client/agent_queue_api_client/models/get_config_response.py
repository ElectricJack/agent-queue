from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.env_var_reference import EnvVarReference
    from ..models.get_config_response_config import GetConfigResponseConfig


T = TypeVar("T", bound="GetConfigResponse")


@_attrs_define
class GetConfigResponse:
    """
    Attributes:
        path (str | Unset):  Default: ''.
        config (GetConfigResponseConfig | Unset):
        section (None | str | Unset):
        hot_reloadable (list[str] | Unset):
        restart_required (list[str] | Unset):
        unclassified (list[str] | Unset):
        env_var_references (list[EnvVarReference] | Unset):
        error (None | str | Unset):
    """

    path: str | Unset = ""
    config: GetConfigResponseConfig | Unset = UNSET
    section: None | str | Unset = UNSET
    hot_reloadable: list[str] | Unset = UNSET
    restart_required: list[str] | Unset = UNSET
    unclassified: list[str] | Unset = UNSET
    env_var_references: list[EnvVarReference] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        config: dict[str, Any] | Unset = UNSET
        if not isinstance(self.config, Unset):
            config = self.config.to_dict()

        section: None | str | Unset
        if isinstance(self.section, Unset):
            section = UNSET
        else:
            section = self.section

        hot_reloadable: list[str] | Unset = UNSET
        if not isinstance(self.hot_reloadable, Unset):
            hot_reloadable = self.hot_reloadable

        restart_required: list[str] | Unset = UNSET
        if not isinstance(self.restart_required, Unset):
            restart_required = self.restart_required

        unclassified: list[str] | Unset = UNSET
        if not isinstance(self.unclassified, Unset):
            unclassified = self.unclassified

        env_var_references: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.env_var_references, Unset):
            env_var_references = []
            for env_var_references_item_data in self.env_var_references:
                env_var_references_item = env_var_references_item_data.to_dict()
                env_var_references.append(env_var_references_item)

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if path is not UNSET:
            field_dict["path"] = path
        if config is not UNSET:
            field_dict["config"] = config
        if section is not UNSET:
            field_dict["section"] = section
        if hot_reloadable is not UNSET:
            field_dict["hot_reloadable"] = hot_reloadable
        if restart_required is not UNSET:
            field_dict["restart_required"] = restart_required
        if unclassified is not UNSET:
            field_dict["unclassified"] = unclassified
        if env_var_references is not UNSET:
            field_dict["env_var_references"] = env_var_references
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.env_var_reference import EnvVarReference  # noqa: PLC0415
        from ..models.get_config_response_config import GetConfigResponseConfig  # noqa: PLC0415

        d = dict(src_dict)
        path = d.pop("path", UNSET)

        _config = d.pop("config", UNSET)
        config: GetConfigResponseConfig | Unset
        if isinstance(_config, Unset):
            config = UNSET
        else:
            config = GetConfigResponseConfig.from_dict(_config)

        def _parse_section(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        section = _parse_section(d.pop("section", UNSET))

        hot_reloadable = cast(list[str], d.pop("hot_reloadable", UNSET))

        restart_required = cast(list[str], d.pop("restart_required", UNSET))

        unclassified = cast(list[str], d.pop("unclassified", UNSET))

        _env_var_references = d.pop("env_var_references", UNSET)
        env_var_references: list[EnvVarReference] | Unset = UNSET
        if _env_var_references is not UNSET:
            env_var_references = []
            for env_var_references_item_data in _env_var_references:
                env_var_references_item = EnvVarReference.from_dict(env_var_references_item_data)

                env_var_references.append(env_var_references_item)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        get_config_response = cls(
            path=path,
            config=config,
            section=section,
            hot_reloadable=hot_reloadable,
            restart_required=restart_required,
            unclassified=unclassified,
            env_var_references=env_var_references,
            error=error,
        )

        get_config_response.additional_properties = d
        return get_config_response

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
