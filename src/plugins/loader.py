"""Plugin loading utilities: git clone, requirements install, module import.

This module handles the mechanical aspects of getting plugin code onto disk
and into the Python runtime. The PluginRegistry uses these functions during
install, update, and reload operations.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from src.plugins.base import Plugin, PluginInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


async def clone_plugin_repo(
    url: str,
    target_dir: str | Path,
    *,
    branch: str | None = None,
    rev: str | None = None,
) -> str:
    """Clone a plugin repository into target_dir/src/.

    Args:
        url: Git repository URL.
        target_dir: Plugin instance directory (e.g. ~/.agent-queue/plugins/my-plugin/).
        branch: Optional branch to clone.
        rev: Optional specific revision to checkout after cloning.

    Returns:
        The resolved HEAD commit SHA after cloning.

    Raises:
        RuntimeError: If the git clone fails.
    """
    target = Path(target_dir)
    src_dir = target / "src"

    # Clean existing source if present
    if src_dir.exists():
        shutil.rmtree(src_dir)

    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([url, str(src_dir)])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Git clone failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    # Checkout specific revision if requested
    if rev:
        proc = subprocess.run(
            ["git", "checkout", rev],
            cwd=str(src_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Git checkout {rev} failed: {proc.stderr.strip()}"
            )

    # Get the resolved HEAD SHA
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(src_dir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip()


async def pull_plugin_repo(
    install_path: str | Path,
    *,
    rev: str | None = None,
) -> str:
    """Pull latest changes for an installed plugin.

    Args:
        install_path: Plugin instance directory.
        rev: Optional specific revision to checkout after pulling.

    Returns:
        The resolved HEAD commit SHA after pulling.

    Raises:
        RuntimeError: If the git pull fails.
    """
    src_dir = Path(install_path) / "src"
    if not src_dir.exists():
        raise RuntimeError(f"Plugin source directory not found: {src_dir}")

    proc = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=str(src_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Git pull failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    if rev:
        proc = subprocess.run(
            ["git", "checkout", rev],
            cwd=str(src_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Git checkout {rev} failed: {proc.stderr.strip()}"
            )

    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(src_dir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip()


def get_current_rev(install_path: str | Path) -> str:
    """Get the current HEAD SHA for a plugin's source directory.

    Args:
        install_path: Plugin instance directory.

    Returns:
        The HEAD commit SHA, or empty string on failure.
    """
    src_dir = Path(install_path) / "src"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(src_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Requirements installation
# ---------------------------------------------------------------------------


def install_requirements(install_path: str | Path) -> bool:
    """Install a plugin's Python requirements if requirements.txt exists.

    Args:
        install_path: Plugin instance directory.

    Returns:
        True if requirements were installed (or none needed), False on failure.
    """
    src_dir = Path(install_path) / "src"
    req_file = src_dir / "requirements.txt"

    if not req_file.exists():
        return True

    logger.info("Installing requirements for plugin at %s", install_path)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            logger.error(
                "pip install failed for %s: %s",
                install_path, proc.stderr.strip(),
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("pip install timed out for %s", install_path)
        return False
    except Exception as e:
        logger.error("pip install error for %s: %s", install_path, e)
        return False


# ---------------------------------------------------------------------------
# Plugin manifest parsing
# ---------------------------------------------------------------------------


def parse_plugin_yaml(install_path: str | Path) -> PluginInfo:
    """Parse the plugin.yaml manifest from a plugin's source directory.

    Args:
        install_path: Plugin instance directory (containing src/plugin.yaml).

    Returns:
        Parsed PluginInfo.

    Raises:
        FileNotFoundError: If plugin.yaml doesn't exist.
        ValueError: If plugin.yaml is invalid.
    """
    src_dir = Path(install_path) / "src"
    manifest_path = src_dir / "plugin.yaml"

    if not manifest_path.exists():
        # Also try plugin.yml
        manifest_path = src_dir / "plugin.yml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No plugin.yaml found in {src_dir}"
        )

    with open(manifest_path) as f:
        data = yaml.safe_load(f)

    if not data or not isinstance(data, dict):
        raise ValueError(f"Invalid plugin.yaml in {src_dir}")

    if "name" not in data:
        raise ValueError(f"plugin.yaml missing required 'name' field in {src_dir}")

    return PluginInfo.from_dict(data)


# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------


def import_plugin_module(install_path: str | Path) -> type[Plugin]:
    """Import the plugin module and find the Plugin subclass.

    Loads ``plugin.py`` from the plugin's source directory and returns
    the first class that subclasses Plugin.

    Args:
        install_path: Plugin instance directory (containing src/plugin.py).

    Returns:
        The Plugin subclass (not instantiated).

    Raises:
        FileNotFoundError: If plugin.py doesn't exist.
        ImportError: If the module fails to import.
        ValueError: If no Plugin subclass is found.
    """
    src_dir = Path(install_path) / "src"
    plugin_file = src_dir / "plugin.py"

    if not plugin_file.exists():
        raise FileNotFoundError(f"No plugin.py found in {src_dir}")

    # Create a unique module name to avoid conflicts
    plugin_name = Path(install_path).name
    module_name = f"aq_plugin_{plugin_name}"

    # Remove old module if reloading
    if module_name in sys.modules:
        del sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, str(plugin_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {plugin_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    # Add the src dir to the module's search path so relative imports work
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        # Clean up on failure
        sys.modules.pop(module_name, None)
        raise ImportError(f"Failed to import plugin module {plugin_file}: {e}") from e

    # Find the Plugin subclass
    plugin_class = None
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Plugin)
            and attr is not Plugin
        ):
            plugin_class = attr
            break

    if plugin_class is None:
        raise ValueError(
            f"No Plugin subclass found in {plugin_file}. "
            f"The plugin module must define a class that extends Plugin."
        )

    return plugin_class


# ---------------------------------------------------------------------------
# Prompt setup
# ---------------------------------------------------------------------------


def setup_prompts(install_path: str | Path) -> None:
    """Copy default prompts from plugin source to instance directory.

    Only copies prompts that don't already exist in the instance directory,
    preserving user customizations.

    Args:
        install_path: Plugin instance directory.
    """
    src_prompts = Path(install_path) / "src" / "prompts"
    inst_prompts = Path(install_path) / "prompts"

    if not src_prompts.exists():
        return

    inst_prompts.mkdir(parents=True, exist_ok=True)

    for src_file in src_prompts.iterdir():
        if src_file.is_file():
            dest_file = inst_prompts / src_file.name
            if not dest_file.exists():
                shutil.copy2(src_file, dest_file)
                logger.debug("Copied default prompt: %s", src_file.name)


def reset_prompts(install_path: str | Path) -> int:
    """Re-copy all default prompts, overwriting instance copies.

    Args:
        install_path: Plugin instance directory.

    Returns:
        Number of prompts reset.
    """
    src_prompts = Path(install_path) / "src" / "prompts"
    inst_prompts = Path(install_path) / "prompts"

    if not src_prompts.exists():
        return 0

    inst_prompts.mkdir(parents=True, exist_ok=True)
    count = 0

    for src_file in src_prompts.iterdir():
        if src_file.is_file():
            dest_file = inst_prompts / src_file.name
            shutil.copy2(src_file, dest_file)
            count += 1

    return count
