"""The installation's Git commit identity: suggestions, the interview and ``config.git-identity``.

``src/install/git_identity.py`` suggests a default from the authenticated
``gh`` account and the global git config; ``config.git-identity`` records an
explicit choice and otherwise only reports.  Every host fact comes through the
runner/``which`` seam, so nothing here touches the network, a real ``gh`` or
the operator's ``~/.agent-queue``.  The wizard and the unattended flags are
exercised through ``aq install`` in ``tests/test_install_cli.py``; the
interactive ``aq system config git-identity`` in ``tests/test_cli_system_config.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from src.git.identity import FALLBACK_IDENTITY, GitIdentity
from src.install import InstallEngine, InstallOptions, InstallOutcome
from src.install.command import CommandOutput
from src.install.git_identity import (
    GH_AUTHENTICATED,
    GH_MISSING,
    GH_TIMEOUT,
    GH_UNAUTHENTICATED,
    GH_UNREACHABLE,
    LABEL_GIT_CONFIG,
    LABEL_NOREPLY,
    LABEL_PUBLIC,
    LABEL_VERIFIED,
    LABEL_VERIFIED_PRIMARY,
    NAME_FROM_GIT_CONFIG,
    NAME_FROM_LOGIN,
    NOT_CONFIGURED_NOTE,
    SOURCE_GIT_CONFIG,
    SOURCE_MANUAL,
    Choice,
    Discovery,
    GitHubAccount,
    GitIdentitySettingError,
    Suggestion,
    build_suggestions,
    configured_identity,
    discover,
    discover_suggestions,
    interview,
    noreply_address,
    parse_setting,
    source_for,
)
from src.install.lifecycle import LifecycleMode
from src.install.onboarding import STEP_GIT_IDENTITY, config_step, git_identity_step
from src.install.prerequisites import data_directory_step, host_step
from src.install.results import StepState
from src.install.steps import StepRegistry
from tests.installer_machine import WSL2, Machine, provisioned, step


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer runs before a database exists; it allocates none of its own."""


OCTOCAT = {"login": "octocat", "id": 583231, "name": "Mona Lisa Octocat", "email": None}


class FakeHost:
    """``gh`` and ``git`` as the probes see them.  An unscripted command fails the test."""

    def __init__(
        self,
        *,
        gh: bool = True,
        git: bool = True,
        user: dict[str, Any] | None = None,
        user_failure: CommandOutput | None = None,
        emails: list[dict[str, Any]] | None = None,
        emails_failure: CommandOutput | None = None,
        git_name: str | None = None,
        git_email: str | None = None,
    ) -> None:
        self.gh = gh
        self.git = git
        self.user = user
        self.user_failure = user_failure
        self.emails = emails
        self.emails_failure = emails_failure
        self.git_name = git_name
        self.git_email = git_email
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def which(self, name: str) -> str | None:
        present = {"gh": self.gh, "git": self.git}.get(name, False)
        return f"/usr/bin/{name}" if present else None

    def run(self, argv, **kwargs) -> CommandOutput:
        argv = tuple(str(part) for part in argv)
        self.calls.append((argv, kwargs))
        if argv[:2] == ("/usr/bin/gh", "api"):
            endpoint = argv[-1]
            assert argv[2:4] == ("--hostname", argv[3])
            if endpoint == "user":
                if self.user_failure is not None:
                    return CommandOutput(argv, **_fields(self.user_failure))
                return CommandOutput(argv, 0, json.dumps(self.user))
            if endpoint == "user/emails":
                if self.emails_failure is not None:
                    return CommandOutput(argv, **_fields(self.emails_failure))
                return CommandOutput(argv, 0, json.dumps(self.emails or []))
        if argv[:4] == ("/usr/bin/git", "config", "--global", "--get"):
            value = {"user.name": self.git_name, "user.email": self.git_email}[argv[4]]
            return CommandOutput(argv, 0 if value else 1, f"{value}\n" if value else "")
        raise AssertionError(f"unexpected command: {argv}")

    def ran(self, fragment: str) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if fragment in " ".join(argv)]


def _fields(output: CommandOutput) -> dict[str, Any]:
    return {
        "returncode": output.returncode,
        "stdout": output.stdout,
        "stderr": output.stderr,
        "error": output.error,
    }


SCOPE_REFUSAL = CommandOutput(
    (),
    returncode=1,
    stderr=(
        "gh: Not Found (HTTP 404)\n"
        'This API operation needs the "user" scope. To request it, run:  gh auth refresh -h '
        "github.com -s user\n"
    ),
)


def _discover(host: FakeHost, **kwargs: Any) -> Discovery:
    return discover(runner=host.run, which=host.which, environ={}, **kwargs)


# -- discovery ---------------------------------------------------------------


def test_a_public_profile_email_is_suggested_first_and_labelled_public():
    host = FakeHost(user={**OCTOCAT, "email": "mona@example.com"})

    found = _discover(host)

    assert found.gh_status == GH_AUTHENTICATED
    first = found.suggestions[0]
    assert (first.name, first.email) == ("Mona Lisa Octocat", "mona@example.com")
    assert LABEL_PUBLIC in first.label and "@octocat" in first.label
    assert first.source == "gh:octocat"
    # With a public address there is no reason to ask for more than the profile.
    assert not host.ran("user/emails")


def test_a_private_account_whose_token_lacks_the_scope_gets_the_noreply_address():
    host = FakeHost(user=OCTOCAT, emails_failure=SCOPE_REFUSAL)

    found = _discover(host)

    emails = [suggestion.email for suggestion in found.suggestions]
    assert emails == ["583231+octocat@users.noreply.github.com"]
    assert LABEL_NOREPLY in found.suggestions[0].label
    assert "keeps your email private" in found.suggestions[0].label
    # The refusal is skipped silently: no note, and never a scope request.
    assert found.gh_note == ""
    assert not host.ran("auth")


def test_verified_addresses_are_suggested_primary_first_and_unverified_ones_never():
    host = FakeHost(
        user=OCTOCAT,
        emails=[
            {"email": "old@example.com", "primary": False, "verified": True},
            {"email": "unconfirmed@example.com", "primary": False, "verified": False},
            {"email": "mona@work.example", "primary": True, "verified": True},
        ],
    )

    found = _discover(host)

    rows = [(suggestion.email, suggestion.label) for suggestion in found.suggestions]
    assert rows[0] == ("mona@work.example", f"GitHub @octocat: {LABEL_VERIFIED_PRIMARY}")
    assert rows[1] == ("old@example.com", f"GitHub @octocat: {LABEL_VERIFIED}")
    assert rows[2][0] == "583231+octocat@users.noreply.github.com"
    assert "unconfirmed@example.com" not in [email for email, _ in rows]


def test_global_git_config_is_a_secondary_suggestion_labelled_as_such():
    host = FakeHost(
        user={**OCTOCAT, "email": "mona@example.com"},
        git_name="Mona Work",
        git_email="mona@work.example",
    )

    found = _discover(host)

    last = found.suggestions[-1]
    assert (last.name, last.email, last.source) == ("Mona Work", "mona@work.example", "git-config")
    assert last.label == LABEL_GIT_CONFIG
    assert found.suggestions[0].source == "gh:octocat"


def test_a_profile_without_a_name_borrows_git_configs_name_and_then_the_login():
    with_config = build_suggestions(
        GitHubAccount("github.com", "octocat", 583231), git_name="Mona Work"
    )
    without = build_suggestions(GitHubAccount("github.com", "octocat", 583231))

    assert with_config[0].name == "Mona Work"
    assert with_config[0].name_label == NAME_FROM_GIT_CONFIG
    assert without[0].name == "octocat"
    assert without[0].name_label == NAME_FROM_LOGIN


@pytest.mark.parametrize(
    ("host", "expected_status"),
    [
        (FakeHost(gh=False, git_name="Mona", git_email="mona@example.com"), GH_MISSING),
        (
            FakeHost(
                user_failure=CommandOutput(
                    (),
                    returncode=4,
                    stderr="To get started with GitHub CLI, please run:  gh auth login\n",
                ),
                git_name="Mona",
                git_email="mona@example.com",
            ),
            GH_UNAUTHENTICATED,
        ),
        (
            FakeHost(
                user_failure=CommandOutput((), error=f"gh did not finish within {GH_TIMEOUT:g}s"),
                git_name="Mona",
                git_email="mona@example.com",
            ),
            GH_UNREACHABLE,
        ),
    ],
    ids=["missing", "signed-out", "timeout"],
)
def test_a_gh_that_cannot_answer_leaves_only_what_git_config_offered(host, expected_status):
    found = _discover(host)

    assert found.gh_status == expected_status
    assert found.gh_note
    assert [s.source for s in found.suggestions] == [SOURCE_GIT_CONFIG]
    assert not host.ran("user/emails")


def test_every_probe_is_bounded_and_never_prompts():
    host = FakeHost(user=OCTOCAT, emails_failure=SCOPE_REFUSAL, git_name="M", git_email="m@x.io")

    _discover(host)

    for argv, kwargs in host.calls:
        assert kwargs.get("timeout") and kwargs["timeout"] <= GH_TIMEOUT, argv
        if "gh" in argv[0]:
            assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"


def test_nothing_unverifiable_is_ever_suggested():
    """A malformed address is dropped, never repaired into something plausible."""
    suggestions = build_suggestions(
        GitHubAccount("github.com", "octocat", None, "Mona", "not an address"),
        verified=[("also bad", True)],
        git_name="Mona",
        git_email="<mona@example.com>",
    )

    assert suggestions == ()


def test_only_github_com_gets_a_noreply_suggestion():
    enterprise = GitHubAccount("ghe.example.com", "octocat", 583231, "Mona")

    assert noreply_address(enterprise) is None
    assert build_suggestions(enterprise) == ()
    assert noreply_address(GitHubAccount("github.com", "octocat", None)) is None


def test_discover_suggestions_is_the_list_discover_offers():
    host = FakeHost(user={**OCTOCAT, "email": "mona@example.com"})

    assert discover_suggestions(host.run, host.which) == list(_discover(host).suggestions)


def test_an_edited_pair_is_recorded_as_manual():
    suggestions = build_suggestions(GitHubAccount("github.com", "octocat", 1, "Mona"))

    assert source_for("Mona", "1+octocat@users.noreply.github.com", suggestions) == "gh:octocat"
    assert source_for("Mona L", "1+octocat@users.noreply.github.com", suggestions) == "manual"


# -- explicit settings --------------------------------------------------------


def test_an_explicit_setting_parses_to_a_manual_choice():
    choice = parse_setting({"name": " Ada Lovelace ", "email": "ada@example.com"})

    assert choice == Choice("Ada Lovelace", "ada@example.com", SOURCE_MANUAL)
    assert parse_setting(None) is None
    assert parse_setting({}) is None


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ({"name": "Ada", "emial": "ada@example.com"}, "emial"),
        ({"name": "Ada"}, "both name and email"),
        ({"name": "Ada", "email": "not an email"}, "email"),
        ("Ada <ada@example.com>", "mapping"),
    ],
)
def test_an_unusable_setting_is_refused_by_name(value, fragment):
    with pytest.raises(GitIdentitySettingError, match=fragment):
        parse_setting(value)


# -- the interview -------------------------------------------------------------


def _ask(discovery: Discovery, answers: str, **kwargs: Any) -> tuple[Choice | None, str]:
    with CliRunner().isolation(input=answers) as (_stdout, _stderr, mixed):
        choice = interview(discovery, **kwargs)
    return choice, mixed.getvalue().decode()


GH_SUGGESTIONS = Discovery(
    suggestions=(
        Suggestion(
            "Mona Lisa Octocat",
            "mona@example.com",
            f"GitHub @octocat: {LABEL_PUBLIC}",
            "gh:octocat",
        ),
        Suggestion(
            "Mona Lisa Octocat",
            "583231+octocat@users.noreply.github.com",
            f"GitHub @octocat: {LABEL_NOREPLY}",
            "gh:octocat",
        ),
    ),
    gh_status=GH_AUTHENTICATED,
)


def test_enter_confirms_the_first_suggestion_and_records_its_source():
    choice, output = _ask(GH_SUGGESTIONS, "\n")

    assert choice == Choice("Mona Lisa Octocat", "mona@example.com", "gh:octocat")
    assert LABEL_PUBLIC in output and LABEL_NOREPLY in output


def test_a_number_picks_another_suggestion():
    choice, _ = _ask(GH_SUGGESTIONS, "2\n")

    assert choice is not None
    assert choice.email == "583231+octocat@users.noreply.github.com"


def test_editing_records_the_edited_values_and_an_invalid_email_is_asked_again():
    choice, output = _ask(GH_SUGGESTIONS, "e\nAda Lovelace\nnot-an-email\nada@example.com\n")

    assert choice == Choice("Ada Lovelace", "ada@example.com", SOURCE_MANUAL)
    assert "name@domain" in output
    assert output.count("Commit email") == 2


def test_editing_without_changing_anything_keeps_the_suggestions_source():
    choice, _ = _ask(GH_SUGGESTIONS, "e\n\n\n")

    assert choice == Choice("Mona Lisa Octocat", "mona@example.com", "gh:octocat")


def test_s_sets_it_up_later():
    choice, _ = _ask(GH_SUGGESTIONS, "s\n")

    assert choice is None


def test_with_no_suggestion_a_person_types_one_or_skips():
    nothing = Discovery(gh_status=GH_MISSING, gh_note="gh is not installed")

    typed, output = _ask(nothing, "Ada Lovelace\nada@example.com\n")
    skipped, _ = _ask(nothing, "\n")

    assert "gh is not installed" in output
    assert typed == Choice("Ada Lovelace", "ada@example.com", SOURCE_MANUAL)
    assert skipped is None


def test_an_invalid_name_is_asked_again():
    nothing = Discovery(gh_status=GH_MISSING, gh_note="gh is not installed")

    choice, output = _ask(nothing, "Ada <x>\nAda\nada@example.com\n")

    assert choice == Choice("Ada", "ada@example.com", SOURCE_MANUAL)
    assert "'<' or '>'" in output


def test_with_a_current_identity_enter_keeps_it():
    choice, output = _ask(
        GH_SUGGESTIONS,
        "\n",
        current=GitIdentity("Ada", "ada@example.com"),
        current_source="manual",
    )

    assert choice is None
    assert "Current: Ada <ada@example.com> (manual)" in output


# -- config.git-identity -----------------------------------------------------


def _registry(home: Path) -> StepRegistry:
    registry = StepRegistry((host_step(), data_directory_step(path=home)))
    registry.extend(
        (
            config_step(home=home, which=lambda name: None),
            git_identity_step(home=home),
        )
    )
    return registry


def _run(
    home: Path,
    *,
    settings: dict[str, Any] | None = None,
    mode: LifecycleMode = LifecycleMode.INSTALL,
    dry_run: bool = False,
):
    options = InstallOptions(
        installer_version="1.0.0",
        target_version="1.0.0",
        mode=mode,
        interactive=False,
        dry_run=dry_run,
        capabilities=frozenset(),
        approve=frozenset({"*"}),
        settings=dict(settings or {}),
        state_path=home / "install-state.json",
    )
    return InstallEngine(_registry(home), options, support=WSL2).run()


def _stored(home: Path) -> dict[str, Any] | None:
    return (yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}).get(
        "git_identity"
    )


ADA = {"name": "Ada Lovelace", "email": "ada@example.com"}


def test_an_explicit_identity_is_written_with_its_source(tmp_path):
    home = tmp_path / "aq"

    result = _run(home, settings={"git_identity": {**ADA, "source": "gh:ada"}})

    assert result.outcome is InstallOutcome.READY
    row = step(result, STEP_GIT_IDENTITY)
    assert row.state is StepState.SUCCEEDED
    assert row.detail["configured"] is True
    assert _stored(home) == {**ADA, "source": "gh:ada"}


def test_with_nothing_explicit_nothing_is_written_and_the_fallback_is_reported(tmp_path):
    home = tmp_path / "aq"

    result = _run(home)

    assert result.outcome is InstallOutcome.READY
    row = step(result, STEP_GIT_IDENTITY)
    assert row.summary == NOT_CONFIGURED_NOTE
    assert FALLBACK_IDENTITY.formatted() in row.summary
    assert "aq system config git-identity" in row.summary
    assert row.detail["configured"] is False
    assert _stored(home) is None


def test_a_rerun_with_nothing_explicit_keeps_the_configured_identity(tmp_path):
    home = tmp_path / "aq"
    _run(home, settings={"git_identity": ADA})
    before = (home / "config.yaml").read_bytes()

    rerun = _run(home)

    row = step(rerun, STEP_GIT_IDENTITY)
    assert row.detail["configured"] is True
    assert row.detail["revalidated"] is True
    assert row.summary == "AQ commits as Ada Lovelace <ada@example.com> (manual)"
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("mode", [LifecycleMode.REPAIR, LifecycleMode.UPGRADE])
def test_repair_and_upgrade_leave_an_existing_identity_untouched(tmp_path, mode):
    home = tmp_path / "aq"
    _run(home, settings={"git_identity": ADA})
    before = (home / "config.yaml").read_bytes()

    reconciled = _run(home, mode=mode)

    assert step(reconciled, STEP_GIT_IDENTITY).detail["configured"] is True
    assert (home / "config.yaml").read_bytes() == before


def test_an_explicit_identity_on_a_later_run_replaces_the_configured_one(tmp_path):
    home = tmp_path / "aq"
    _run(home, settings={"git_identity": ADA})

    rerun = _run(home, settings={"git_identity": {"name": "Grace", "email": "grace@example.com"}})

    row = step(rerun, STEP_GIT_IDENTITY)
    assert row.detail["previous"] == ADA
    assert _stored(home) == {"name": "Grace", "email": "grace@example.com", "source": "manual"}
    # The backup is of the file as it was before this write.
    backups = sorted(home.glob("config.yaml.bak*"))
    assert backups and "Ada Lovelace" in backups[-1].read_text(encoding="utf-8")


def test_a_dry_run_writes_nothing(tmp_path):
    home = tmp_path / "aq"
    _run(home)
    before = (home / "config.yaml").read_bytes()

    result = _run(home, settings={"git_identity": ADA}, dry_run=True)

    row = step(result, STEP_GIT_IDENTITY)
    assert row.summary == "would commit as Ada Lovelace <ada@example.com>"
    assert (home / "config.yaml").read_bytes() == before


def test_an_unusable_setting_is_reported_but_never_fails_the_install(tmp_path):
    home = tmp_path / "aq"

    result = _run(home, settings={"git_identity": {"name": "Ada", "email": "nope"}})

    row = step(result, STEP_GIT_IDENTITY)
    assert row.state is StepState.NEEDS_USER
    assert result.outcome is InstallOutcome.READY
    assert STEP_GIT_IDENTITY in result.advisory
    assert _stored(home) is None


def test_the_step_is_advisory_and_ordered_before_config_check():
    from src.install import build_registry

    registry = build_registry(WSL2)
    ids = [row.id for row in registry.ordered()]
    spec = registry.get(STEP_GIT_IDENTITY)

    assert spec.advisory and not spec.halts and not spec.mutating
    assert ids.index("config.defaults") < ids.index(STEP_GIT_IDENTITY) < ids.index("config.check")


def test_configured_identity_reads_a_raw_configuration():
    assert configured_identity({"git_identity": ADA}) == GitIdentity(**ADA)
    assert configured_identity({"git_identity": {"name": "Ada"}}) is None
    assert configured_identity({}) is None


# -- the whole installer -------------------------------------------------------


def _machine(tmp_path: Path) -> Machine:
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")
    return host


def test_an_unattended_install_with_an_explicit_identity_records_it_and_runs_no_probe(tmp_path):
    """The scripted machine raises on any unscripted command: the step runs none."""
    host = _machine(tmp_path)

    result = host.install(settings={"git_identity": ADA})

    assert step(result, STEP_GIT_IDENTITY).state is StepState.SUCCEEDED
    assert yaml.safe_load(host.config_path.read_text(encoding="utf-8"))["git_identity"] == {
        **ADA,
        "source": "manual",
    }
    assert not host.ran("gh")
    assert not host.ran("git", "config")


def test_an_unattended_install_with_nothing_explicit_never_guesses(tmp_path):
    host = _machine(tmp_path)

    result = host.install()

    assert step(result, STEP_GIT_IDENTITY).detail["configured"] is False
    assert "git_identity" not in yaml.safe_load(host.config_path.read_text(encoding="utf-8"))
    assert not host.ran("gh")
