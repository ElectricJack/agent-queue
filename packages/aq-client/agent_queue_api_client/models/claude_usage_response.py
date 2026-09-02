from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claude_usage_response_active_sessions_item import ClaudeUsageResponseActiveSessionsItem
    from ..models.claude_usage_response_model_usage_type_0 import ClaudeUsageResponseModelUsageType0
    from ..models.claude_usage_response_rate_limit_type_0 import ClaudeUsageResponseRateLimitType0


T = TypeVar("T", bound="ClaudeUsageResponse")


@_attrs_define
class ClaudeUsageResponse:
    """
    Attributes:
        subscription (None | str | Unset):
        rate_limit_tier (None | str | Unset):
        active_sessions (list[ClaudeUsageResponseActiveSessionsItem] | Unset):
        active_session_count (int | Unset):  Default: 0.
        active_total_tokens (int | Unset):  Default: 0.
        total_sessions (int | None | Unset):
        total_messages (int | None | Unset):
        model_usage (ClaudeUsageResponseModelUsageType0 | None | Unset):
        stats_date (None | str | Unset):
        stats_error (None | str | Unset):
        rate_limit (ClaudeUsageResponseRateLimitType0 | None | Unset):
        rate_limit_error (None | str | Unset):
    """

    subscription: None | str | Unset = UNSET
    rate_limit_tier: None | str | Unset = UNSET
    active_sessions: list[ClaudeUsageResponseActiveSessionsItem] | Unset = UNSET
    active_session_count: int | Unset = 0
    active_total_tokens: int | Unset = 0
    total_sessions: int | None | Unset = UNSET
    total_messages: int | None | Unset = UNSET
    model_usage: ClaudeUsageResponseModelUsageType0 | None | Unset = UNSET
    stats_date: None | str | Unset = UNSET
    stats_error: None | str | Unset = UNSET
    rate_limit: ClaudeUsageResponseRateLimitType0 | None | Unset = UNSET
    rate_limit_error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.claude_usage_response_model_usage_type_0 import ClaudeUsageResponseModelUsageType0
        from ..models.claude_usage_response_rate_limit_type_0 import ClaudeUsageResponseRateLimitType0

        subscription: None | str | Unset
        if isinstance(self.subscription, Unset):
            subscription = UNSET
        else:
            subscription = self.subscription

        rate_limit_tier: None | str | Unset
        if isinstance(self.rate_limit_tier, Unset):
            rate_limit_tier = UNSET
        else:
            rate_limit_tier = self.rate_limit_tier

        active_sessions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.active_sessions, Unset):
            active_sessions = []
            for active_sessions_item_data in self.active_sessions:
                active_sessions_item = active_sessions_item_data.to_dict()
                active_sessions.append(active_sessions_item)

        active_session_count = self.active_session_count

        active_total_tokens = self.active_total_tokens

        total_sessions: int | None | Unset
        if isinstance(self.total_sessions, Unset):
            total_sessions = UNSET
        else:
            total_sessions = self.total_sessions

        total_messages: int | None | Unset
        if isinstance(self.total_messages, Unset):
            total_messages = UNSET
        else:
            total_messages = self.total_messages

        model_usage: dict[str, Any] | None | Unset
        if isinstance(self.model_usage, Unset):
            model_usage = UNSET
        elif isinstance(self.model_usage, ClaudeUsageResponseModelUsageType0):
            model_usage = self.model_usage.to_dict()
        else:
            model_usage = self.model_usage

        stats_date: None | str | Unset
        if isinstance(self.stats_date, Unset):
            stats_date = UNSET
        else:
            stats_date = self.stats_date

        stats_error: None | str | Unset
        if isinstance(self.stats_error, Unset):
            stats_error = UNSET
        else:
            stats_error = self.stats_error

        rate_limit: dict[str, Any] | None | Unset
        if isinstance(self.rate_limit, Unset):
            rate_limit = UNSET
        elif isinstance(self.rate_limit, ClaudeUsageResponseRateLimitType0):
            rate_limit = self.rate_limit.to_dict()
        else:
            rate_limit = self.rate_limit

        rate_limit_error: None | str | Unset
        if isinstance(self.rate_limit_error, Unset):
            rate_limit_error = UNSET
        else:
            rate_limit_error = self.rate_limit_error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if subscription is not UNSET:
            field_dict["subscription"] = subscription
        if rate_limit_tier is not UNSET:
            field_dict["rate_limit_tier"] = rate_limit_tier
        if active_sessions is not UNSET:
            field_dict["active_sessions"] = active_sessions
        if active_session_count is not UNSET:
            field_dict["active_session_count"] = active_session_count
        if active_total_tokens is not UNSET:
            field_dict["active_total_tokens"] = active_total_tokens
        if total_sessions is not UNSET:
            field_dict["total_sessions"] = total_sessions
        if total_messages is not UNSET:
            field_dict["total_messages"] = total_messages
        if model_usage is not UNSET:
            field_dict["model_usage"] = model_usage
        if stats_date is not UNSET:
            field_dict["stats_date"] = stats_date
        if stats_error is not UNSET:
            field_dict["stats_error"] = stats_error
        if rate_limit is not UNSET:
            field_dict["rate_limit"] = rate_limit
        if rate_limit_error is not UNSET:
            field_dict["rate_limit_error"] = rate_limit_error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claude_usage_response_active_sessions_item import ClaudeUsageResponseActiveSessionsItem
        from ..models.claude_usage_response_model_usage_type_0 import ClaudeUsageResponseModelUsageType0
        from ..models.claude_usage_response_rate_limit_type_0 import ClaudeUsageResponseRateLimitType0

        d = dict(src_dict)

        def _parse_subscription(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subscription = _parse_subscription(d.pop("subscription", UNSET))

        def _parse_rate_limit_tier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rate_limit_tier = _parse_rate_limit_tier(d.pop("rate_limit_tier", UNSET))

        _active_sessions = d.pop("active_sessions", UNSET)
        active_sessions: list[ClaudeUsageResponseActiveSessionsItem] | Unset = UNSET
        if _active_sessions is not UNSET:
            active_sessions = []
            for active_sessions_item_data in _active_sessions:
                active_sessions_item = ClaudeUsageResponseActiveSessionsItem.from_dict(active_sessions_item_data)

                active_sessions.append(active_sessions_item)

        active_session_count = d.pop("active_session_count", UNSET)

        active_total_tokens = d.pop("active_total_tokens", UNSET)

        def _parse_total_sessions(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_sessions = _parse_total_sessions(d.pop("total_sessions", UNSET))

        def _parse_total_messages(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_messages = _parse_total_messages(d.pop("total_messages", UNSET))

        def _parse_model_usage(data: object) -> ClaudeUsageResponseModelUsageType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_usage_type_0 = ClaudeUsageResponseModelUsageType0.from_dict(data)

                return model_usage_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClaudeUsageResponseModelUsageType0 | None | Unset, data)

        model_usage = _parse_model_usage(d.pop("model_usage", UNSET))

        def _parse_stats_date(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stats_date = _parse_stats_date(d.pop("stats_date", UNSET))

        def _parse_stats_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stats_error = _parse_stats_error(d.pop("stats_error", UNSET))

        def _parse_rate_limit(data: object) -> ClaudeUsageResponseRateLimitType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                rate_limit_type_0 = ClaudeUsageResponseRateLimitType0.from_dict(data)

                return rate_limit_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClaudeUsageResponseRateLimitType0 | None | Unset, data)

        rate_limit = _parse_rate_limit(d.pop("rate_limit", UNSET))

        def _parse_rate_limit_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rate_limit_error = _parse_rate_limit_error(d.pop("rate_limit_error", UNSET))

        claude_usage_response = cls(
            subscription=subscription,
            rate_limit_tier=rate_limit_tier,
            active_sessions=active_sessions,
            active_session_count=active_session_count,
            active_total_tokens=active_total_tokens,
            total_sessions=total_sessions,
            total_messages=total_messages,
            model_usage=model_usage,
            stats_date=stats_date,
            stats_error=stats_error,
            rate_limit=rate_limit,
            rate_limit_error=rate_limit_error,
        )

        claude_usage_response.additional_properties = d
        return claude_usage_response

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
