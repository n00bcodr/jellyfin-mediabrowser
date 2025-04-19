# custom_components/mediabrowser/hub.py
# --- CORRECTED VERSION ---
"""Hub for the Media Browser (Emby/Jellyfin) integration."""

from copy import deepcopy
import urllib.parse
import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Awaitable
import uuid # Ensure imported

import aiohttp
import async_timeout
# Import necessary exceptions and constants
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.const import CONF_URL, CONF_NAME
from homeassistant.util import uuid as uuid_util # Alias if needed, or use uuid directly

# Ensure all helpers needed are imported
from .helpers import (
    get_library_changed_event_data,
    get_user_data_changed_event_data,
    get_session_event_data,
    snake_case,
    size_of, # Import size_of if used elsewhere or remove
)

from .const import (
    APP_PLAYERS,
    CONF_API_KEY,
    CONF_CACHE_SERVER_ID,
    CONF_CACHE_SERVER_NAME,
    CONF_CACHE_SERVER_PING,
    CONF_CACHE_SERVER_USER_ID,
    CONF_CACHE_SERVER_VERSION,
    CONF_CLIENT_NAME,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_VERSION,
    CONF_EVENTS_ACTIVITY_LOG,
    CONF_EVENTS_OTHER,
    CONF_EVENTS_SESSIONS,
    CONF_EVENTS_TASKS,
    CONF_IGNORE_APP_PLAYERS,
    CONF_IGNORE_DLNA_PLAYERS,
    CONF_IGNORE_MOBILE_PLAYERS, # Ensure this exists in const.py
    CONF_IGNORE_WEB_PLAYERS,
    CONF_TIMEOUT,
    DEFAULT_CLIENT_NAME,
    DEFAULT_DEVICE_NAME,
    DEFAULT_DEVICE_VERSION,
    DEFAULT_EVENTS_ACTIVITY_LOG,
    DEFAULT_EVENTS_OTHER,
    DEFAULT_EVENTS_SESSIONS,
    DEFAULT_EVENTS_TASKS,
    DEFAULT_IGNORE_APP_PLAYERS,
    DEFAULT_IGNORE_DLNA_PLAYERS,
    DEFAULT_IGNORE_MOBILE_PLAYERS, # CORRECTED constant name
    DEFAULT_IGNORE_WEB_PLAYERS,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SSL_PORT,
    DEVICE_PROFILE_BASIC,
    DLNA_PLAYERS,
    KEEP_ALIVE_TIMEOUT,
    KEY_ALL,
    LATEST_QUERY_PARAMS,
    MOBILE_PLAYERS, # CORRECTED constant name
    WEB_PLAYERS,
    ApiUrl,
    Item,
    Query,
    Response,
    ServerType,
    Session,
    Value,
    WebsocketMessage,
)
# Import specific errors used
from .errors import ConnectError, RequestError, BrowseMediaError # Import necessary errors

_LOGGER = logging.getLogger(__package__)


class MediaBrowserHub:
    """Represents an Emby/Jellyfin connection."""

    def __init__(self, options: dict[str, Any]) -> None:
        """Initialize."""
        # Parse URL
        url_str = options.get(CONF_URL)
        if not url_str: raise ValueError("URL is required in configuration")
        parsed_url = urllib.parse.urlparse(url_str)
        if not parsed_url.scheme or not parsed_url.netloc: raise ValueError(f"Invalid URL format: {url_str}")
        self._host: str = parsed_url.hostname or ""
        if not self._host: raise ValueError(f"Could not parse hostname from URL: {url_str}")
        self._use_ssl: bool = parsed_url.scheme == "https"
        if parsed_url.port is not None:
            # Use the port specified in the URL
            self._port: int = parsed_url.port
        else:
            # Default to standard ports if none specified
            self._port: int = 443 if self._use_ssl else 80
        # --- End Corrected Port Logic ---
        # Store API Key
        self.api_key: str | None = options.get(CONF_API_KEY); _LOGGER.debug("Hub initialized %s API Key.", "with" if self.api_key else "without")
        self.user_id: str | None = options.get(CONF_CACHE_SERVER_USER_ID)
        # Other options
        self.timeout: float = options.get(CONF_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)
        self.client_name: str = options.get(CONF_CLIENT_NAME, DEFAULT_CLIENT_NAME)
        self.device_name: str = options.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME)
        self.device_id: str = options.get(CONF_DEVICE_ID) or uuid.uuid4().hex
        self.device_version: str = (options.get(CONF_DEVICE_VERSION, DEFAULT_DEVICE_VERSION))
        self._custom_name: str | None = options.get(CONF_NAME)
        # Player ignore options (CORRECTED CONSTANT)
        self.ignore_web_players: bool = options.get(CONF_IGNORE_WEB_PLAYERS, DEFAULT_IGNORE_WEB_PLAYERS)
        self.ignore_dlna_players: bool = options.get(CONF_IGNORE_DLNA_PLAYERS, DEFAULT_IGNORE_DLNA_PLAYERS)
        self.ignore_mobile_players: bool = options.get(CONF_IGNORE_MOBILE_PLAYERS, DEFAULT_IGNORE_MOBILE_PLAYERS)
        self.ignore_app_players: bool = options.get(CONF_IGNORE_APP_PLAYERS, DEFAULT_IGNORE_APP_PLAYERS)
        # Event options
        self.send_session_events: bool = options.get(CONF_EVENTS_SESSIONS, DEFAULT_EVENTS_SESSIONS)
        self.send_activity_events: bool = options.get(CONF_EVENTS_ACTIVITY_LOG, DEFAULT_EVENTS_ACTIVITY_LOG)
        self.send_task_events: bool = options.get(CONF_EVENTS_TASKS, DEFAULT_EVENTS_TASKS)
        self.send_other_events: bool = options.get(CONF_EVENTS_OTHER, DEFAULT_EVENTS_OTHER)
        # Cached server info
        self.server_id: str | None = options.get(CONF_CACHE_SERVER_ID)
        self.server_name: str | None = options.get(CONF_CACHE_SERVER_NAME)
        self.server_ping: str | None = options.get(CONF_CACHE_SERVER_PING)
        self.server_version: str | None = options.get(CONF_CACHE_SERVER_VERSION)
        self.server_type: ServerType = ServerType.UNKNOWN
        # Internal state
        self._last_keep_alive: datetime = datetime.utcnow()
        self._keep_alive_timeout: float | None = None
        self._is_api_key_validated: bool = False
        # Setup URLs and Headers
        schema_rest = "https" if self._use_ssl else "http"
        self._rest_url: str = f"{schema_rest}://{self._host}:{self._port}"
        base_path = parsed_url.path.rstrip('/')
        self._rest_url += base_path
        self.server_url: str = self._rest_url
        self._default_params: dict[str, Any] = {}
        self._default_headers: dict[str, Any] = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
        self._ws_url: str = ""
        self._auth_update()
        self._rest = aiohttp.ClientSession()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_loop: asyncio.Task[None] | None = None
        self._abort: bool = True
        self._availability_listeners: set[Callable[[bool], Awaitable[None]]] = set()
        self._availability_task: asyncio.Task[None] | None = None
        self.is_available: bool = False
        self._sessions: dict[str, dict[str, Any]] = {}
        self._raw_sessions: dict[str, dict[str, Any]] = {}
        self._library_infos: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._sessions_listeners: set[Callable[[list[dict[str, Any]]], Awaitable[None]]] = set()
        self._session_changed_listeners: set[Callable[[dict[str, Any] | None, dict[str, Any] | None], Awaitable[None]]] = set()
        self._library_listeners: dict[tuple[str, str, str], set[Callable[[dict[str, Any]], Awaitable[None]]]] = {}
        self._websocket_listeners: set[Callable[[str, dict[str, Any] | None], Awaitable[None]]] = set()
        self._last_activity_log_entry: str | None = None

    @property
    def name(self) -> str:
        """Return the name of the server."""
        return self._custom_name or self.server_name or DEFAULT_SERVER_NAME

    # --- Listener Registration Methods ---
    def on_availability_changed(
        self, callback: Callable[[bool], Awaitable[None]]
    ) -> Callable[[], None]:
        """Registers a callback for availability updates."""
        def remove_availability_listener() -> None:
            self._availability_listeners.discard(callback)
        self._availability_listeners.add(callback)
        return remove_availability_listener

    def on_sessions_changed(
        self, callback: Callable[[list[dict[str, Any]]], Awaitable[None]]
    ) -> Callable[[], None]:
        """Registers a callback for session updates."""
        def remove_sessions_listener() -> None:
            self._sessions_listeners.discard(callback)
        self._sessions_listeners.add(callback)
        return remove_sessions_listener

    def on_session_changed(
        self,
        callback: Callable[
            [dict[str, Any] | None, dict[str, Any] | None], Awaitable[None]
        ],
    ) -> Callable[[], None]:
        """Registers a callback for individual session changes."""
        def remove_session_changed_listener() -> None:
            self._session_changed_listeners.discard(callback)
        self._session_changed_listeners.add(callback)
        return remove_session_changed_listener

    def on_library_changed(
        self,
        library_id: str,
        user_id: str,
        item_type: str, # Should be ItemType but passed as string from options
        callback: Callable[[dict[str, Any]], Awaitable[None]],
    ):
        """Registers a callback for library changes."""
        key = (library_id, user_id, item_type)
        library_listeners = self._library_listeners.setdefault(key, set())
        self._library_infos.setdefault(key, {})
        def remove_library_listener() -> None:
            library_listeners.discard(callback)
            if not library_listeners:
                self._library_infos.pop(key, None)
                self._library_listeners.pop(key, None) # Also remove the key from listeners
        library_listeners.add(callback)
        return remove_library_listener

    def on_websocket_message(
        self, callback: Callable[[str, dict[str, Any] | None], Awaitable[None]]
    ) -> Callable[[], None]:
        """Registers a callback for WebSocket messages."""
        def remove_websocket_listener() -> None:
            self._websocket_listeners.discard(callback)
        self._websocket_listeners.add(callback)
        return remove_websocket_listener

    # --- API Call Methods ---
    async def async_command(self, session_id, command: str, data=None, params=None):
        await self._async_needs_authentication()
        url=f"{ApiUrl.SESSIONS}/{session_id}{ApiUrl.COMMAND}"
        command_data={"Name": command, "Arguments": data or {}}
        return await self._async_rest_post_get_text(url,command_data,params)

    async def async_get_artists(self, params):
        await self._async_needs_authentication()
        return await self._async_rest_get_json(ApiUrl.ARTISTS, params)

    async def async_get_genres(self, params):
        await self._async_needs_authentication()
        return await self._async_rest_get_json(ApiUrl.GENRES, params)

    async def async_get_items(self, params):
        await self._async_needs_authentication()
        # Return default empty structure if API call returns None or error
        return await self._async_rest_get_json(ApiUrl.ITEMS, params) or {Response.ITEMS: [], Response.TOTAL_RECORD_COUNT: 0}

    async def async_get_libraries(self):
        await self._async_needs_authentication()
        try:
            libs_resp=(await self._async_rest_get_json(ApiUrl.LIBRARIES,{Query.IS_HIDDEN:Value.FALSE}) or {})
            libs = libs_resp.get(Response.ITEMS,[])
            chans_resp=(await self._async_rest_get_json(ApiUrl.CHANNELS) or {})
            chans = chans_resp.get(Response.ITEMS,[])
            return libs+chans
        except Exception as e:
            _LOGGER.error("Error fetching libraries/channels: %s",e)
            return []

    async def async_get_persons(self, params):
        await self._async_needs_authentication()
        return await self._async_rest_get_json(ApiUrl.PERSONS, params)

    async def async_get_sessions(self):
        await self._async_needs_authentication()
        response = await self._async_rest_get_json(ApiUrl.SESSIONS)
        return self._preprocess_sessions(response or [])

    async def async_get_last_sessions(self):
        if self.is_available:
            try:
                return await self.async_get_sessions()
            except Exception as e:
                _LOGGER.warning("Failed to get sessions while available, returning cache: %s", e)
        return list(self._sessions.values())

    async def async_get_playback_info(self, item_id):
        await self._async_needs_authentication()
        playback_data={"DeviceProfile":DEVICE_PROFILE_BASIC,"AutoOpenLiveStream":True,"IsPlayback":True}
        user_id_val = self.user_id
        if user_id_val:
            playback_data["UserId"]=user_id_val
        else:
             _LOGGER.warning("User ID not available for playback info, results might be limited.")
             # Don't add UserId if None
        return await self._async_rest_post_get_json(f"{ApiUrl.ITEMS}/{item_id}{ApiUrl.PLAYBACK_INFO}", playback_data)

    async def async_get_prefixes(self, params):
        await self._async_needs_authentication()
        # Determine server type if not already known
        if self.server_type == ServerType.UNKNOWN:
            await self._async_determine_server_type()

        if self.server_type == ServerType.EMBY:
            prefix_resp = await self._async_rest_get_json(ApiUrl.PREFIXES, params)
            return prefix_resp or []
        else: # Jellyfin: Calculate from items
            items_resp = await self.async_get_items(params | {Query.FIELDS: Item.NAME}) # Only need Name field
            items = items_resp.get(Response.ITEMS, [])
            prefixes = {item[Item.NAME][0].upper() for item in items if item.get(Item.NAME)}
            return [{Item.NAME: prefix} for prefix in sorted(list(prefixes))]

    async def async_get_studios(self, params):
        await self._async_needs_authentication()
        return await self._async_rest_get_json(ApiUrl.STUDIOS, params)

    async def async_get_user_items(self, user_id_val, params):
        await self._async_needs_authentication()
        if not user_id_val:
            _LOGGER.warning("Attempted to get items for an invalid User ID.")
            return {Response.ITEMS: [], Response.TOTAL_RECORD_COUNT: 0} # Return empty result
        return await self._async_rest_get_json(f"{ApiUrl.USERS}/{user_id_val}{ApiUrl.ITEMS}", params)

    async def async_get_users(self):
        await self._async_needs_authentication()
        response = await self._async_rest_get_json(ApiUrl.USERS)
        return response or []

    async def async_get_years(self, params):
        await self._async_needs_authentication()
        return await self._async_rest_get_json(ApiUrl.YEARS, params)

    async def async_play(self, session_id, params=None):
        await self._async_needs_authentication()
        url=f"{ApiUrl.SESSIONS}/{session_id}{ApiUrl.PLAYING}"
        return await self._async_rest_post_get_text(url,params=params)

    async def async_play_command(self, session_id, command: str, params=None):
        await self._async_needs_authentication()
        url=f"{ApiUrl.SESSIONS}/{session_id}{ApiUrl.PLAYING}/{command}"
        return await self._async_rest_post_get_text(url,params=params)

    async def async_rescan(self):
        await self._async_needs_authentication()
        await self._async_rest_post_get_text(ApiUrl.LIBRARY_REFRESH)

    async def async_restart(self):
        await self._async_needs_authentication()
        await self._async_rest_post_get_text(ApiUrl.RESTART)

    async def async_shutdown(self):
        await self._async_needs_authentication()
        await self._async_rest_post_get_text(ApiUrl.SHUTDOWN)

    async def async_start(self, websocket: bool):
        if not self.api_key: raise ConfigEntryAuthFailed("API Key missing")
        # Perform checks before starting websocket
        await self._async_needs_server_verification()
        await self._async_needs_authentication()
        await self._async_needs_sessions()
        if websocket:
            if self._ws_loop is None or self._ws_loop.done():
                self._ws_loop = asyncio.create_task(self._async_ws_loop())

    async def async_test_api_key(self) -> dict[str, Any]:
        """Test if the current API key is valid by hitting an authenticated endpoint."""
        if not self.api_key:
            raise ConfigEntryAuthFailed("API Key is missing for validation")
        try:
            info = await self._async_rest_get_json(ApiUrl.INFO) # Use GET for Info
            if info and isinstance(info, dict) and "Id" in info and "ServerName" in info:
                self._is_api_key_validated = True
                # Optionally attempt to fetch user ID associated with the key
                try:
                    if not self.user_id:
                        # Example: Assume /Auth/Keys might provide user info, structure dependent
                        # auth_keys_info = await self._async_rest_get_json(ApiUrl.AUTH_KEYS)
                        # self.user_id = ... parse auth_keys_info ...
                        _LOGGER.debug("API Key validated via System/Info. User ID determination may need specific endpoint/logic.")
                except Exception as user_err:
                    _LOGGER.warning("Could not fetch user info with API key: %s", user_err)
                return info
            else:
                _LOGGER.warning("API Key test failed: Unexpected response format from System/Info: %s", info)
                raise ConfigEntryAuthFailed("API Key test failed: Unexpected response format")
        except ConfigEntryAuthFailed:
            raise
        except aiohttp.ClientResponseError as err:
            if err.status == 401: raise ConfigEntryAuthFailed("Invalid API Key") from err
            if err.status == 403: raise ConfigEntryAuthFailed("API Key does not have sufficient permissions") from err
            _LOGGER.error("API Key test failed with HTTP status %d: %s", err.status, err.message)
            raise RequestError(f"API Key test failed: {err.status} {err.message}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error during API key test: %s", err)
            raise ConfigEntryAuthFailed(f"API Key test failed: {err}") from err

    # --- Internal Helper Methods ---
    async def _async_get_activity_log_entries(self, params):
        await self._async_needs_authentication()
        return await self._async_rest_get_json(ApiUrl.ACTIVITY_LOG_ENTRIES, params)

    async def _async_needs_authentication(self):
        """Ensure the API key is set and validated."""
        if not self.api_key: raise ConfigEntryAuthFailed("API Key is missing")
        if not self._is_api_key_validated:
            _LOGGER.debug("API key test...")
            try:
                await self.async_test_api_key()
                _LOGGER.debug("API key validation successful.")
            except ConfigEntryAuthFailed as err:
                _LOGGER.warning("API key validation failed: %s", err)
                self._set_available(False)
                raise
            except Exception as err:
                _LOGGER.error("Unexpected error during API key validation: %s", err, exc_info=True)
                self._set_available(False)
                raise ConfigEntryAuthFailed(f"API key validation encountered an error: {err}") from err

    async def _async_needs_sessions(self):
        """Fetch initial sessions if none are cached."""
        if not self._sessions:
            try:
                sessions_list = await self.async_get_sessions()
                self._sessions = {session[Session.ID]: session for session in sessions_list if Session.ID in session}
                self._raw_sessions = {s[Session.ID]: get_session_event_data(s) for s in sessions_list if Session.ID in s}
                _LOGGER.debug("Fetched initial sessions: %d", len(self._sessions))
            except Exception as e:
                _LOGGER.warning("Failed to fetch initial sessions: %s", e)

    async def _async_determine_server_type(self) -> None:
        """Determine if the server is Emby or Jellyfin based on ping or info."""
        _LOGGER.debug("Attempting to determine server type...")
        ping_response = self.server_ping
        if not ping_response:
            try: ping_response = await self._async_ping() # Updates self.server_ping
            except Exception as e: _LOGGER.debug("Ping failed during server type determination: %s", e); ping_response = None

        if ping_response == "Jellyfin Server": self.server_type = ServerType.JELLYFIN
        elif ping_response == "Emby Server": self.server_type = ServerType.EMBY
        else:
            try:
                self._auth_update() # Set headers based on guess/current state
                info = await self._async_rest_get_json(ApiUrl.INFO)
                if info and isinstance(info, dict):
                    if "ProductName" in info and "Jellyfin" in info["ProductName"]: self.server_type = ServerType.JELLYFIN
                    elif "ServerName" in info and "Emby" in info["ServerName"]: self.server_type = ServerType.EMBY
                    else: _LOGGER.warning("Could not determine server type from info, defaulting to Jellyfin."); self.server_type = ServerType.JELLYFIN
                else: _LOGGER.warning("Could not get valid System/Info for type determination, defaulting to Jellyfin."); self.server_type = ServerType.JELLYFIN
            except Exception as e: _LOGGER.warning("Error getting System/Info for type determination (%s), defaulting to Jellyfin.", e); self.server_type = ServerType.JELLYFIN
        _LOGGER.debug("Determined server type: %s", self.server_type)
        self._auth_update() # Update auth mechanism based on determined type

    async def _async_needs_server_verification(self) -> None:
        """Gets information about the server and checks ID."""
        if self.server_type == ServerType.UNKNOWN: await self._async_determine_server_type()
        try:
            info = await self._async_rest_get_json(ApiUrl.INFO)
            if not info or not isinstance(info, dict): raise ConnectError("Received invalid System/Info response")
            server_id_val = info.get("Id")
            if not server_id_val: raise ConnectError("Server ID missing in System/Info response")
            if self.server_id is not None and self.server_id != server_id_val: raise ClientMismatchError(f"Server ID mismatch: {self.server_id} vs {server_id_val}")
            self.server_id = server_id_val
            self.server_name = info.get("ServerName", self.server_name)
            self.server_version = info.get("Version", self.server_version)
            if self.server_type == ServerType.UNKNOWN: # Refine type if still unknown
                if "ProductName" in info and "Jellyfin" in info["ProductName"]: self.server_type = ServerType.JELLYFIN
                elif "ServerName" in info and "Emby" in info["ServerName"]: self.server_type = ServerType.EMBY
                else: self.server_type = ServerType.JELLYFIN
                self._auth_update()
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Error fetching server info: %s (%s)", err.status, err.message)
            if err.status == 401: raise ConfigEntryAuthFailed("Authentication failed fetching server info") from err
            if err.status == 403: raise ConfigEntryAuthFailed("Permissions error fetching server info") from err
            raise ConnectError(f"Failed to connect fetching server info: {err.status}") from err
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, ConnectError) as err:
             _LOGGER.error("Connection error fetching server info: %s", err)
             raise ConnectError(f"Connection error fetching server info: {err}") from err
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching server info: %s", e)
            raise ConnectError(f"Unexpected error fetching server info: {e}") from e

    async def _async_ping(self):
        try:
            ping_resp = await self._async_rest_get_text(ApiUrl.PING)
            self.server_ping = ping_resp # Store ping response
            # Determine type based on ping, but don't overwrite if already known
            if self.server_type == ServerType.UNKNOWN:
                if ping_resp == "Jellyfin Server": self.server_type = ServerType.JELLYFIN
                elif ping_resp == "Emby Server": self.server_type = ServerType.EMBY
            return ping_resp
        except Exception as e:
            _LOGGER.debug("Ping failed: %s", e)
            return None

    def _auth_update(self) -> None:
        """Update authentication headers and URLs based on API key and server type."""
        self._default_params = {}
        self._default_headers = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
        schema_ws = "wss" if self._use_ssl else "ws"
        base_ws_url = self.server_url.replace("http", "ws", 1) # Base URL already includes path
        self._ws_url = ""

        if not self.api_key:
            _LOGGER.debug("No API key for auth update."); self._ws_url = f"{base_ws_url}/socket"; return

        server_type_to_use = self.server_type if self.server_type != ServerType.UNKNOWN else ServerType.JELLYFIN
        device_id_val = self.device_id or "ha-mediabrowser-unknown"

        if server_type_to_use == ServerType.JELLYFIN:
            self._ws_url = f"{base_ws_url}/socket?api_key={self.api_key}&deviceId={device_id_val}"
            auth_header = (f'MediaBrowser Client="{self.client_name}", Device="{self.device_name}", DeviceId="{device_id_val}", Version="{self.device_version}", Token="{self.api_key}"')
            self._default_headers["X-Emby-Authorization"] = auth_header
            self._default_headers.pop("X-Emby-Token", None) # Clear Emby token if present
        else: # Emby style
            self._ws_url = f"{base_ws_url}/embywebsocket?api_key={self.api_key}&deviceId={device_id_val}"
            self._default_headers["X-Emby-Token"] = self.api_key
            # Emby might need other X-Emby-* headers too, depending on version
            self._default_headers["X-Emby-Device-Id"] = device_id_val
            self._default_headers["X-Emby-Device"] = self.device_name
            self._default_headers["X-Emby-Client"] = self.client_name
            self._default_headers.pop("X-Emby-Authorization", None) # Clear Jellyfin header

        _LOGGER.debug("Auth updated for %s. WS URL: %s", server_type_to_use, self._ws_url)
        _LOGGER.debug("Default Headers: %s", self._default_headers)

    # --- REST Helper Methods ---
    async def _async_rest_post_response(self, url, data=None, params=None):
        target_url = self.server_url + url; request_params = self._default_params.copy();
        if params: request_params.update(params)
        _LOGGER.debug("POST %s | Params: %s | Headers: %s | Body: %s", target_url, request_params or None, self._default_headers, str(data)[:200])
        # Adjust SSL handling based on your needs (e.g., allow self-signed)
        ssl_verify = None if self._use_ssl else False # Basic handling
        async with async_timeout.timeout(self.timeout):
            result = await self._rest.post(target_url, json=data, params=request_params or None, headers=self._default_headers, ssl=ssl_verify, raise_for_status=False)
        if not result.ok: _LOGGER.warning("POST %s failed %d", target_url, result.status); result.raise_for_status()
        return result

    async def _async_rest_get_response(self, url, params=None):
        target_url = self.server_url + url; request_params = self._default_params.copy();
        if params: request_params.update(params)
        _LOGGER.debug("GET %s | Params: %s | Headers: %s", target_url, request_params or None, self._default_headers)
        ssl_verify = None if self._use_ssl else False # Basic handling
        async with async_timeout.timeout(self.timeout):
            result = await self._rest.get(target_url, params=request_params or None, headers=self._default_headers, ssl=ssl_verify, raise_for_status=False)
        if not result.ok: _LOGGER.warning("GET %s failed %d", target_url, result.status); result.raise_for_status()
        return result

    async def _async_rest_post_get_json(self, url, data=None, params=None):
        response = await self._async_rest_post_response(url, data, params)
        content_type = response.headers.get('Content-Type', '');
        if 'application/json' in content_type:
            try: return await response.json() or {}
            except json.JSONDecodeError as e: _LOGGER.warning("Failed JSON decode POST %s: %s", url, e); return {}
        else: _LOGGER.warning("Non-JSON response POST %s (Type: %s)", url, content_type); return {}

    async def _async_rest_post_get_text(self, url, data=None, params=None):
        response = await self._async_rest_post_response(url, data, params); return await response.text()

    async def _async_rest_get_json(self, url, params=None):
        response = await self._async_rest_get_response(url, params)
        content_type = response.headers.get('Content-Type', '');
        if 'application/json' in content_type:
            if response.content_length == 0: return {}
            try: return await response.json() or {}
            except json.JSONDecodeError as e: _LOGGER.warning("Failed JSON decode GET %s: %s", url, e); return None
        else: _LOGGER.warning("Non-JSON response GET %s (Type: %s)", url, content_type); return None

    async def _async_rest_get_text(self, url, params=None):
        response = await self._async_rest_get_response(url, params); return await response.text()

    # --- Websocket Methods ---
    async def _async_ws_connect(self):
        if not self._ws_url: raise ConnectionError("WS URL missing")
        _LOGGER.debug("Connect WS: %s", self._ws_url)
        self._abort=False
        ssl_verify = None if self._use_ssl else False # Basic handling
        async with async_timeout.timeout(self.timeout):
            self._ws = await self._rest.ws_connect(self._ws_url, headers=self._default_headers, ssl=ssl_verify)
        # Send initial messages after successful connection
        await self._ws.send_json({"MessageType":"SessionsStart", "Data": "0,1500"})
        if self.send_activity_events: await self._ws.send_json({"MessageType":"ActivityLogEntryStart", "Data": "0,1000"})
        if self.send_task_events: await self._ws.send_json({"MessageType":"ScheduledTasksInfoStart", "Data": "0,1500"})
        _LOGGER.debug("Websocket connected and initial messages sent.")

    async def _async_ws_disconnect(self):
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _async_ws_loop(self):
        delay = 1
        while not self._abort:
            try:
                await self._async_needs_server_verification()
                await self._async_needs_authentication()
                await self._async_needs_sessions()
                await self._async_ws_connect()
            except ClientMismatchError as err: _LOGGER.error("Server mismatch, stopping: %s", err); self._set_available(False); break
            except ConfigEntryAuthFailed as err: _LOGGER.error("Auth failed during WS setup, stopping: %s", err); self._set_available(False); break
            except (aiohttp.ClientConnectionError, aiohttp.WSServerHandshakeError) as err: _LOGGER.warning("WS connect failed: %s", err)
            except aiohttp.ClientResponseError as err:
                _LOGGER.warning("WS request error: %s (%s)", err.status, err.message)
                if err.status in (401, 403): _LOGGER.error("WS connection unauthorized, stopping."); self._set_available(False); break
            except (asyncio.TimeoutError, TimeoutError) as err: _LOGGER.warning("Timeout during WS connection: %s", err)
            except asyncio.CancelledError: _LOGGER.debug("WS loop task cancelled during setup."); break
            except Exception as err: _LOGGER.exception("Unexpected error during WS setup: %s", err)
            else: # Connection successful
                self._set_available(True); delay = 1; _LOGGER.info("WS connection established to %s", self.name)
                while not self._abort and self._ws is not None and not self._ws.closed:
                    try:
                        msg = await self._ws.receive(timeout=KEEP_ALIVE_TIMEOUT * 1.5)
                        if msg.type == aiohttp.WSMsgType.TEXT: asyncio.create_task(self._handle_message(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED): _LOGGER.warning("WS connection closed by server (Type: %s).", msg.type); break
                        elif msg.type == aiohttp.WSMsgType.ERROR: _LOGGER.error("WS error received: %s", self._ws.exception()); break
                    except asyncio.TimeoutError:
                        if self._keep_alive_timeout is None:
                            now = datetime.utcnow()
                            if (now - self._last_keep_alive).total_seconds() >= KEEP_ALIVE_TIMEOUT: self._send_keep_alive()
                    except asyncio.CancelledError: _LOGGER.debug("WS loop task cancelled during handling."); self._abort = True; break
                    except Exception as err: _LOGGER.exception("Error handling WS message: %s", err); break
                # After inner loop breaks
                if not self._abort: _LOGGER.warning("WS disconnected unexpectedly. Reconnecting...")
                self._set_available(False); await self._async_ws_disconnect()
            # Reconnect delay
            if not self._abort:
                _LOGGER.debug("Attempting WS reconnect in %d seconds", delay); await asyncio.sleep(delay); delay = min(delay * 2, 60)
        _LOGGER.debug("Websocket loop finished.")


    # --- Listener Calling Methods ---
    async def _call_availability_listeners(self, available):
        listeners_to_call = list(self._availability_listeners)
        _LOGGER.debug("Calling %d availability listeners with state: %s", len(listeners_to_call), available)
        for listener in listeners_to_call:
            try: await listener(available)
            except Exception as err: _LOGGER.error("Error calling availability listener %s: %s", getattr(listener, '__name__', repr(listener)), err, exc_info=True)

    async def _call_sessions_listeners(self, sessions):
        listeners_to_call = list(self._sessions_listeners)
        _LOGGER.debug("Calling %d sessions listeners.", len(listeners_to_call))
        for listener in listeners_to_call:
            try: await listener(sessions)
            except Exception as err: _LOGGER.error("Error calling sessions listener %s: %s", getattr(listener, '__name__', repr(listener)), err, exc_info=True)

    async def _call_session_changed_listeners(self, added, removed, updated):
        listeners_to_call = list(self._session_changed_listeners)
        events = ([(None, session) for session in added] + [(session, None) for session in removed] + updated)
        _LOGGER.debug("Calling %d session changed listeners for %d events.", len(listeners_to_call), len(events))
        for event in events:
            for listener in listeners_to_call:
                try: await listener(event[0], event[1])
                except Exception as err: _LOGGER.error("Error calling session changed listener %s: %s", getattr(listener, '__name__', repr(listener)), err, exc_info=True)

    async def _call_library_listeners(self, library_ids):
        listeners_dict_copy = self._library_listeners.copy(); infos_copy = self._library_infos.copy()
        listeners_to_call_map: dict[tuple[str, str, str], list[Callable]] = {}
        for key, listeners in listeners_dict_copy.items():
             library_id, _, _ = key
             if library_id in library_ids or library_id == KEY_ALL: listeners_to_call_map[key] = list(listeners)
        _LOGGER.debug("Calling library listeners for keys: %s", list(listeners_to_call_map.keys()))
        for key, listeners in listeners_to_call_map.items():
            info_to_pass = infos_copy.get(key)
            if info_to_pass is None: _LOGGER.warning("No library info found for key %s", key); continue
            for listener in listeners:
                try: await listener(info_to_pass)
                except Exception as err: _LOGGER.error("Error calling library listener %s for key %s: %s", getattr(listener, '__name__', repr(listener)), key, err, exc_info=True)

    async def _call_websocket_listeners(self, message_type, data):
        listeners_to_call = list(self._websocket_listeners)
        # Avoid overly verbose logging for frequent messages like KeepAlive
        # if message_type != "keep_alive":
        #    _LOGGER.debug("Calling %d websocket listeners for type: %s", len(listeners_to_call), message_type)
        for listener in listeners_to_call:
            try: await listener(message_type, data)
            except Exception as err: _LOGGER.error("Error calling websocket listener %s for type %s: %s", getattr(listener, '__name__', repr(listener)), message_type, err, exc_info=True)

    async def _call_websocket_listeners_for_list(self, messages):
        listeners_to_call = list(self._websocket_listeners);
        if not listeners_to_call: return
        _LOGGER.debug("Calling %d websocket listeners for %d messages.", len(listeners_to_call), len(messages))
        for message_type, data in messages:
            for listener in listeners_to_call:
                try: await listener(message_type, data)
                except Exception as err: _LOGGER.error("Error calling websocket listener %s for type %s: %s", getattr(listener, '__name__', repr(listener)), message_type, err, exc_info=True)

    # --- Other Internal Methods ---
    def _set_available(self, availability: bool):
        if self.is_available == availability: return
        _LOGGER.info("%s server became %s", self.name, "available" if availability else "unavailable")
        self.is_available = availability
        if self._availability_listeners:
            if self._availability_task and not self._availability_task.done(): self._availability_task.cancel()
            self._availability_task = asyncio.create_task(self._call_availability_listeners(availability))

    async def _handle_message(self, message_str: str):
        try: msg = json.loads(message_str)
        except json.JSONDecodeError: _LOGGER.warning("Invalid JSON WS: %s", message_str[:200]); return
        msg_type = msg.get("MessageType"); data = msg.get("Data")
        if not msg_type: _LOGGER.debug("WS no type: %s", msg); return

        call_listeners = self.send_other_events
        # Use a dictionary or if/elif chain for message type handling
        if msg_type == WebsocketMessage.SESSIONS:
            if isinstance(data, list): asyncio.create_task(self._handle_sessions_message(data))
            else: _LOGGER.warning("SESSIONS !list: %s", type(data))
            call_listeners = False
        elif msg_type == WebsocketMessage.KEEP_ALIVE:
            #_LOGGER.debug("KeepAlive recv") # Can be too verbose
            self._last_keep_alive = datetime.utcnow(); call_listeners = False
        elif msg_type == WebsocketMessage.FORCE_KEEP_ALIVE:
             try: nt=max(5.0,(int(data)/1000.0)*0.8); self._keep_alive_timeout=nt; _LOGGER.debug("Dyn keep-alive: %.1fs", nt)
             except Exception as e: _LOGGER.warning("Bad FORCE_KA (%s): %s", data, e); self._keep_alive_timeout = KEEP_ALIVE_TIMEOUT
             call_listeners = False
        elif msg_type == WebsocketMessage.LIBRARY_CHANGED:
             if self._library_listeners:
                 if isinstance(data, dict): asyncio.create_task(self._handle_library_changed_message(data)); data = get_library_changed_event_data(data)
                 else: _LOGGER.warning("LIB_CHG !dict: %s", type(data)); data = {}
             else: call_listeners = False
        elif msg_type == WebsocketMessage.ACTIVITY_LOG_ENTRY:
             asyncio.create_task(self._handle_activity_log_message()); call_listeners = False
        elif msg_type == WebsocketMessage.SCHEDULED_TASK_INFO:
             call_listeners = self.send_task_events
        elif msg_type == WebsocketMessage.USER_DATA_CHANGED:
             if isinstance(data, dict): data = get_user_data_changed_event_data(data)
             else: _LOGGER.warning("USER_DATA !dict: %s", type(data)); data = {}
        # Add other message types here...

        # Generic listener call
        if call_listeners and self._websocket_listeners:
            event_data = {"server_id": self.server_id, snake_case(msg_type): data or {}}
            asyncio.create_task(self._call_websocket_listeners(snake_case(msg_type), event_data))

        # Keep-alive check (only if server doesn't dictate)
        if self._keep_alive_timeout is None:
            now = datetime.utcnow()
            if (now - self._last_keep_alive).total_seconds() >= KEEP_ALIVE_TIMEOUT:
                self._send_keep_alive()

    async def _handle_sessions_message(self, sessions: list[dict[str, Any]]) -> None:
        _LOGGER.debug("Processing sessions message with %d sessions.", len(sessions))
        new_raw = {s["Id"]: get_session_event_data(deepcopy(s)) for s in sessions if "Id" in s}
        new_proc = {s["Id"]: s for s in self._preprocess_sessions(deepcopy(sessions)) if "Id" in s}
        added_raw, removed_raw, updated_raw = self._get_changed_sessions(self._raw_sessions, new_raw)
        added, removed, updated = self._get_changed_sessions(self._sessions, new_proc)
        self._raw_sessions = new_raw; self._sessions = new_proc
        if added or removed or updated:
             if self._sessions_listeners: asyncio.create_task(self._call_sessions_listeners(list(new_proc.values())))
             if self._session_changed_listeners: asyncio.create_task(self._call_session_changed_listeners(added, removed, updated))
        if self.send_session_events and self._websocket_listeners and (added_raw or removed_raw or updated_raw):
             msgs = ([("session_changed", {"old": None, "new": s}) for s in added_raw] +
                     [("session_changed", {"old": s, "new": None}) for s in removed_raw] +
                     [("session_changed", {"old": old, "new": new}) for old, new in updated_raw])
             asyncio.create_task(self._call_websocket_listeners_for_list(msgs))

    async def _handle_activity_log_message(self):
        if not self.send_activity_events or not self._websocket_listeners: return
        _LOGGER.debug("Handling activity log update trigger.")
        params = {}; last_seen = self._last_activity_log_entry
        if last_seen: params[Query.MIN_DATE] = last_seen
        else: params[Query.LIMIT] = 1 # Fetch only one initially
        try: response = await self._async_get_activity_log_entries(params)
        except Exception as e: _LOGGER.error("Failed fetch activity log: %s", e); return
        entries = response.get(Response.ITEMS); new_last_time = None
        if not entries: _LOGGER.debug("No new activity log entries."); return
        try: new_last_time = max((e.get(Item.DATE) for e in entries if e.get(Item.DATE)), default=last_seen)
        except ValueError: new_last_time = last_seen
        sorted_entries = sorted((e for e in entries if e.get(Item.DATE)), key=lambda x: x.get(Item.DATE, "")); to_fire = []
        if last_seen: to_fire = [e for e in sorted_entries if e.get(Item.DATE, "") > last_seen]
        else: to_fire = sorted_entries # Fire all on first run
        if to_fire and new_last_time and new_last_time > (last_seen or ""): self._last_activity_log_entry = new_last_time
        if to_fire:
             _LOGGER.debug("Firing %d new activity events.", len(to_fire))
             messages = [("activity_log_entry", entry | {"server_id": self.server_id}) for entry in to_fire]
             asyncio.create_task(self._call_websocket_listeners_for_list(messages))
        else: _LOGGER.debug("No activity log entries to fire.")

    async def _handle_library_changed_message(self, data: dict[str, Any], force_updates: bool = False) -> None:
        if not self._library_listeners: return
        _LOGGER.debug("Handling library changed (Force:%s): %s", force_updates, data.get(Item.COLLECTION_FOLDERS))
        folders = data.get(Item.COLLECTION_FOLDERS, []); affected_keys = set()
        # Determine affected keys based on folders or force update
        if not folders and not force_updates:
            affected_keys = {k for k in self._library_listeners if k[0] == KEY_ALL}
            if not affected_keys: return
        else: affected_keys = {k for k in self._library_listeners if force_updates or k[0] == KEY_ALL or k[0] in folders}
        if not affected_keys: _LOGGER.debug("No sensors match changed folders."); return

        _LOGGER.debug("Updating library sensors for keys: %s", affected_keys)
        old_infos = self._library_infos.copy(); new_infos = self._library_infos.copy()
        # Use dictionary to map keys to tasks for easier result processing
        fetch_tasks = {key: asyncio.create_task(self._fetch_library_info(*key)) for key in affected_keys}
        # Wait for all fetches to complete
        await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)

        updated_keys_for_listeners = set()
        for key, task in fetch_tasks.items():
             try:
                 result = task.result() # Get result or raise exception
                 if result is not None: # Fetch successful (even if empty list)
                      new_infos[key] = result
                      if force_updates or old_infos.get(key) != result: updated_keys_for_listeners.add(key)
                 else: # Fetch returned None (treat as empty/failed?)
                      new_infos[key] = {Response.ITEMS: [], Response.TOTAL_RECORD_COUNT: 0}
                      if force_updates or old_infos.get(key) != new_infos[key]: updated_keys_for_listeners.add(key)
             except Exception as e:
                 _LOGGER.error("Failed fetch library info for %s: %s", key, e)
                 # Keep old data by not updating new_infos[key] or pop it? Let's pop.
                 new_infos.pop(key, None)


        self._library_infos = new_infos # Update main cache

        if updated_keys_for_listeners:
             listeners_to_call_map = {
                 key: list(self._library_listeners.get(key, []))
                 for key in updated_keys_for_listeners if key in self._library_listeners
             }
             for key, listeners in listeners_to_call_map.items():
                  info = new_infos.get(key)
                  if info is None: continue # Should not happen if key is in map
                  for listener in listeners:
                       try: asyncio.create_task(listener(info))
                       except Exception as err: _LOGGER.error("Error scheduling library listener for %s: %s", key, err, exc_info=True)

    async def _fetch_library_info(self, library_id: str, user_id: str, item_type: str) -> dict | None:
        key = (library_id, user_id, item_type)
        try:
            params = LATEST_QUERY_PARAMS.copy(); params[Query.INCLUDE_ITEM_TYPES] = item_type
            if library_id != KEY_ALL: params[Query.PARENT_ID] = library_id
            if user_id != KEY_ALL:
                 if not user_id: _LOGGER.warning("Invalid User ID '%s' for key %s", user_id, key); return None
                 # Use await here
                 return await self.async_get_user_items(user_id, params)
            else:
                 # Use await here
                 return await self.async_get_items(params)
        except Exception as e:
            _LOGGER.error("Exception fetch library for %s: %s", key, e); raise

    def force_library_change(self, library_id: str):
        _LOGGER.debug("Force lib update ID: %s", library_id)
        asyncio.create_task(self._handle_library_changed_message({Item.COLLECTION_FOLDERS:[library_id]}, force_updates=True))

    def _get_changed_sessions(self, old: dict, new: dict):
        added = [s for k, s in new.items() if k not in old]
        removed = [s for k, s in old.items() if k not in new]
        updated = [(old[k], s) for k, s in new.items() if k in old and old[k] != s]
        return added, removed, updated

    def _preprocess_sessions(self, sessions: list[dict[str, Any]]):
        # Ensure correct constant names are used
        return [ s for s in sessions if s.get(Item.DEVICE_ID) != self.device_id and s.get(Item.CLIENT) != self.client_name and
            (not self.ignore_web_players or s.get(Item.CLIENT) not in WEB_PLAYERS) and
            (not self.ignore_dlna_players or s.get(Item.CLIENT) not in DLNA_PLAYERS) and
            (not self.ignore_mobile_players or s.get(Item.CLIENT) not in MOBILE_PLAYERS) and # Corrected constant
            (not self.ignore_app_players or s.get(Item.CLIENT) not in APP_PLAYERS) ]

    def _send_keep_alive(self) -> None:
        if not self._abort and self._ws is not None and not self._ws.closed:
            _LOGGER.debug("Send KA %s", self.name)
            try: asyncio.create_task(self._ws.send_json({"MessageType": "KeepAlive"})); self._last_keep_alive = datetime.utcnow()
            except Exception as e: _LOGGER.warning("Fail send KA: %s", e)

    async def async_stop(self) -> None:
        _LOGGER.debug("Stop hub %s", self.name); self._abort = True
        if self._ws_loop is not None and not self._ws_loop.done():
            self._ws_loop.cancel();
            try: await self._ws_loop
            except asyncio.CancelledError: _LOGGER.debug("WS loop cancel stop.")
        await self._async_ws_disconnect(); self._ws_loop = None
        if self._rest and not self._rest.closed:
            await self._rest.close(); _LOGGER.debug("aiohttp closed.")
        _LOGGER.debug("Hub stop %s complete.", self.name)

class ClientMismatchError(ConnectError):
    """Error to indicate that server unique id mismatch."""

# --- End of MediaBrowserHub class ---