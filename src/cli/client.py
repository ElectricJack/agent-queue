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

from .exceptions import CommandError, DaemonNotRunningError, ScopeDeniedError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _resolve_api_url() -> str:
    """Resolve the daemon API base URL.

    Priority:
    1. ``AQ_API_URL`` environment variable (canonical — design §5.1 / §7.1;
       set automatically inside agent sessions by session-runtime)
    2. ``AGENT_QUEUE_API_URL`` environment variable (legacy alias, kept
       indefinitely per design §9.3 — baked into existing shells/docs)
    3. MCP server config from ``~/.agent-queue/config.yaml``
    4. Default ``http://127.0.0.1:8081``
    """
    env_url = os.environ.get("AQ_API_URL") or os.environ.get("AGENT_QUEUE_API_URL")
    if env_url:
        return env_url.rstrip("/")

    config_dir = os.path.expanduser("~/.agent-queue")
    config_file = os.path.join(config_dir, "config.yaml")
    if os.path.exists(config_file):
        try:
            import yaml

            with open(config_file, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            mcp = cfg.get("mcp_server", {})
            host = mcp.get("host", "127.0.0.1")
            port = mcp.get("port", 8081)
            return f"http://{host}:{port}"
        except Exception:
            pass

    return "http://127.0.0.1:8081"


def _relay_error(resp: Any) -> str:
    """Extract a human message from a chat-relay error response."""
    try:
        payload = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}"
    detail = None
    if isinstance(payload, dict):
        # FastAPI raises with `detail`; /api/execute answers with `error`.
        detail = payload.get("detail") or payload.get("error")
    return str(detail or payload or f"HTTP {resp.status_code}")


_DEFAULT_TIMEOUT = 30.0

# Per-command read timeouts (seconds) for commands that run an LLM or other
# long operations server-side. Anything not listed uses _DEFAULT_TIMEOUT.
_COMMAND_TIMEOUTS: dict[str, float] = {
    "compile_playbook": 300.0,
}


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

    def __init__(self, base_url: str | None = None, token: str | None = None):
        self._base_url = base_url or _resolve_api_url()
        # AQ_API_TOKEN: per-session bearer token, injected by session-runtime
        # (design §7.1). Unused server-side until Phase S2 (auth) lands —
        # plumbed through now so callers don't need to change again later.
        self._token = token or os.environ.get("AQ_API_TOKEN")
        self._http: httpx.AsyncClient | None = None
        self._generated_client: Any | None = None

    async def connect(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        self._http = httpx.AsyncClient(
            base_url=self._base_url, timeout=_DEFAULT_TIMEOUT, headers=headers
        )
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

            self._generated_client = Client(base_url=self._base_url, timeout=_DEFAULT_TIMEOUT)
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

    async def execute(self, command: str, args: dict[str, Any] | None = None) -> Any:
        """Execute a CommandHandler command via the REST API.

        Uses the generic ``/api/execute`` endpoint which handles all commands
        including plugin-contributed ones.  The typed endpoint dispatch is
        disabled until the daemon's FastAPI routes are stable — building the
        dispatch map imports ~150 modules (7s) and the routes currently 404.

        Raises ``CommandError`` if the command returns an error.
        Raises ``DaemonNotRunningError`` on connection failure.
        """
        return await self._execute_generic(command, args or {})

    async def _execute_typed(
        self,
        command: str,
        args: dict[str, Any],
        entry: tuple[Any, type],
    ) -> Any:
        """Execute via the generated typed API client.

        Returns the typed response model directly (no dict conversion).
        """
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
            # Typed client couldn't parse the response — fall back to generic
            logger.debug("Typed call for %s returned None, falling back to generic", command)
            return await self._execute_generic(command, args)

        # Check for error response (422 models have an 'error' field)
        if hasattr(result, "error") and result.error is not None:
            raise CommandError(command, result.error)

        return result

    async def _execute_generic(self, command: str, args: dict[str, Any]) -> dict:
        """Execute via the generic /api/execute endpoint."""
        assert self._http is not None, "CLIClient not connected"
        timeout = _COMMAND_TIMEOUTS.get(command, _DEFAULT_TIMEOUT)
        try:
            resp = await self._http.post(
                "/api/execute",
                json={"command": command, "args": args},
                timeout=timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc

        if resp.status_code in (401, 403):
            # aq-surface §4.1 exit code 4: auth/scope denial is not a command
            # error the caller can fix by changing its arguments.
            raise ScopeDeniedError(command, _relay_error(resp))

        data = resp.json()
        if not data.get("ok"):
            raise CommandError(
                command,
                data.get("error", "Unknown error"),
                details=data.get("details"),
            )
        return data.get("result", {})

    # -- Chat relay (supervisor-agent §6.2) ---------------------------------
    # These two hit the explicit ``/api/sessions/{name}/message[s]`` router
    # rather than ``/api/execute``: the session name lives in the path, and
    # ``aq chat`` is the client the endpoints exist for.

    async def send_session_message(
        self,
        name: str,
        body: str,
        *,
        from_id: str = "cli",
        from_kind: str = "user",
        thread_id: str | None = None,
        subject: str | None = None,
        priority: int = 100,
    ) -> dict:
        """POST ``/api/sessions/{name}/message``.  Returns the response dict."""
        assert self._http is not None, "CLIClient not connected"
        payload: dict[str, Any] = {
            "body": body,
            "from": from_id,
            "from_kind": from_kind,
            "priority": priority,
        }
        if thread_id:
            payload["thread_id"] = thread_id
        if subject:
            payload["subject"] = subject
        try:
            resp = await self._http.post(f"/api/sessions/{name}/message", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("message_send", _relay_error(resp))
        return resp.json()

    async def get_session_messages(
        self,
        name: str,
        *,
        thread_id: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> dict:
        """GET ``/api/sessions/{name}/messages``.  Returns the response dict."""
        assert self._http is not None, "CLIClient not connected"
        params: dict[str, Any] = {"limit": limit}
        if thread_id:
            params["thread_id"] = thread_id
        if since is not None:
            params["since"] = since
        try:
            resp = await self._http.get(f"/api/sessions/{name}/messages", params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("message_list", _relay_error(resp))
        return resp.json()

    # -- Streams (console-stream pane view) --------------------------------
    # Bespoke router, not /api/execute — mirrors send_session_message's
    # direct-httpx pattern above.

    async def start_stream(
        self, command: list[str], cwd: str, *,
        title: str | None = None, session_id: str, project_id: str | None = None,
    ) -> dict:
        assert self._http is not None, "CLIClient not connected"
        payload: dict = {"command": command, "cwd": cwd, "session_id": session_id}
        if title:
            payload["title"] = title
        if project_id:
            payload["project_id"] = project_id
        try:
            resp = await self._http.post("/api/streams", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code in (401, 403):
            raise ScopeDeniedError("stream_start", _relay_error(resp))
        if resp.status_code >= 400:
            raise CommandError("stream_start", _relay_error(resp))
        return resp.json()

    async def get_stream(self, stream_id: str) -> dict:
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.get(f"/api/streams/{stream_id}")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("stream_metadata", _relay_error(resp))
        return resp.json()

    async def tail_stream(self, stream_id: str, *, after_seq: int = -1) -> dict:
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.get(
                f"/api/streams/{stream_id}/tail", params={"after_seq": after_seq}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("stream_tail", _relay_error(resp))
        return resp.json()

    async def kill_stream(self, stream_id: str) -> dict:
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.post(f"/api/streams/{stream_id}/kill")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("stream_kill", _relay_error(resp))
        return resp.json()

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
        self._db_url = db_path or _resolve_db_url()
        self._db = None

    async def connect(self) -> None:
        if self._db_url.startswith(("postgresql://", "postgres://")):
            from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

            self._db = PostgreSQLDatabaseAdapter(self._db_url, pool_min=1, pool_max=2)
        else:
            from src.database import Database

            if not os.path.exists(self._db_url):
                raise FileNotFoundError(
                    f"Database not found at {self._db_url}. "
                    "Is Agent Q running? Set AGENT_QUEUE_DB to override."
                )
            self._db = Database(self._db_url)
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


def _resolve_db_config() -> dict | None:
    """Read the database section from config.yaml, if present."""
    config_dir = os.path.expanduser("~/.agent-queue")
    config_file = os.path.join(config_dir, "config.yaml")
    if os.path.exists(config_file):
        try:
            import yaml

            with open(config_file, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            db_section = cfg.get("database")
            if isinstance(db_section, dict) and db_section.get("url"):
                return db_section
        except Exception:
            pass
    return None


def _resolve_db_url() -> str:
    """Resolve the database URL (DSN or file path) for CLI operations."""
    env_url = os.environ.get("AGENT_QUEUE_DB")
    if env_url:
        return env_url

    db_config = _resolve_db_config()
    if db_config and db_config.get("url"):
        url = db_config["url"]
        if url.startswith(("postgresql://", "postgres://")):
            return url
        return os.path.expanduser(url)

    config_dir = os.path.expanduser("~/.agent-queue")
    config_file = os.path.join(config_dir, "config.yaml")
    if os.path.exists(config_file):
        try:
            import yaml

            with open(config_file, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            db_path = cfg.get("database_path")
            if db_path:
                return os.path.expanduser(db_path)
        except Exception:
            pass

    return os.path.join(config_dir, "agent-queue.db")
