"""REST client for CLI operations.

Delegates commands to the daemon's typed API endpoints (``/api/{category}/{command}``)
via the generated ``agent_queue_api_client`` package.  Falls back to the generic
``/api/execute`` endpoint for commands not covered by the generated client.

Plugin operations still need direct database access (filesystem ops that
don't belong in CommandHandler), so ``PluginClient`` is provided as a
separate class for that purpose.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
from typing import Any

import httpx

from .exceptions import CommandError, DaemonNotRunningError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _resolve_api_url() -> str:
    """Resolve the daemon API base URL.

    Priority:
    1. ``AGENT_QUEUE_API_URL`` environment variable
    2. MCP server config from ``~/.agent-queue/config.yaml``
    3. Default ``http://127.0.0.1:8081``
    """
    env_url = os.environ.get("AGENT_QUEUE_API_URL")
    if env_url:
        return env_url.rstrip("/")

    config_dir = os.path.expanduser("~/.agent-queue")
    config_file = os.path.join(config_dir, "config.yaml")
    if os.path.exists(config_file):
        try:
            import yaml

            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            mcp = cfg.get("mcp_server", {})
            host = mcp.get("host", "127.0.0.1")
            port = mcp.get("port", 8081)
            return f"http://{host}:{port}"
        except Exception:
            pass

    return "http://127.0.0.1:8081"


# ---------------------------------------------------------------------------
# Typed endpoint dispatch
# ---------------------------------------------------------------------------

# Lazily built map: command_name → (api_module, request_model_class)
_TYPED_DISPATCH: dict[str, tuple[Any, type]] | None = None


def _build_typed_dispatch() -> dict[str, tuple[Any, type]]:
    """Discover all generated API functions and their request models.

    Returns a dict mapping command names to (module, RequestModelClass) tuples.
    The module has an ``asyncio()`` function that accepts ``client`` and ``body``.
    """
    dispatch: dict[str, tuple[Any, type]] = {}
    try:
        import agent_queue_api_client.api as api_pkg

        for _, cat_name, ispkg in pkgutil.iter_modules(api_pkg.__path__):
            if not ispkg:
                continue
            cat_mod = importlib.import_module(f"agent_queue_api_client.api.{cat_name}")
            for _, func_name, _ in pkgutil.iter_modules(cat_mod.__path__):
                try:
                    mod = importlib.import_module(
                        f"agent_queue_api_client.api.{cat_name}.{func_name}"
                    )
                    if not hasattr(mod, "asyncio"):
                        continue
                    # Find the request model: look for the _get_kwargs body param type
                    # Convention: {FuncName}Request in the module's imports
                    req_model = None
                    for attr_name in dir(mod):
                        obj = getattr(mod, attr_name)
                        if (
                            isinstance(obj, type)
                            and attr_name.endswith("Request")
                            and hasattr(obj, "to_dict")
                        ):
                            req_model = obj
                            break
                    if req_model is not None:
                        dispatch[func_name] = (mod, req_model)
                except Exception:
                    pass
    except ImportError:
        logger.debug("agent_queue_api_client not installed, typed dispatch unavailable")
    return dispatch


def _get_typed_dispatch() -> dict[str, tuple[Any, type]]:
    """Get or build the typed dispatch map (cached)."""
    global _TYPED_DISPATCH
    if _TYPED_DISPATCH is None:
        _TYPED_DISPATCH = _build_typed_dispatch()
    return _TYPED_DISPATCH


# ---------------------------------------------------------------------------
# REST CLI client
# ---------------------------------------------------------------------------


class CLIClient:
    """Async HTTP client that delegates commands to the daemon.

    Routes commands through the generated typed API client when possible,
    falling back to ``/api/execute`` for unrecognized commands.

    Usage::

        async with CLIClient() as client:
            result = await client.execute("list_tasks", {"project_id": "myproj"})
    """

    def __init__(self, base_url: str | None = None):
        self._base_url = base_url or _resolve_api_url()
        self._http: httpx.AsyncClient | None = None
        self._generated_client: Any | None = None

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        try:
            resp = await self._http.get("/api/health")
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            await self._http.aclose()
            self._http = None
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc

        # Set up the generated client, sharing the same httpx.AsyncClient
        try:
            from agent_queue_api_client.client import Client

            self._generated_client = Client(base_url=self._base_url, timeout=30.0)
            self._generated_client.set_async_httpx_client(self._http)
        except ImportError:
            pass

    async def close(self) -> None:
        # Don't close the httpx client via generated client — we own it
        self._generated_client = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> CLIClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def execute(self, command: str, args: dict[str, Any] | None = None) -> dict:
        """Execute a CommandHandler command via the REST API.

        Routes through the typed endpoint when available, falls back to /api/execute.

        Returns the result dict on success.
        Raises ``CommandError`` if the command returns an error.
        Raises ``DaemonNotRunningError`` on connection failure.
        """
        # Try typed endpoint first
        if self._generated_client is not None:
            dispatch = _get_typed_dispatch()
            entry = dispatch.get(command)
            if entry is not None:
                return await self._execute_typed(command, args or {}, entry)

        # Fallback to generic /api/execute
        return await self._execute_generic(command, args or {})

    async def _execute_typed(
        self,
        command: str,
        args: dict[str, Any],
        entry: tuple[Any, type],
    ) -> dict:
        """Execute via the generated typed API client."""
        mod, req_model = entry
        try:
            # Build the request model from args
            body = req_model(**args)
            result = await mod.asyncio(client=self._generated_client, body=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        except TypeError as exc:
            # Request model construction failed — fall back to generic
            logger.debug("Typed call for %s failed (%s), falling back", command, exc)
            return await self._execute_generic(command, args)

        if result is None:
            raise CommandError(command, "No response from server")

        # Check for error response (422 models have an 'error' field)
        if hasattr(result, "error") and result.error is not None:
            raise CommandError(command, result.error)

        # Convert to dict for formatter compatibility
        if hasattr(result, "to_dict"):
            return result.to_dict()
        return result if isinstance(result, dict) else {}

    async def _execute_generic(self, command: str, args: dict[str, Any]) -> dict:
        """Execute via the generic /api/execute endpoint."""
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.post(
                "/api/execute",
                json={"command": command, "args": args},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc

        data = resp.json()
        if not data.get("ok"):
            raise CommandError(command, data.get("error", "Unknown error"))
        return data.get("result", {})

    async def list_tool_definitions(self) -> list[dict]:
        """Fetch tool definitions from the daemon for CLI auto-generation."""
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.get("/api/tools")
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc


# ---------------------------------------------------------------------------
# Plugin client — direct DB access for plugin management operations
# ---------------------------------------------------------------------------


class PluginClient:
    """Direct database client for plugin management operations.

    Plugin commands involve filesystem operations (git clone, pip install)
    that don't belong in CommandHandler.  This client provides the DB
    access those operations need.
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or _default_plugin_db_path()
        self._db = None

    async def connect(self) -> None:
        from src.database import Database

        if not os.path.exists(self._db_path):
            raise FileNotFoundError(
                f"Database not found at {self._db_path}. "
                "Is AgentQueue running? Set AGENT_QUEUE_DB to override."
            )
        self._db = Database(self._db_path)
        await self._db.initialize()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> PluginClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self):
        assert self._db is not None, "PluginClient not connected"
        return self._db

    async def list_plugins(self, status: str | None = None) -> list[dict]:
        return await self.db.list_plugins(status=status)

    async def get_plugin(self, plugin_id: str) -> dict | None:
        return await self.db.get_plugin(plugin_id)

    async def create_plugin(self, **kwargs) -> None:
        await self.db.create_plugin(**kwargs)

    async def update_plugin(self, plugin_id: str, **kwargs) -> None:
        await self.db.update_plugin(plugin_id, **kwargs)

    async def delete_plugin(self, plugin_id: str) -> None:
        await self.db.delete_plugin(plugin_id)

    async def delete_plugin_data_all(self, plugin_id: str) -> None:
        await self.db.delete_plugin_data_all(plugin_id)

    async def list_hooks(self, **kwargs):
        return await self.db.list_hooks(**kwargs)

    async def list_hook_runs(self, hook_id: str, limit: int = 20):
        return await self.db.list_hook_runs(hook_id, limit=limit)


def _default_plugin_db_path() -> str:
    """Resolve the database path for plugin operations."""
    env_path = os.environ.get("AGENT_QUEUE_DB")
    if env_path:
        return env_path

    config_dir = os.path.expanduser("~/.agent-queue")
    config_file = os.path.join(config_dir, "config.yaml")
    if os.path.exists(config_file):
        try:
            import yaml

            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            db_path = cfg.get("database", {}).get("path")
            if db_path:
                return os.path.expanduser(db_path)
        except Exception:
            pass

    return os.path.join(config_dir, "agent-queue.db")
