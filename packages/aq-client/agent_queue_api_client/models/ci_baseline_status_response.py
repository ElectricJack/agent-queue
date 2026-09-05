from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CiBaselineStatusResponse")


@_attrs_define
class CiBaselineStatusResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        project_id (str | Unset):  Default: ''.
        ref (str | Unset):  Default: ''.
        head_sha (None | str | Unset):
        state (str | Unset):  Default: 'unknown'.
        failing_checks (list[str] | Unset):
        pending_checks (list[str] | Unset):
        failing_tests (list[str] | Unset):
        run_url (None | str | Unset):
        signature (None | str | Unset):
        attempt (int | Unset):  Default: 0.
        prior_attempts (list[str] | Unset):
        escalated (bool | Unset):  Default: False.
        dedup_key (None | str | Unset):
        title (None | str | Unset):
        description (None | str | Unset):
        escalation_key (None | str | Unset):
        escalation_title (None | str | Unset):
        escalation_question (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool | Unset = True
    project_id: str | Unset = ""
    ref: str | Unset = ""
    head_sha: None | str | Unset = UNSET
    state: str | Unset = "unknown"
    failing_checks: list[str] | Unset = UNSET
    pending_checks: list[str] | Unset = UNSET
    failing_tests: list[str] | Unset = UNSET
    run_url: None | str | Unset = UNSET
    signature: None | str | Unset = UNSET
    attempt: int | Unset = 0
    prior_attempts: list[str] | Unset = UNSET
    escalated: bool | Unset = False
    dedup_key: None | str | Unset = UNSET
    title: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    escalation_key: None | str | Unset = UNSET
    escalation_title: None | str | Unset = UNSET
    escalation_question: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        project_id = self.project_id

        ref = self.ref

        head_sha: None | str | Unset
        if isinstance(self.head_sha, Unset):
            head_sha = UNSET
        else:
            head_sha = self.head_sha

        state = self.state

        failing_checks: list[str] | Unset = UNSET
        if not isinstance(self.failing_checks, Unset):
            failing_checks = self.failing_checks

        pending_checks: list[str] | Unset = UNSET
        if not isinstance(self.pending_checks, Unset):
            pending_checks = self.pending_checks

        failing_tests: list[str] | Unset = UNSET
        if not isinstance(self.failing_tests, Unset):
            failing_tests = self.failing_tests

        run_url: None | str | Unset
        if isinstance(self.run_url, Unset):
            run_url = UNSET
        else:
            run_url = self.run_url

        signature: None | str | Unset
        if isinstance(self.signature, Unset):
            signature = UNSET
        else:
            signature = self.signature

        attempt = self.attempt

        prior_attempts: list[str] | Unset = UNSET
        if not isinstance(self.prior_attempts, Unset):
            prior_attempts = self.prior_attempts

        escalated = self.escalated

        dedup_key: None | str | Unset
        if isinstance(self.dedup_key, Unset):
            dedup_key = UNSET
        else:
            dedup_key = self.dedup_key

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        escalation_key: None | str | Unset
        if isinstance(self.escalation_key, Unset):
            escalation_key = UNSET
        else:
            escalation_key = self.escalation_key

        escalation_title: None | str | Unset
        if isinstance(self.escalation_title, Unset):
            escalation_title = UNSET
        else:
            escalation_title = self.escalation_title

        escalation_question: None | str | Unset
        if isinstance(self.escalation_question, Unset):
            escalation_question = UNSET
        else:
            escalation_question = self.escalation_question

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if ref is not UNSET:
            field_dict["ref"] = ref
        if head_sha is not UNSET:
            field_dict["head_sha"] = head_sha
        if state is not UNSET:
            field_dict["state"] = state
        if failing_checks is not UNSET:
            field_dict["failing_checks"] = failing_checks
        if pending_checks is not UNSET:
            field_dict["pending_checks"] = pending_checks
        if failing_tests is not UNSET:
            field_dict["failing_tests"] = failing_tests
        if run_url is not UNSET:
            field_dict["run_url"] = run_url
        if signature is not UNSET:
            field_dict["signature"] = signature
        if attempt is not UNSET:
            field_dict["attempt"] = attempt
        if prior_attempts is not UNSET:
            field_dict["prior_attempts"] = prior_attempts
        if escalated is not UNSET:
            field_dict["escalated"] = escalated
        if dedup_key is not UNSET:
            field_dict["dedup_key"] = dedup_key
        if title is not UNSET:
            field_dict["title"] = title
        if description is not UNSET:
            field_dict["description"] = description
        if escalation_key is not UNSET:
            field_dict["escalation_key"] = escalation_key
        if escalation_title is not UNSET:
            field_dict["escalation_title"] = escalation_title
        if escalation_question is not UNSET:
            field_dict["escalation_question"] = escalation_question
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        project_id = d.pop("project_id", UNSET)

        ref = d.pop("ref", UNSET)

        def _parse_head_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        head_sha = _parse_head_sha(d.pop("head_sha", UNSET))

        state = d.pop("state", UNSET)

        failing_checks = cast(list[str], d.pop("failing_checks", UNSET))

        pending_checks = cast(list[str], d.pop("pending_checks", UNSET))

        failing_tests = cast(list[str], d.pop("failing_tests", UNSET))

        def _parse_run_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_url = _parse_run_url(d.pop("run_url", UNSET))

        def _parse_signature(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        signature = _parse_signature(d.pop("signature", UNSET))

        attempt = d.pop("attempt", UNSET)

        prior_attempts = cast(list[str], d.pop("prior_attempts", UNSET))

        escalated = d.pop("escalated", UNSET)

        def _parse_dedup_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dedup_key = _parse_dedup_key(d.pop("dedup_key", UNSET))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_escalation_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        escalation_key = _parse_escalation_key(d.pop("escalation_key", UNSET))

        def _parse_escalation_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        escalation_title = _parse_escalation_title(d.pop("escalation_title", UNSET))

        def _parse_escalation_question(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        escalation_question = _parse_escalation_question(d.pop("escalation_question", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        ci_baseline_status_response = cls(
            success=success,
            project_id=project_id,
            ref=ref,
            head_sha=head_sha,
            state=state,
            failing_checks=failing_checks,
            pending_checks=pending_checks,
            failing_tests=failing_tests,
            run_url=run_url,
            signature=signature,
            attempt=attempt,
            prior_attempts=prior_attempts,
            escalated=escalated,
            dedup_key=dedup_key,
            title=title,
            description=description,
            escalation_key=escalation_key,
            escalation_title=escalation_title,
            escalation_question=escalation_question,
            error=error,
        )

        ci_baseline_status_response.additional_properties = d
        return ci_baseline_status_response

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
