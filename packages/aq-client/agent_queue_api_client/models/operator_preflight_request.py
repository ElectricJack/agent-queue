from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operator_preflight_request_policy_type_0 import OperatorPreflightRequestPolicyType0


T = TypeVar("T", bound="OperatorPreflightRequest")


@_attrs_define
class OperatorPreflightRequest:
    """
    Attributes:
        actions (list[Any]):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        branches (list[Any] | None | Unset):
        runner_labels (list[Any] | None | Unset):
        app_ids (list[Any] | None | Unset):
        policy (None | OperatorPreflightRequestPolicyType0 | Unset):
        repository_id (None | str | Unset):
    """

    actions: list[Any]
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    branches: list[Any] | None | Unset = UNSET
    runner_labels: list[Any] | None | Unset = UNSET
    app_ids: list[Any] | None | Unset = UNSET
    policy: None | OperatorPreflightRequestPolicyType0 | Unset = UNSET
    repository_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.operator_preflight_request_policy_type_0 import OperatorPreflightRequestPolicyType0

        actions = self.actions

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        branches: list[Any] | None | Unset
        if isinstance(self.branches, Unset):
            branches = UNSET
        elif isinstance(self.branches, list):
            branches = self.branches

        else:
            branches = self.branches

        runner_labels: list[Any] | None | Unset
        if isinstance(self.runner_labels, Unset):
            runner_labels = UNSET
        elif isinstance(self.runner_labels, list):
            runner_labels = self.runner_labels

        else:
            runner_labels = self.runner_labels

        app_ids: list[Any] | None | Unset
        if isinstance(self.app_ids, Unset):
            app_ids = UNSET
        elif isinstance(self.app_ids, list):
            app_ids = self.app_ids

        else:
            app_ids = self.app_ids

        policy: dict[str, Any] | None | Unset
        if isinstance(self.policy, Unset):
            policy = UNSET
        elif isinstance(self.policy, OperatorPreflightRequestPolicyType0):
            policy = self.policy.to_dict()
        else:
            policy = self.policy

        repository_id: None | str | Unset
        if isinstance(self.repository_id, Unset):
            repository_id = UNSET
        else:
            repository_id = self.repository_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "actions": actions,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if branches is not UNSET:
            field_dict["branches"] = branches
        if runner_labels is not UNSET:
            field_dict["runner_labels"] = runner_labels
        if app_ids is not UNSET:
            field_dict["app_ids"] = app_ids
        if policy is not UNSET:
            field_dict["policy"] = policy
        if repository_id is not UNSET:
            field_dict["repository_id"] = repository_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operator_preflight_request_policy_type_0 import OperatorPreflightRequestPolicyType0

        d = dict(src_dict)
        actions = cast(list[Any], d.pop("actions"))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        def _parse_branches(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                branches_type_0 = cast(list[Any], data)

                return branches_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        branches = _parse_branches(d.pop("branches", UNSET))

        def _parse_runner_labels(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                runner_labels_type_0 = cast(list[Any], data)

                return runner_labels_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        runner_labels = _parse_runner_labels(d.pop("runner_labels", UNSET))

        def _parse_app_ids(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                app_ids_type_0 = cast(list[Any], data)

                return app_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        app_ids = _parse_app_ids(d.pop("app_ids", UNSET))

        def _parse_policy(data: object) -> None | OperatorPreflightRequestPolicyType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                policy_type_0 = OperatorPreflightRequestPolicyType0.from_dict(data)

                return policy_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperatorPreflightRequestPolicyType0 | Unset, data)

        policy = _parse_policy(d.pop("policy", UNSET))

        def _parse_repository_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        repository_id = _parse_repository_id(d.pop("repository_id", UNSET))

        operator_preflight_request = cls(
            actions=actions,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            branches=branches,
            runner_labels=runner_labels,
            app_ids=app_ids,
            policy=policy,
            repository_id=repository_id,
        )

        operator_preflight_request.additional_properties = d
        return operator_preflight_request

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
