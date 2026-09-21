from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_availability_status_evidence_item import ProviderAvailabilityStatusEvidenceItem
    from ..models.provider_override import ProviderOverride
    from ..models.provider_transition import ProviderTransition
    from ..models.provider_usage_reading import ProviderUsageReading


T = TypeVar("T", bound="ProviderAvailabilityStatus")


@_attrs_define
class ProviderAvailabilityStatus:
    """One provider's availability, as the CLI, API, doctor and cards show it.

    Every field is server-derived; the dashboard never recomputes a state.
    ``state`` is the *effective* state (the override while one is active);
    ``derived_*`` is what the evidence alone says.  ``until`` is the expected
    recovery -- the provider's own reset clock or a backoff deadline -- and
    ``None`` when nothing is known (``unauthenticated`` needs a human).

        Attributes:
            provider (str):
            state (str):
            half (str):
            vendor (str | Unset):  Default: ''.
            reason_code (str | Unset):  Default: ''.
            reason (str | Unset):  Default: ''.
            since (float | None | Unset):
            until (float | None | Unset):
            derived_state (str | Unset):  Default: ''.
            derived_reason (str | Unset):  Default: ''.
            derived_until (float | None | Unset):
            override (None | ProviderOverride | Unset):
            held (int | Unset):  Default: 0.
            rerouted (int | Unset):  Default: 0.
            batch_id (None | str | Unset):
            level (int | Unset):  Default: 0.
            generation (int | Unset):  Default: 0.
            consecutive_failures (int | Unset):  Default: 0.
            last_failure_at (float | None | Unset):
            last_success_at (float | None | Unset):
            last_probe_at (float | None | Unset):
            probation (bool | Unset):  Default: False.
            remediation (str | Unset):  Default: ''.
            mode (str | Unset):  Default: 'enforce'.
            usage (None | ProviderUsageReading | Unset):
            evidence (list[ProviderAvailabilityStatusEvidenceItem] | Unset):
            transitions (list[ProviderTransition] | Unset):
            updated_at (float | None | Unset):
    """

    provider: str
    state: str
    half: str
    vendor: str | Unset = ""
    reason_code: str | Unset = ""
    reason: str | Unset = ""
    since: float | None | Unset = UNSET
    until: float | None | Unset = UNSET
    derived_state: str | Unset = ""
    derived_reason: str | Unset = ""
    derived_until: float | None | Unset = UNSET
    override: None | ProviderOverride | Unset = UNSET
    held: int | Unset = 0
    rerouted: int | Unset = 0
    batch_id: None | str | Unset = UNSET
    level: int | Unset = 0
    generation: int | Unset = 0
    consecutive_failures: int | Unset = 0
    last_failure_at: float | None | Unset = UNSET
    last_success_at: float | None | Unset = UNSET
    last_probe_at: float | None | Unset = UNSET
    probation: bool | Unset = False
    remediation: str | Unset = ""
    mode: str | Unset = "enforce"
    usage: None | ProviderUsageReading | Unset = UNSET
    evidence: list[ProviderAvailabilityStatusEvidenceItem] | Unset = UNSET
    transitions: list[ProviderTransition] | Unset = UNSET
    updated_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_override import ProviderOverride
        from ..models.provider_usage_reading import ProviderUsageReading

        provider = self.provider

        state = self.state

        half = self.half

        vendor = self.vendor

        reason_code = self.reason_code

        reason = self.reason

        since: float | None | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        derived_state = self.derived_state

        derived_reason = self.derived_reason

        derived_until: float | None | Unset
        if isinstance(self.derived_until, Unset):
            derived_until = UNSET
        else:
            derived_until = self.derived_until

        override: dict[str, Any] | None | Unset
        if isinstance(self.override, Unset):
            override = UNSET
        elif isinstance(self.override, ProviderOverride):
            override = self.override.to_dict()
        else:
            override = self.override

        held = self.held

        rerouted = self.rerouted

        batch_id: None | str | Unset
        if isinstance(self.batch_id, Unset):
            batch_id = UNSET
        else:
            batch_id = self.batch_id

        level = self.level

        generation = self.generation

        consecutive_failures = self.consecutive_failures

        last_failure_at: float | None | Unset
        if isinstance(self.last_failure_at, Unset):
            last_failure_at = UNSET
        else:
            last_failure_at = self.last_failure_at

        last_success_at: float | None | Unset
        if isinstance(self.last_success_at, Unset):
            last_success_at = UNSET
        else:
            last_success_at = self.last_success_at

        last_probe_at: float | None | Unset
        if isinstance(self.last_probe_at, Unset):
            last_probe_at = UNSET
        else:
            last_probe_at = self.last_probe_at

        probation = self.probation

        remediation = self.remediation

        mode = self.mode

        usage: dict[str, Any] | None | Unset
        if isinstance(self.usage, Unset):
            usage = UNSET
        elif isinstance(self.usage, ProviderUsageReading):
            usage = self.usage.to_dict()
        else:
            usage = self.usage

        evidence: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.evidence, Unset):
            evidence = []
            for evidence_item_data in self.evidence:
                evidence_item = evidence_item_data.to_dict()
                evidence.append(evidence_item)

        transitions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.transitions, Unset):
            transitions = []
            for transitions_item_data in self.transitions:
                transitions_item = transitions_item_data.to_dict()
                transitions.append(transitions_item)

        updated_at: float | None | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "state": state,
                "half": half,
            }
        )
        if vendor is not UNSET:
            field_dict["vendor"] = vendor
        if reason_code is not UNSET:
            field_dict["reason_code"] = reason_code
        if reason is not UNSET:
            field_dict["reason"] = reason
        if since is not UNSET:
            field_dict["since"] = since
        if until is not UNSET:
            field_dict["until"] = until
        if derived_state is not UNSET:
            field_dict["derived_state"] = derived_state
        if derived_reason is not UNSET:
            field_dict["derived_reason"] = derived_reason
        if derived_until is not UNSET:
            field_dict["derived_until"] = derived_until
        if override is not UNSET:
            field_dict["override"] = override
        if held is not UNSET:
            field_dict["held"] = held
        if rerouted is not UNSET:
            field_dict["rerouted"] = rerouted
        if batch_id is not UNSET:
            field_dict["batch_id"] = batch_id
        if level is not UNSET:
            field_dict["level"] = level
        if generation is not UNSET:
            field_dict["generation"] = generation
        if consecutive_failures is not UNSET:
            field_dict["consecutive_failures"] = consecutive_failures
        if last_failure_at is not UNSET:
            field_dict["last_failure_at"] = last_failure_at
        if last_success_at is not UNSET:
            field_dict["last_success_at"] = last_success_at
        if last_probe_at is not UNSET:
            field_dict["last_probe_at"] = last_probe_at
        if probation is not UNSET:
            field_dict["probation"] = probation
        if remediation is not UNSET:
            field_dict["remediation"] = remediation
        if mode is not UNSET:
            field_dict["mode"] = mode
        if usage is not UNSET:
            field_dict["usage"] = usage
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if transitions is not UNSET:
            field_dict["transitions"] = transitions
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_availability_status_evidence_item import ProviderAvailabilityStatusEvidenceItem
        from ..models.provider_override import ProviderOverride
        from ..models.provider_transition import ProviderTransition
        from ..models.provider_usage_reading import ProviderUsageReading

        d = dict(src_dict)
        provider = d.pop("provider")

        state = d.pop("state")

        half = d.pop("half")

        vendor = d.pop("vendor", UNSET)

        reason_code = d.pop("reason_code", UNSET)

        reason = d.pop("reason", UNSET)

        def _parse_since(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        derived_state = d.pop("derived_state", UNSET)

        derived_reason = d.pop("derived_reason", UNSET)

        def _parse_derived_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        derived_until = _parse_derived_until(d.pop("derived_until", UNSET))

        def _parse_override(data: object) -> None | ProviderOverride | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                override_type_0 = ProviderOverride.from_dict(data)

                return override_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderOverride | Unset, data)

        override = _parse_override(d.pop("override", UNSET))

        held = d.pop("held", UNSET)

        rerouted = d.pop("rerouted", UNSET)

        def _parse_batch_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        batch_id = _parse_batch_id(d.pop("batch_id", UNSET))

        level = d.pop("level", UNSET)

        generation = d.pop("generation", UNSET)

        consecutive_failures = d.pop("consecutive_failures", UNSET)

        def _parse_last_failure_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_failure_at = _parse_last_failure_at(d.pop("last_failure_at", UNSET))

        def _parse_last_success_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_success_at = _parse_last_success_at(d.pop("last_success_at", UNSET))

        def _parse_last_probe_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_probe_at = _parse_last_probe_at(d.pop("last_probe_at", UNSET))

        probation = d.pop("probation", UNSET)

        remediation = d.pop("remediation", UNSET)

        mode = d.pop("mode", UNSET)

        def _parse_usage(data: object) -> None | ProviderUsageReading | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                usage_type_0 = ProviderUsageReading.from_dict(data)

                return usage_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderUsageReading | Unset, data)

        usage = _parse_usage(d.pop("usage", UNSET))

        _evidence = d.pop("evidence", UNSET)
        evidence: list[ProviderAvailabilityStatusEvidenceItem] | Unset = UNSET
        if _evidence is not UNSET:
            evidence = []
            for evidence_item_data in _evidence:
                evidence_item = ProviderAvailabilityStatusEvidenceItem.from_dict(evidence_item_data)

                evidence.append(evidence_item)

        _transitions = d.pop("transitions", UNSET)
        transitions: list[ProviderTransition] | Unset = UNSET
        if _transitions is not UNSET:
            transitions = []
            for transitions_item_data in _transitions:
                transitions_item = ProviderTransition.from_dict(transitions_item_data)

                transitions.append(transitions_item)

        def _parse_updated_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))

        provider_availability_status = cls(
            provider=provider,
            state=state,
            half=half,
            vendor=vendor,
            reason_code=reason_code,
            reason=reason,
            since=since,
            until=until,
            derived_state=derived_state,
            derived_reason=derived_reason,
            derived_until=derived_until,
            override=override,
            held=held,
            rerouted=rerouted,
            batch_id=batch_id,
            level=level,
            generation=generation,
            consecutive_failures=consecutive_failures,
            last_failure_at=last_failure_at,
            last_success_at=last_success_at,
            last_probe_at=last_probe_at,
            probation=probation,
            remediation=remediation,
            mode=mode,
            usage=usage,
            evidence=evidence,
            transitions=transitions,
            updated_at=updated_at,
        )

        provider_availability_status.additional_properties = d
        return provider_availability_status

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
