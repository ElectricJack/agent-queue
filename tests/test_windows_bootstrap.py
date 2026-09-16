"""The bootstraps are intentionally thin, inspectable transports.

PowerShell is not available in the Linux test environment, and the shell
bootstrap mutates the machine it runs on, so these checks protect the public
contract rather than executing it: Windows owns WSL enablement, the shared
shell script owns host setup on both macOS and WSL2, and every AQ policy
decision is delegated to ``aq install``.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WINDOWS_BOOTSTRAP = ROOT / "scripts" / "install-windows.ps1"
SHELL_BOOTSTRAP = ROOT / "scripts" / "install.sh"
WSL_SHIM = ROOT / "scripts" / "install-wsl.sh"


def test_windows_entrypoint_handles_install_conversion_and_resume() -> None:
    text = WINDOWS_BOOTSTRAP.read_text()

    assert "wsl.exe --list --verbose" in text
    assert "wsl.exe --install -d $Distro" in text
    assert "wsl.exe --set-version $resolvedDistro 2" in text
    assert "Test-Administrator" in text
    assert "Restart Windows" in text
    assert "rerun this command" in text


def test_windows_entrypoint_reuses_the_actual_ubuntu_2404_alias_safely() -> None:
    text = WINDOWS_BOOTSTRAP.read_text()

    assert "Resolve-SupportedWslDistro" in text
    assert '"Ubuntu-24.04"' in text
    assert '"Ubuntu"' in text
    assert "cat /etc/os-release" in text
    assert "Test-SupportedUbuntuRelease" in text
    assert "[switch]$CheckOnly" in text
    assert '$line = $line -replace [char]0, ""' in text
    assert '$repoArgument = "\'$Repository\'"' in text


def test_windows_entrypoint_starts_wsl_in_linux_home_and_delegates() -> None:
    text = WINDOWS_BOOTSTRAP.read_text()

    assert "--distribution $resolvedDistro --cd ~ -- bash -lc" in text
    assert "scripts/install.sh" in text
    assert "Start-Process <url>" in text
    assert "localhost" in text


def test_shell_bootstrap_serves_both_supported_hosts() -> None:
    text = SHELL_BOOTSTRAP.read_text()

    # One script, two branches -- and nothing else installable.
    assert "Darwin) host=\"macos\" ;;" in text
    assert 'host="wsl"' in text
    assert "Unsupported operating system" in text
    assert "is not a WSL2 distribution" in text


def test_shell_bootstrap_stays_in_linux_home_and_calls_common_installer() -> None:
    text = SHELL_BOOTSTRAP.read_text()

    assert 'checkout_dir="${AQ_CHECKOUT_DIR:-$HOME/.local/share/agent-queue}"' in text
    assert '[[ "$checkout_dir" == /mnt/* ]]' in text
    assert "WSL1 is not supported" in text
    assert '"$aq_command" install --interactive' in text
    assert "ip route show default" in text


def test_shell_bootstrap_refuses_macos_below_the_supported_release() -> None:
    text = SHELL_BOOTSTRAP.read_text()

    assert "sw_vers -productVersion" in text
    assert "(( macos_major < 14 ))" in text


def test_shell_bootstrap_never_types_a_password_for_the_user() -> None:
    """Homebrew and the Command Line Tools are needs_user checkpoints.

    ``aq install``'s macOS steps refuse to enter an administrator password, and
    a bootstrap that did so behind their back would defeat that contract.
    """
    text = SHELL_BOOTSTRAP.read_text()

    assert "xcode-select --install" in text
    assert "Homebrew/install/HEAD/install.sh" in text
    assert "which AQ never types" in text

    # The official Homebrew command is *quoted for the user to run*, never
    # executed, and the only privilege escalation is the WSL distribution's
    # own apt.  Check the commands the script runs, not the prose around them.
    commands = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    escalations = [line for line in commands if line.startswith("sudo ")]
    assert escalations == ["sudo apt-get update", "sudo apt-get install -y git python3-venv"]

    # Homebrew's installer appears escaped inside a next_action message -- it is
    # printed for the user and the script exits 10, rather than being run.
    brew_line = next(line for line in commands if "Homebrew/install/HEAD" in line)
    assert brew_line.startswith('/bin/bash -c \\"\\$(curl')
    assert brew_line.endswith('\\""')
    assert "exit 10" in text[text.index("Homebrew/install/HEAD") :]


def test_shell_bootstrap_links_aq_before_putting_local_bin_on_path() -> None:
    """A PATH entry with nothing linked into it left no ``aq`` in a new shell."""
    text = SHELL_BOOTSTRAP.read_text()

    link = text.index('ln -sf "$checkout_dir/.venv/bin/$binname"')
    path_export = text.index("path_line=")
    assert link < path_export
    assert 'zsh) profile_file="$HOME/.zprofile" ;;' in text


def test_shell_bootstrap_installs_from_inside_the_checkout() -> None:
    """The `cli` extra names the API client by a *relative* path.

    ``pyproject.toml`` declares ``agent-queue-api-client @ file:packages/aq-client``
    and pip resolves that against the current working directory, not the
    project being installed.  A bootstrap runs from wherever the user happens
    to be, so installing without changing directory first looked for
    ``packages/aq-client`` under *that* directory and died with
    ``OSError: [Errno 2] No such file or directory``.
    """
    text = SHELL_BOOTSTRAP.read_text()

    # `aq_command=` also appears in the reuse branch above, so anchor the end
    # of the slice to the assignment that follows the venv build.
    start = text.index('"$python_bin" -m venv')
    install = text[start : text.index("aq_command=", start)]
    assert 'cd "$checkout_dir"' in install
    assert install.index('cd "$checkout_dir"') < install.index("pip install -e")
    # Every pip install runs relative to the checkout, never an absolute
    # project path that would re-open the same cwd-resolution hole.
    assert '-e "./packages/aq-client"' in install
    assert '-e ".[cli]"' in install
    assert '[cli]"' in install and '"$checkout_dir[cli]"' not in install


def test_the_relative_client_path_the_bootstrap_compensates_for_still_exists() -> None:
    """If the dependency stops being relative, the cd above can be revisited."""
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert "agent-queue-api-client @ file:packages/aq-client" in pyproject


def test_shell_bootstrap_reports_a_needs_user_stop_instead_of_aborting() -> None:
    text = SHELL_BOOTSTRAP.read_text()

    handoff = text[text.index("set +e\nif [[ -t 0 ]]") :]
    assert handoff.index("fi\nstatus=$?\nset -e") > 0
    assert 'exit "$status"' in text


def test_shell_bootstrap_hands_the_installer_a_terminal_not_the_script() -> None:
    """Under ``curl ... | bash`` the script itself is stdin.

    Letting the wizard inherit that made it read the remaining script text as
    answers -- one "Error: invalid input" per leftover line, then Abort at end
    of file -- and `--interactive` overrode the installer's own isatty()
    detection, which would otherwise have avoided prompting at all.
    """
    text = SHELL_BOOTSTRAP.read_text()

    handoff = text[text.index("Running the common AQ installer") :]

    # A real terminal: prompt on it directly.
    assert '"$aq_command" install --interactive </dev/tty' in handoff
    # `[[ -r /dev/tty ]]` reports readable where there is no controlling
    # terminal to open, which silently skipped the installer; probe by opening.
    assert "(exec </dev/tty) 2>/dev/null" in handoff
    code = [line for line in handoff.splitlines() if not line.strip().startswith("#")]
    assert not [line for line in code if "-r /dev/tty" in line]
    # No terminal: let the installer detect that itself, and never inherit the
    # pipe, so the rest of this script cannot be consumed.
    assert '"$aq_command" install </dev/null' in handoff


def test_shell_bootstrap_forces_interactive_only_with_a_terminal() -> None:
    text = SHELL_BOOTSTRAP.read_text()
    handoff = text[text.index("Running the common AQ installer") :]

    interactive_calls = [
        line.strip() for line in handoff.splitlines() if "install --interactive" in line
    ]
    assert len(interactive_calls) == 2
    # Both are inside a branch that established a terminal first.
    assert handoff.index("[[ -t 0 ]]") < handoff.index(interactive_calls[0])
    assert all("</dev/null" not in call for call in interactive_calls)


def test_the_old_wsl_url_still_reaches_the_shared_bootstrap() -> None:
    text = WSL_SHIM.read_text()

    assert "scripts/install.sh" in text
    assert 'bash -s -- "$@"' in text
