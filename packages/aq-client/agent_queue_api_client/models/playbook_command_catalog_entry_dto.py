from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_command_catalog_entry_dto_parameters_schema import (
        PlaybookCommandCatalogEntryDTOParametersSchema,
    )


T = TypeVar("T", bound="PlaybookCommandCatalogEntryDTO")


@_attrs_define
class PlaybookCommandCatalogEntryDTO:
    """One contract's dashboard- and CLI-facing discovery metadata.

    Attributes:
        name (str):
        title (str):
        summary (str):
        docs_url (str):
        parameters_schema (PlaybookCommandCatalogEntryDTOParametersSchema | Unset):
        origin (None | str | Unset):
        plugin (None | str | Unset):
        available (bool | Unset):  Default: True.
        execution_available (bool | Unset):  Default: False.
        denied (bool | Unset):  Default: False.
        denial_reason (None | str | Unset):
        schema_source (None | str | Unset):
        schema_quality (None | str | Unset):
        result_mode (None | str | Unset):
        capability (None | str | Unset):
        caller_requirements (list[str] | Unset):
        effect_class (None | str | Unset):
        idempotency_mechanism (None | str | Unset):
        idempotency_key_field (None | str | Unset):
        retry_safe (bool | Unset):  Default: False.
        timeout_seconds (int | None | Unset):
        preview_available (bool | Unset):  Default: False.
        sensitivity (None | str | Unset):
        receipt_projection (list[str] | Unset):
    """

    name: str
    title: str
    summary: str
    docs_url: str
    parameters_schema: PlaybookCommandCatalogEntryDTOParametersSchema | Unset = UNSET
    origin: None | str | Unset = UNSET
    plugin: None | str | Unset = UNSET
    available: bool | Unset = True
    execution_available: bool | Unset = False
    denied: bool | Unset = False
    denial_reason: None | str | Unset = UNSET
    schema_source: None | str | Unset = UNSET
    schema_quality: None | str | Unset = UNSET
    result_mode: None | str | Unset = UNSET
    capability: None | str | Unset = UNSET
    caller_requirements: list[str] | Unset = UNSET
    effect_class: None | str | Unset = UNSET
    idempotency_mechanism: None | str | Unset = UNSET
    idempotency_key_field: None | str | Unset = UNSET
    retry_safe: bool | Unset = False
    timeout_seconds: int | None | Unset = UNSET
    preview_available: bool | Unset = False
    sensitivity: None | str | Unset = UNSET
    receipt_projection: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        title = self.title

        summary = self.summary

        docs_url = self.docs_url

        parameters_schema: dict[str, Any] | Unset = UNSET
        if not isinstance(self.parameters_schema, Unset):
            parameters_schema = self.parameters_schema.to_dict()

        origin: None | str | Unset
        if isinstance(self.origin, Unset):
            origin = UNSET
        else:
            origin = self.origin

        plugin: None | str | Unset
        if isinstance(self.plugin, Unset):
            plugin = UNSET
        else:
            plugin = self.plugin

        available = self.available

        execution_available = self.execution_available

        denied = self.denied

        denial_reason: None | str | Unset
        if isinstance(self.denial_reason, Unset):
            denial_reason = UNSET
        else:
            denial_reason = self.denial_reason

        schema_source: None | str | Unset
        if isinstance(self.schema_source, Unset):
            schema_source = UNSET
        else:
            schema_source = self.schema_source

        schema_quality: None | str | Unset
        if isinstance(self.schema_quality, Unset):
            schema_quality = UNSET
        else:
            schema_quality = self.schema_quality

        result_mode: None | str | Unset
        if isinstance(self.result_mode, Unset):
            result_mode = UNSET
        else:
            result_mode = self.result_mode

        capability: None | str | Unset
        if isinstance(self.capability, Unset):
            capability = UNSET
        else:
            capability = self.capability

        caller_requirements: list[str] | Unset = UNSET
        if not isinstance(self.caller_requirements, Unset):
            caller_requirements = self.caller_requirements

        effect_class: None | str | Unset
        if isinstance(self.effect_class, Unset):
            effect_class = UNSET
        else:
            effect_class = self.effect_class

        idempotency_mechanism: None | str | Unset
        if isinstance(self.idempotency_mechanism, Unset):
            idempotency_mechanism = UNSET
        else:
            idempotency_mechanism = self.idempotency_mechanism

        idempotency_key_field: None | str | Unset
        if isinstance(self.idempotency_key_field, Unset):
            idempotency_key_field = UNSET
        else:
            idempotency_key_field = self.idempotency_key_field

        retry_safe = self.retry_safe

        timeout_seconds: int | None | Unset
        if isinstance(self.timeout_seconds, Unset):
            timeout_seconds = UNSET
        else:
            timeout_seconds = self.timeout_seconds

        preview_available = self.preview_available

        sensitivity: None | str | Unset
        if isinstance(self.sensitivity, Unset):
            sensitivity = UNSET
        else:
            sensitivity = self.sensitivity

        receipt_projection: list[str] | Unset = UNSET
        if not isinstance(self.receipt_projection, Unset):
            receipt_projection = self.receipt_projection

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "name": name,
                "title": title,
                "summary": summary,
                "docs_url": docs_url,
            }
        )
        if parameters_schema is not UNSET:
            field_dict["parameters_schema"] = parameters_schema
        if origin is not UNSET:
            field_dict["origin"] = origin
        if plugin is not UNSET:
            field_dict["plugin"] = plugin
        if available is not UNSET:
            field_dict["available"] = available
        if execution_available is not UNSET:
            field_dict["execution_available"] = execution_available
        if denied is not UNSET:
            field_dict["denied"] = denied
        if denial_reason is not UNSET:
            field_dict["denial_reason"] = denial_reason
        if schema_source is not UNSET:
            field_dict["schema_source"] = schema_source
        if schema_quality is not UNSET:
            field_dict["schema_quality"] = schema_quality
        if result_mode is not UNSET:
            field_dict["result_mode"] = result_mode
        if capability is not UNSET:
            field_dict["capability"] = capability
        if caller_requirements is not UNSET:
            field_dict["caller_requirements"] = caller_requirements
        if effect_class is not UNSET:
            field_dict["effect_class"] = effect_class
        if idempotency_mechanism is not UNSET:
            field_dict["idempotency_mechanism"] = idempotency_mechanism
        if idempotency_key_field is not UNSET:
            field_dict["idempotency_key_field"] = idempotency_key_field
        if retry_safe is not UNSET:
            field_dict["retry_safe"] = retry_safe
        if timeout_seconds is not UNSET:
            field_dict["timeout_seconds"] = timeout_seconds
        if preview_available is not UNSET:
            field_dict["preview_available"] = preview_available
        if sensitivity is not UNSET:
            field_dict["sensitivity"] = sensitivity
        if receipt_projection is not UNSET:
            field_dict["receipt_projection"] = receipt_projection

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_command_catalog_entry_dto_parameters_schema import (
            PlaybookCommandCatalogEntryDTOParametersSchema,
        )

        d = dict(src_dict)
        name = d.pop("name")

        title = d.pop("title")

        summary = d.pop("summary")

        docs_url = d.pop("docs_url")

        _parameters_schema = d.pop("parameters_schema", UNSET)
        parameters_schema: PlaybookCommandCatalogEntryDTOParametersSchema | Unset
        if isinstance(_parameters_schema, Unset):
            parameters_schema = UNSET
        else:
            parameters_schema = PlaybookCommandCatalogEntryDTOParametersSchema.from_dict(_parameters_schema)

        def _parse_origin(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        origin = _parse_origin(d.pop("origin", UNSET))

        def _parse_plugin(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plugin = _parse_plugin(d.pop("plugin", UNSET))

        available = d.pop("available", UNSET)

        execution_available = d.pop("execution_available", UNSET)

        denied = d.pop("denied", UNSET)

        def _parse_denial_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        denial_reason = _parse_denial_reason(d.pop("denial_reason", UNSET))

        def _parse_schema_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        schema_source = _parse_schema_source(d.pop("schema_source", UNSET))

        def _parse_schema_quality(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        schema_quality = _parse_schema_quality(d.pop("schema_quality", UNSET))

        def _parse_result_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_mode = _parse_result_mode(d.pop("result_mode", UNSET))

        def _parse_capability(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        capability = _parse_capability(d.pop("capability", UNSET))

        caller_requirements = cast(list[str], d.pop("caller_requirements", UNSET))

        def _parse_effect_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effect_class = _parse_effect_class(d.pop("effect_class", UNSET))

        def _parse_idempotency_mechanism(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_mechanism = _parse_idempotency_mechanism(d.pop("idempotency_mechanism", UNSET))

        def _parse_idempotency_key_field(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_key_field = _parse_idempotency_key_field(d.pop("idempotency_key_field", UNSET))

        retry_safe = d.pop("retry_safe", UNSET)

        def _parse_timeout_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        timeout_seconds = _parse_timeout_seconds(d.pop("timeout_seconds", UNSET))

        preview_available = d.pop("preview_available", UNSET)

        def _parse_sensitivity(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sensitivity = _parse_sensitivity(d.pop("sensitivity", UNSET))

        receipt_projection = cast(list[str], d.pop("receipt_projection", UNSET))

        playbook_command_catalog_entry_dto = cls(
            name=name,
            title=title,
            summary=summary,
            docs_url=docs_url,
            parameters_schema=parameters_schema,
            origin=origin,
            plugin=plugin,
            available=available,
            execution_available=execution_available,
            denied=denied,
            denial_reason=denial_reason,
            schema_source=schema_source,
            schema_quality=schema_quality,
            result_mode=result_mode,
            capability=capability,
            caller_requirements=caller_requirements,
            effect_class=effect_class,
            idempotency_mechanism=idempotency_mechanism,
            idempotency_key_field=idempotency_key_field,
            retry_safe=retry_safe,
            timeout_seconds=timeout_seconds,
            preview_available=preview_available,
            sensitivity=sensitivity,
            receipt_projection=receipt_projection,
        )

        return playbook_command_catalog_entry_dto
