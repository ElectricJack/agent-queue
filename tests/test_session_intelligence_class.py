"""Session launch consumes intelligence class → provider → model."""
from types import SimpleNamespace

from src.intelligence_classes import IntelligenceClass, resolve_class


def test_resolve_returns_model_and_thinking():
    cls = IntelligenceClass(
        id="fast",
        name="Fast",
        description="",
        mapping={"anthropic": {"model": "claude-haiku-4-5", "thinking": "off"}},
    )
    assert resolve_class(cls, "anthropic")["model"] == "claude-haiku-4-5"


def test_compose_argv_uses_resolved_model(tmp_path, monkeypatch):
    from src.sessions.spec import SessionSpecBuilder

    # Fake harness + profile + task
    harness = SimpleNamespace(
        id="claude",
        command="claude",
        args=["--dangerously-skip-permissions"],
        model_flag="--model",
        effort_flag=None,
        permission_flag=None,
        settings_flag=None,
        session_id_flag=None,
        resume=SimpleNamespace(style="none", flag="", subcommand=""),
        prompt_mode="none",
        prompt_flag="",
        max_argv_prompt_bytes=1024,
        provider="anthropic",
    )
    profile = SimpleNamespace(model="claude-sonnet-4-6", default_class="", effort="")
    classes = {
        "fast": IntelligenceClass(
            id="fast", name="Fast", description="",
            mapping={"anthropic": {"model": "claude-haiku-4-5"}},
        )
    }

    class _Cfg:
        mcp_server = None
        security = None
        data_dir = str(tmp_path)

    builder = SessionSpecBuilder(_Cfg(), intelligence_classes=classes)
    argv = builder._compose_argv(
        harness=harness, profile=profile, session_id="s1",
        resume_key=None, prompt=None, session_name="s",
        files=[], task_intelligence_class="fast",
    )
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"


def test_compose_argv_falls_back_to_profile_default_class(tmp_path):
    from src.sessions.spec import SessionSpecBuilder

    harness = SimpleNamespace(
        id="claude",
        command="claude",
        args=[],
        model_flag="--model",
        effort_flag=None,
        permission_flag=None,
        settings_flag=None,
        session_id_flag=None,
        resume=SimpleNamespace(style="none", flag="", subcommand=""),
        prompt_mode="none",
        prompt_flag="",
        max_argv_prompt_bytes=1024,
        provider="",  # force inference
    )
    profile = SimpleNamespace(model="claude-sonnet-4-6", default_class="deep", effort="")
    classes = {
        "deep": IntelligenceClass(
            id="deep", name="Deep", description="",
            mapping={"anthropic": {"model": "claude-opus-4-1"}},
        )
    }

    class _Cfg:
        mcp_server = None
        security = None
        data_dir = str(tmp_path)

    builder = SessionSpecBuilder(_Cfg(), intelligence_classes=classes)
    argv = builder._compose_argv(
        harness=harness, profile=profile, session_id="s1",
        resume_key=None, prompt=None, session_name="s",
        files=[], task_intelligence_class=None,
    )
    # provider inferred from harness.id "claude" → anthropic
    assert argv[argv.index("--model") + 1] == "claude-opus-4-1"


def test_compose_argv_unknown_class_falls_back_to_profile_model(tmp_path):
    from src.sessions.spec import SessionSpecBuilder

    harness = SimpleNamespace(
        id="claude",
        command="claude",
        args=[],
        model_flag="--model",
        effort_flag=None,
        permission_flag=None,
        settings_flag=None,
        session_id_flag=None,
        resume=SimpleNamespace(style="none", flag="", subcommand=""),
        prompt_mode="none",
        prompt_flag="",
        max_argv_prompt_bytes=1024,
        provider="anthropic",
    )
    profile = SimpleNamespace(model="claude-sonnet-4-6", default_class="", effort="")

    class _Cfg:
        mcp_server = None
        security = None
        data_dir = str(tmp_path)

    builder = SessionSpecBuilder(_Cfg(), intelligence_classes={})
    argv = builder._compose_argv(
        harness=harness, profile=profile, session_id="s1",
        resume_key=None, prompt=None, session_name="s",
        files=[], task_intelligence_class="bogus-class-name",
    )
    # Unknown class → fall back to profile.model (no launch failure)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


def test_provider_inference_from_harness_command():
    from src.sessions.spec import _infer_provider_from_harness

    assert _infer_provider_from_harness(SimpleNamespace(id="claude", command="claude")) == "anthropic"
    assert _infer_provider_from_harness(SimpleNamespace(id="codex", command="codex")) == "openai"
    assert _infer_provider_from_harness(SimpleNamespace(id="gemini", command="gemini")) == "google"
    assert _infer_provider_from_harness(SimpleNamespace(id="", command="unknown")) == ""
