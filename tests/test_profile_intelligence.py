"""Resolved provider/model policy for a profile (``src.profiles.intelligence``).

The semantic graph's AI cards state which provider and model a step's profile
actually runs on; these pin the resolution the cards depend on.
"""

from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile
from src.playbooks.validation import NullProfileLookup, VaultProfileLookup
from src.profiles.intelligence import ProfileIntelligence, intelligence_for


def _classes():
    return {
        "deep-high": IntelligenceClass(
            id="deep-high",
            name="Deep",
            description="",
            mapping={
                "anthropic": {"model": "claude-opus-5", "thinking": "high"},
                "openai": {"model": "gpt-5", "reasoning_effort": "high"},
                "codex": {"model": "gpt-5-codex", "reasoning_effort": "high"},
                "google": {"model": "gemini-3-pro"},
            },
        ),
        "anthropic-only": IntelligenceClass(
            id="anthropic-only",
            name="Anthropic only",
            description="",
            mapping={"anthropic": {"model": "claude-sonnet-5"}},
        ),
        "openai-api-only": IntelligenceClass(
            id="openai-api-only",
            name="OpenAI API only",
            description="",
            mapping={"openai": {"model": "gpt-5"}},
        ),
    }


def _profile(**overrides):
    fields = {"id": "worker", "name": "Worker", "harness": "claude"}
    fields.update(overrides)
    return AgentProfile(**fields)


def test_claude_profile_resolves_class_provider_and_model():
    resolved = intelligence_for(_profile(default_class="deep-high"), _classes())
    assert resolved == ProfileIntelligence("deep-high", "anthropic", "claude-opus-5")


def test_gemini_harness_reads_the_google_slice():
    resolved = intelligence_for(
        _profile(harness="gemini", default_class="deep-high"), _classes()
    )
    assert resolved == ProfileIntelligence("deep-high", "google", "gemini-3-pro")


def test_codex_harness_prefers_the_codex_slice_over_the_openai_api_slice():
    resolved = intelligence_for(
        _profile(harness="codex", default_class="deep-high"), _classes()
    )
    assert resolved == ProfileIntelligence("deep-high", "openai", "gpt-5-codex")


def test_codex_harness_falls_back_to_the_openai_slice_when_no_codex_slice_exists():
    resolved = intelligence_for(
        _profile(harness="codex", default_class="openai-api-only"), _classes()
    )
    assert resolved == ProfileIntelligence("openai-api-only", "openai", "gpt-5")


def test_a_class_without_a_slice_for_the_provider_reports_no_model():
    resolved = intelligence_for(
        _profile(harness="codex", default_class="anthropic-only"), _classes()
    )
    assert resolved == ProfileIntelligence("anthropic-only", "openai", None)


def test_an_unknown_class_still_reports_the_class_and_provider():
    resolved = intelligence_for(_profile(default_class="ghost"), _classes())
    assert resolved == ProfileIntelligence("ghost", "anthropic", None)


def test_no_class_snapshot_reports_no_model_rather_than_a_fallback():
    resolved = intelligence_for(_profile(default_class="deep-high"), None)
    assert resolved == ProfileIntelligence("deep-high", "anthropic", None)


def test_a_profile_without_a_class_still_names_its_provider():
    assert intelligence_for(_profile(), _classes()) == ProfileIntelligence(
        None, "anthropic", None
    )


def test_an_unknown_harness_determines_nothing():
    resolved = intelligence_for(
        _profile(harness="homegrown", default_class="deep-high"), _classes()
    )
    assert resolved == ProfileIntelligence("deep-high", None, None)


def test_vault_lookup_routes_a_known_profile_and_ignores_an_unknown_one():
    profile = _profile(default_class="deep-high")
    lookup = VaultProfileLookup({profile.id: profile}, intelligence_classes=_classes())
    assert lookup.routing("worker") == ProfileIntelligence(
        "deep-high", "anthropic", "claude-opus-5"
    )
    assert lookup.routing("ghost") is None
    assert lookup.profile("worker") is profile


def test_vault_lookup_without_a_class_snapshot_still_reports_class_and_provider():
    profile = _profile(default_class="deep-high")
    lookup = VaultProfileLookup({profile.id: profile})
    assert lookup.routing("worker") == ProfileIntelligence("deep-high", "anthropic", None)


def test_null_lookup_routes_nothing():
    assert NullProfileLookup().routing("worker") is None
