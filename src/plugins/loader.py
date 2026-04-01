"""Plugin loading utilities: git clone, requirements install, module import.

This module handles the mechanical aspects of getting plugin code onto disk
and into the Python runtime. The PluginRegistry uses these functions during
install, update, and reload operations.

Supports two plugin formats:

- **pyproject.toml** (preferred): Plugin is a proper Python package with
  ``[project.entry-points."aq.plugins"]`` declaring the Plugin subclass.
  Installed via ``pip install -e .``.  Metadata read from
  ``importlib.metadata``.

- **plugin.yaml** (legacy): Custom manifest with metadata and a ``plugin.py``
  entry point.  Dependencies from ``requirements.txt``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

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
# Package-based installation (pyproject.toml)
# ---------------------------------------------------------------------------


def has_pyproject(install_path: str | Path) -> bool:
    """Check if a plugin uses pyproject.toml packaging.

    Args:
        install_path: Plugin instance directory (containing src/).

    Returns:
        True if src/pyproject.toml exists and declares an aq.plugins entry point.
    """
    pyproject_path = Path(install_path) / "src" / "pyproject.toml"
    if not pyproject_path.exists():
        return False
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        eps = data.get("project", {}).get("entry-points", {})
        return "aq.plugins" in eps
    except Exception:
        return False


def install_plugin_package(install_path: str | Path) -> bool:
    """Install a plugin as an editable Python package via ``pip install -e``.

    Falls back to :func:`install_requirements` if no ``pyproject.toml`` is
    found.

    Args:
        install_path: Plugin instance directory (containing src/).

    Returns:
        True on success, False on failure.
    """
    src_dir = Path(install_path) / "src"
    pyproject = src_dir / "pyproject.toml"

    if not pyproject.exists():
        return install_requirements(install_path)

    logger.info("Installing plugin package from %s", src_dir)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(src_dir), "-q"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            logger.error(
                "pip install -e failed for %s: %s",
                install_path, proc.stderr.strip(),
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("pip install -e timed out for %s", install_path)
        return False
    except Exception as e:
        logger.error("pip install -e error for %s: %s", install_path, e)
        return False


def load_plugin_via_entry_point(plugin_name: str) -> type[Plugin] | None:
    """Load a Plugin subclass via the ``aq.plugins`` entry point group.

    This is the preferred loading path for pyproject.toml-based plugins.
    After ``pip install -e .``, the entry point is available via
    ``importlib.metadata``.

    Args:
        plugin_name: The entry point name (matches plugin name).

    Returns:
        The Plugin subclass, or None if no matching entry point is found.
    """
    try:
        eps = importlib.metadata.entry_points(group="aq.plugins")
    except Exception:
        return None

    for ep in eps:
        if ep.name == plugin_name:
            try:
                cls = ep.load()
                if isinstance(cls, type) and issubclass(cls, Plugin) and cls is not Plugin:
                    return cls
                logger.warning(
                    "Entry point '%s' for plugin '%s' is not a Plugin subclass",
                    ep.value, plugin_name,
                )
            except Exception as e:
                logger.error(
                    "Failed to load entry point for plugin '%s': %s",
                    plugin_name, e, exc_info=True,
                )
            return None
    return None


def parse_pyproject_metadata(install_path: str | Path) -> dict:
    """Read basic metadata from a plugin's ``pyproject.toml``.

    This reads the TOML file directly (without requiring the package to be
    installed) for use during discovery.

    Args:
        install_path: Plugin instance directory (containing src/).

    Returns:
        Dict with ``name``, ``version``, ``description``, ``author`` keys.

    Raises:
        FileNotFoundError: If pyproject.toml doesn't exist.
        ValueError: If required fields are missing.
    """
    pyproject_path = Path(install_path) / "src" / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"No pyproject.toml found in {pyproject_path.parent}")

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    name = project.get("name")
    if not name:
        raise ValueError(f"pyproject.toml missing 'project.name' in {pyproject_path}")

    authors = project.get("authors", [])
    author = authors[0].get("name", "") if authors else ""

    return {
        "name": name,
        "version": project.get("version", "0.0.0"),
        "description": project.get("summary", project.get("description", "")),
        "author": author,
    }


def parse_plugin_metadata(
    install_path: str | Path,
    plugin_class: type[Plugin],
) -> PluginInfo:
    """Build PluginInfo from installed package metadata and class attributes.

    Reads name/version/description/author from ``importlib.metadata`` (the
    installed package).  Reads permissions, config_schema, and default_config
    from the Plugin subclass attributes.

    Falls back to reading ``pyproject.toml`` directly if the package metadata
    isn't available (e.g. not yet installed).

    Args:
        install_path: Plugin instance directory.
        plugin_class: The loaded Plugin subclass.

    Returns:
        Populated PluginInfo.
    """
    # Try to get the distribution name from the entry point or pyproject
    meta_dict: dict[str, str] = {}

    # First try: read from installed package metadata
    try:
        pyproject_meta = parse_pyproject_metadata(install_path)
        dist_name = pyproject_meta["name"]
        try:
            dist = importlib.metadata.metadata(dist_name)
            meta_dict = {
                "name": dist.get("Name", dist_name),
                "version": dist.get("Version", "0.0.0"),
                "description": dist.get("Summary", ""),
                "author": dist.get("Author", ""),
            }
        except importlib.metadata.PackageNotFoundError:
            # Package not installed yet, use pyproject data directly
            meta_dict = pyproject_meta
    except (FileNotFoundError, ValueError):
        meta_dict = {"name": Path(install_path).name, "version": "0.0.0"}

    return PluginInfo(
        name=meta_dict.get("name", Path(install_path).name),
        version=meta_dict.get("version", "0.0.0"),
        description=meta_dict.get("description", ""),
        author=meta_dict.get("author", ""),
        permissions=list(plugin_class.plugin_permissions),
        config_schema=dict(plugin_class.config_schema),
        default_config=dict(plugin_class.default_config),
    )


# ---------------------------------------------------------------------------
# Plugin manifest parsing (legacy — plugin.yaml)
# ---------------------------------------------------------------------------


def parse_plugin_yaml(install_path: str | Path) -> PluginInfo:
    """Parse the plugin.yaml manifest from a plugin's source directory.

    .. deprecated::
        Use ``pyproject.toml`` with ``[project.entry-points."aq.plugins"]``
        instead.  This function remains for backward compatibility with
        legacy plugins.

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
