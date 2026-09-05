"""Project-onboarding command contract (design §5, §8).

The contract lands before any behaviour: every later package builds against
these request/response types, the stable error codes, and the seven command
names wired through ``CommandHandler``.  The handler bodies are stubs that
answer a structured ``not_implemented`` error, so these tests pin the
*shape* of the surface — validation, dispatch, scope, and the generated
API — rather than any onboarding behaviour.
"""

from __future__ import annotations

import pytest

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.scope import check_command_scope
from src.commands.contracts.project_onboarding import (
    ONBOARDING_COMMANDS,
    REQUEST_MODELS,
    RESULT_MODELS,
    GithubCloneOnboardingRequest,
    InitOnboardingRequest,
    LinkOnboardingRequest,
    OnboardProjectResult,
    ProjectOnboardingError,
    ProjectOnboardingErrorCode,
    parse_onboard_project_request,
    parse_request,
)

COMMON = {
    "request_id": "req-1",
    "root_id": "development",
    "relative_path": "my-repo",
    "project_name": "My Repo",
    "project_id": "my-repo",
}

SEVEN = {
    "list_project_roots",
    "browse_project_root",
    "get_github_auth_status",
    "list_github_owners",
    "search_github_repositories",
    "onboard_project",
    "get_project_onboarding",
}


def _error_code(exc_info) -> str:
    return exc_info.value.code


# ---------------------------------------------------------------------------
# onboard_project request — discriminated union (§5.3)
# ---------------------------------------------------------------------------


def test_the_seven_commands_are_the_contract():
    assert ONBOARDING_COMMANDS == frozenset(SEVEN)
    assert set(REQUEST_MODELS) == SEVEN
    assert set(RESULT_MODELS) == SEVEN


def test_link_request_parses_with_only_common_fields():
    req = parse_onboard_project_request({**COMMON, "source_mode": "link"})
    assert isinstance(req, LinkOnboardingRequest)
    assert req.source_mode == "link"
    assert req.default_branch is None


def test_init_request_defaults_readme_on_and_github_off():
    req = parse_onboard_project_request({**COMMON, "source_mode": "init"})
    assert isinstance(req, InitOnboardingRequest)
    assert req.create_readme is True
    assert req.create_github is False
    assert req.github_visibility == "private"


def test_init_request_with_github_creation_carries_owner_and_visibility():
    req = parse_onboard_project_request(
        {
            **COMMON,
            "source_mode": "init",
            "create_github": True,
            "github_owner": "jkern",
            "github_repo": "my-repo",
            "github_visibility": "public",
        }
    )
    assert isinstance(req, InitOnboardingRequest)
    assert req.github_owner == "jkern"
    assert req.github_visibility == "public"


def test_github_clone_request_accepts_a_discovered_repository():
    req = parse_onboard_project_request(
        {
            **COMMON,
            "source_mode": "github_clone",
            "github_repository": {"owner": "jkern", "name": "my-repo"},
        }
    )
    assert isinstance(req, GithubCloneOnboardingRequest)
    assert req.github_repository.owner == "jkern"
    assert req.github_url is None


def test_github_clone_request_accepts_a_pasted_url():
    req = parse_onboard_project_request(
        {**COMMON, "source_mode": "github_clone", "github_url": "https://github.com/jkern/my-repo"}
    )
    assert isinstance(req, GithubCloneOnboardingRequest)
    assert req.github_repository is None


def test_unknown_fields_are_rejected():
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "link", "surprise": 1})
    assert _error_code(exc_info) == "invalid_request"
    assert any(e["field"] == "surprise" for e in exc_info.value.field_errors)


def test_a_link_request_carrying_init_fields_is_rejected():
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "link", "create_readme": True})
    assert _error_code(exc_info) == "invalid_request"
    assert any(e["field"] == "create_readme" for e in exc_info.value.field_errors)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {
            "github_repository": {"owner": "jkern", "name": "my-repo"},
            "github_url": "https://github.com/jkern/my-repo",
        },
    ],
    ids=["neither", "both"],
)
def test_github_clone_requires_exactly_one_source(extra):
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "github_clone", **extra})
    assert _error_code(exc_info) == "invalid_request"


def test_init_with_create_github_but_no_owner_is_rejected():
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "init", "create_github": True})
    assert _error_code(exc_info) == "invalid_request"
    assert any(e["field"] == "github_owner" for e in exc_info.value.field_errors)


def test_init_github_fields_without_create_github_are_rejected():
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "init", "github_owner": "jkern"})
    assert _error_code(exc_info) == "invalid_request"


def test_unknown_source_mode_is_rejected():
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "svn"})
    assert _error_code(exc_info) == "invalid_request"
    assert any(e["field"] == "source_mode" for e in exc_info.value.field_errors)


@pytest.mark.parametrize(
    "relative_path", ["/abs/path", "../escape", "a/../b", "nul\x00byte", "", "   "]
)
def test_syntactically_unsafe_relative_paths_are_rejected(relative_path):
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request(
            {**COMMON, "source_mode": "link", "relative_path": relative_path}
        )
    assert _error_code(exc_info) == "invalid_request"
    assert any(e["field"] == "relative_path" for e in exc_info.value.field_errors)


@pytest.mark.parametrize("project_id", ["My Repo", "-leading", "trailing-", "a/b", ""])
def test_project_id_must_be_a_url_safe_slug(project_id):
    with pytest.raises(ProjectOnboardingError) as exc_info:
        parse_onboard_project_request({**COMMON, "source_mode": "link", "project_id": project_id})
    assert _error_code(exc_info) == "invalid_request"
    assert any(e["field"] == "project_id" for e in exc_info.value.field_errors)


def test_nested_browse_directory_paths_are_allowed():
    req = parse_request("browse_project_root", {"root_id": "development", "relative_path": "a/b"})
    assert req.relative_path == "a/b"


def test_browse_defaults_to_the_root_itself():
    req = parse_request("browse_project_root", {"root_id": "development"})
    assert req.relative_path == ""


def test_search_query_is_bounded():
    with pytest.raises(ProjectOnboardingError):
        parse_request("search_github_repositories", {"query": "x" * 300})
    with pytest.raises(ProjectOnboardingError):
        parse_request("search_github_repositories", {"query": "aq", "limit": 500})
    assert parse_request("search_github_repositories", {"query": "aq"}).limit == 20


def test_parse_request_rejects_an_unknown_command():
    with pytest.raises(KeyError):
        parse_request("delete_project", {})


# ---------------------------------------------------------------------------
# Errors and the success payload (§5.3, §8)
# ---------------------------------------------------------------------------


def test_stable_error_codes_cover_the_design_list():
    codes = {c.value for c in ProjectOnboardingErrorCode}
    assert {
        "project_id_conflict",
        "destination_conflict",
        "destination_locked",
        "invalid_git_repository",
        "root_escape",
        "root_unavailable",
        "github_cli_missing",
        "github_auth_required",
        "github_repository_inaccessible",
        "github_repository_conflict",
        "clone_failed",
        "init_failed",
        "commit_failed",
        "push_failed",
        "registration_failed",
        # browsing
        "not_found",
        "not_directory",
        # request-level
        "invalid_request",
        "request_conflict",
        "not_implemented",
    } <= codes


def test_error_serialises_to_the_structured_command_shape():
    err = ProjectOnboardingError(
        ProjectOnboardingErrorCode.PUSH_FAILED,
        "push rejected",
        phase="push",
        details={"github_repository_url": "https://github.com/jkern/my-repo"},
    )
    payload = err.to_dict()
    assert payload["success"] is False
    assert payload["error"] == "push rejected"
    assert payload["error_code"] == "push_failed"
    assert payload["phase"] == "push"
    assert payload["details"]["github_repository_url"].endswith("my-repo")
    assert "field_errors" not in payload


def test_error_rejects_a_code_outside_the_stable_set():
    with pytest.raises(ValueError):
        ProjectOnboardingError("made_up_code", "nope")


def test_success_payload_carries_the_design_fields():
    result = OnboardProjectResult(
        request_id="req-1",
        project_id="my-repo",
        workspace_id="ws-1",
        source_type="link",
        root_id="development",
        relative_path="my-repo",
        canonical_path="/home/jkern/dev/my-repo",
        default_branch="main",
        remote_url=None,
        actions=["project_created", "workspace_registered"],
    )
    assert result.model_dump()["remote_url"] is None
    assert result.actions == ["project_created", "workspace_registered"]


# ---------------------------------------------------------------------------
# CommandHandler wiring — callable, structured not_implemented
# ---------------------------------------------------------------------------

VALID_ARGS = {
    "list_project_roots": {},
    "browse_project_root": {"root_id": "development"},
    "get_github_auth_status": {},
    "list_github_owners": {},
    "search_github_repositories": {"query": "aq"},
    "onboard_project": {**COMMON, "source_mode": "link"},
    "get_project_onboarding": {"request_id": "req-1"},
}


@pytest.mark.parametrize(
    "command", sorted(SEVEN - {"onboard_project", "get_project_onboarding"})
)
async def test_unimplemented_discovery_commands_dispatch_to_structured_stub(
    command_handler_factory, command
):
    handler = await command_handler_factory()
    result = await handler.execute(command, dict(VALID_ARGS[command]))
    assert result["success"] is False
    assert result["error_code"] == "not_implemented"
    assert command in result["error"]


async def test_invalid_arguments_are_rejected_before_the_stub_answers(command_handler_factory):
    handler = await command_handler_factory()
    result = await handler.execute(
        "onboard_project", {**COMMON, "source_mode": "github_clone"}
    )
    assert result["success"] is False
    assert result["error_code"] == "invalid_request"
    assert result["field_errors"]


async def test_a_project_scoped_supervisor_cannot_onboard(command_handler_factory):
    handler = await command_handler_factory()
    handler._current_scope = {"kind": "session", "elevated": True, "project_id": "p"}
    result = await handler._cmd_onboard_project({**COMMON, "source_mode": "link"})
    assert "global admin" in result["error"]
    assert result["error_code"] != "not_implemented"


# ---------------------------------------------------------------------------
# Scope policy (§7) — privileged local / global-admin only
# ---------------------------------------------------------------------------


def test_the_scope_module_gates_exactly_the_contract_commands():
    # ``src.api.scope`` is a leaf and lists the names literally; this is the
    # drift guard that keeps its copy equal to the contract's set.
    from src.api.scope import PROJECT_ONBOARDING_COMMANDS

    assert PROJECT_ONBOARDING_COMMANDS == ONBOARDING_COMMANDS


@pytest.mark.parametrize("command", sorted(SEVEN))
def test_local_and_global_admin_scopes_may_run_the_commands(command):
    assert check_command_scope(command, {}, LOCAL_SCOPE) is None
    admin = RequestScope(kind="session", session_id="s", elevated=True)
    assert check_command_scope(command, {}, admin) is None


@pytest.mark.parametrize("command", sorted(SEVEN))
def test_project_scoped_and_plain_sessions_are_refused(command):
    supervisor = RequestScope(kind="session", session_id="s", elevated=True, project_id="p1")
    assert "global admin" in (check_command_scope(command, {}, supervisor) or "")
    worker = RequestScope(kind="session", session_id="s", task_id="t", project_id="p1")
    assert (check_command_scope(command, {}, worker) or "").startswith("out of scope")


# ---------------------------------------------------------------------------
# Surface — typed tool definitions, API routes, response models
# ---------------------------------------------------------------------------


def test_tool_definitions_exist_in_the_project_category():
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

    by_name = {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}
    for command in SEVEN:
        assert command in by_name, command
        assert _TOOL_CATEGORIES[command] == "project"
    onboard = by_name["onboard_project"]["input_schema"]
    assert set(onboard["required"]) == {
        "request_id", "source_mode", "root_id", "relative_path", "project_name", "project_id",
    }
    assert onboard["properties"]["source_mode"]["enum"] == ["link", "init", "github_clone"]


def test_the_seven_commands_have_response_models():
    from src.api.models import get_all_response_models

    models = get_all_response_models()
    for command in SEVEN:
        assert command in models, command
    assert models["onboard_project"].model_fields.keys() >= {
        "project_id", "workspace_id", "source_type", "root_id", "relative_path",
        "canonical_path", "default_branch", "remote_url", "actions",
    }


def test_the_seven_commands_appear_in_the_offline_openapi_spec():
    from src.api.spec import build_openapi_spec

    spec = build_openapi_spec()
    operation_ids = {
        op.get("operationId")
        for methods in spec["paths"].values()
        for op in methods.values()
        if isinstance(op, dict)
    }
    assert SEVEN <= operation_ids
