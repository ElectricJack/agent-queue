"""The Windows bootstrap is intentionally a thin, inspectable transport.

PowerShell is not available in the Linux test environment, so these checks
protect its public contract: Windows owns WSL enablement; the WSL script owns
the Linux-side setup and delegates AQ policy to ``aq install``.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WINDOWS_BOOTSTRAP = ROOT / "scripts" / "install-windows.ps1"
WSL_BOOTSTRAP = ROOT / "scripts" / "install-wsl.sh"


def test_windows_entrypoint_handles_install_conversion_and_resume() -> None:
    text = WINDOWS_BOOTSTRAP.read_text()

    assert "wsl.exe --list --verbose" in text
    assert "wsl.exe --install -d $Distro" in text
    assert "wsl.exe --set-version $Distro 2" in text
    assert "Test-Administrator" in text
    assert "Restart Windows" in text
    assert "rerun this command" in text


def test_windows_entrypoint_starts_wsl_in_linux_home_and_delegates() -> None:
    text = WINDOWS_BOOTSTRAP.read_text()

    assert "--distribution $Distro --cd ~ -- bash -lc" in text
    assert "install-wsl.sh" in text
    assert "Start-Process <url>" in text
    assert "localhost" in text


def test_wsl_bootstrap_stays_in_linux_home_and_calls_common_installer() -> None:
    text = WSL_BOOTSTRAP.read_text()

    assert 'checkout_dir="${AQ_CHECKOUT_DIR:-$HOME/.local/share/agent-queue}"' in text
    assert '[[ "$checkout_dir" == /mnt/* ]]' in text
    assert "WSL1 is not supported" in text
    assert '"$aq_command" install --interactive' in text
    assert 'export PATH="$HOME/.local/bin:$PATH"' in text
    assert "ip route show default" in text
