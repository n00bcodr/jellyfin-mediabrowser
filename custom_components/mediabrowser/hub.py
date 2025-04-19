# custom_components/mediabrowser/hub.py
# --- CORRECTED VERSION (Indentation, ClientMismatchError Import, WS Logging) ---
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
from homeassistant.exceptions import HomeAssistantError # Import base HA error
from homeassistant.util import uuid as uuid_util

# Ensure all helpers needed are imported
from .helpers import (
    get_library_changed_event_data,
    get_user_data_changed_event_data,
    get_session_event_data,
    snake_case,
    size_of, # Import size_of if used elsewhere or remove
)

from .const import (
    APP_PLAYERS, CONF_API_KEY, CONF_CACHE_SERVER_ID, CONF_CACHE_SERVER_NAME,
    CONF_CACHE_SERVER_PING, CONF_CACHE_SERVER_USER_ID, CONF_CACHE_SERVER_VERSION,
    CONF_CLIENT_NAME, CONF_DEVICE_ID, CONF_DEVICE_NAME, CONF_DEVICE_VERSION,
    CONF_EVENTS_ACTIVITY_LOG, CONF_EVENTS_OTHER, CONF_EVENTS_SESSIONS,
    CONF_EVENTS_TASKS, CONF_IGNORE_APP_PLAYERS, CONF_IGNORE_DLNA_PLAYERS,
    CONF_IGNORE_MOBILE_PLAYERS, CONF_IGNORE_WEB_PLAYERS, CONF_TIMEOUT,
    DEFAULT_CLIENT_NAME, DEFAULT_DEVICE_NAME, DEFAULT_DEVICE_VERSION,
    DEFAULT_EVENTS_ACTIVITY_LOG, DEFAULT_EVENTS_OTHER, DEFAULT_EVENTS_SESSIONS,
    DEFAULT_EVENTS_TASKS, DEFAULT_IGNORE_APP_PLAYERS, DEFAULT_IGNORE_DLNA_PLAYERS,
    DEFAULT_IGNORE_MOBILE_PLAYERS, DEFAULT_IGNORE_WEB_PLAYERS, DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT, DEFAULT_SSL_PORT, DEVICE_PROFILE_BASIC, DLNA_PLAYERS,
    KEEP_ALIVE_TIMEOUT, KEY_ALL, LATEST_QUERY_PARAMS, MOBILE_PLAYERS, WEB_PLAYERS,
    ApiUrl, Item, Query, Response, ServerType, Session, Value, WebsocketMessage,
)
# Import specific errors used FROM errors.py (ClientMismatchError is defined below)
from .errors import ConnectError, RequestError, BrowseMediaError, ForbiddenError, UnauthorizedError, NotFoundError # Import necessary errors defined in errors.py

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
        # --- CORRECTED Port Logic ---
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
        # Player ignore options
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
    def on_availability_changed(self, callback: Callable[[bool], Awaitable[None]]) -> Callable[[], None]:
        """Registers a callback for availability updates."""
        def remove_availability_listener() -> None: self._availability_listeners.discard(callback)
        self._availability_listeners.add(callback); return remove_availability_listener

    def on_sessions_changed(self, callback: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> Callable[[], None]:
        """Registers a callback for session updates."""
        def remove_sessions_listener() -> None: self._sessions_listeners.discard(callback)
        self._sessions_listeners.add(callback); return remove_sessions_listener

    def on_session_changed(self, callback: Callable[[dict[str, Any] | None, dict[str, Any] | None], Awaitable[None]]) -> Callable[[], None]:
        """Registers a callback for individual session changes."""
        def remove_session_changed_listener() -> None: self._session_changed_listeners.discard(callback)
        self._session_changed_listeners.add(callback); return remove_session_changed_listener

    def on_library_changed(self, library_id: str, user_id: str, item_type: str, callback: Callable[[dict[str, Any]], Awaitable[None]]) -> Callable[[], None]:
        """Registers a callback for library changes."""
        key = (library_id, user_id, item_type); library_listeners = self._library_listeners.setdefault(key, set())
        self._library_infos.setdefault(key, {}); library_listeners.add(callback)
        def remove_library_listener() -> None:
            library_listeners.discard(callback)
            if not library_listeners: self._library_infos.pop(key, None); self._library_listeners.pop(key, None)
        return remove_library_listener

    def on_websocket_message(self, callback: Callable[[str, dict[str, Any] | None], Awaitable[None]]) -> Callable[[], None]:
        """Registers a callback for WebSocket messages."""
        def remove_websocket_listener() -> None: self._websocket_listeners.discard(callback)
        self._websocket_listeners.add(callback); return remove_websocket_listener

    # --- API Call Methods (Ensure consistent error handling and logging) ---
    async def async_command(self, session_id, command: str, data=None, params=None):
        await self._async_needs_authentication(); url=f"{ApiUrl.SESSIONS}/{session_id}{ApiUrl.COMMAND}"
        cmd_data={"Name": command, "Arguments": data or {}}; return await self._async_rest_post_get_text(url, cmd_data, params)
    async def async_get_artists(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.ARTISTS, params)
    async def async_get_genres(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.GENRES, params)
    async def async_get_items(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.ITEMS, params) or {Response.ITEMS: [], Response.TOTAL_RECORD_COUNT: 0}
    async def async_get_libraries(self):
        await self._async_needs_authentication(); libs=[]; chans=[]
        try: libs_resp=(await self._async_rest_get_json(ApiUrl.LIBRARIES,{Query.IS_HIDDEN:Value.FALSE}) or {}); libs=libs_resp.get(Response.ITEMS,[])
        except Exception as e: _LOGGER.error("Err fetch libs: %s",e)
        try: chans_resp=(await self._async_rest_get_json(ApiUrl.CHANNELS) or {}); chans=chans_resp.get(Response.ITEMS,[])
        except Exception as e: _LOGGER.error("Err fetch chans: %s",e)
        return sorted(libs+chans, key=lambda x: x.get(Item.NAME, ""))
    async def async_get_persons(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.PERSONS, params)
    async def async_get_sessions(self): await self._async_needs_authentication(); resp=await self._async_rest_get_json(ApiUrl.SESSIONS); return self._preprocess_sessions(resp or [])
    async def async_get_last_sessions(self):
        if self.is_available:
            try: return await self.async_get_sessions()
            except Exception as e: _LOGGER.warning("Fail get sess: %s, using cache",e)
        return list(self._sessions.values())
    async def async_get_playback_info(self, item_id):
        await self._async_needs_authentication(); pb_data={"DeviceProfile":DEVICE_PROFILE_BASIC,"AutoOpenLiveStream":True,"IsPlayback":True}
        uid=self.user_id;
        if uid: pb_data["UserId"]=uid
        else: _LOGGER.warning("No UserID for playback")
        return await self._async_rest_post_get_json(f"{ApiUrl.ITEMS}/{item_id}{ApiUrl.PLAYBACK_INFO}", pb_data)
    async def async_get_prefixes(self, params):
        await self._async_needs_authentication();
        if self.server_type == ServerType.UNKNOWN: await self._async_determine_server_type()
        if self.server_type == ServerType.EMBY: resp=await self._async_rest_get_json(ApiUrl.PREFIXES, params); return resp or []
        else: items_resp=await self.async_get_items(params|{Query.FIELDS:Item.NAME}); items=items_resp.get(Response.ITEMS,[]); px={i[Item.NAME][0].upper() for i in items if i.get(Item.NAME)}; return [{Item.NAME:p} for p in sorted(list(px))]
    async def async_get_studios(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.STUDIOS, params)
    async def async_get_user_items(self, uid, params): await self._async_needs_authentication(); if not uid: _LOGGER.warning("Invalid UserID"); return {Response.ITEMS:[],Response.TOTAL_RECORD_COUNT:0}; return await self._async_rest_get_json(f"{ApiUrl.USERS}/{uid}{ApiUrl.ITEMS}", params)
    async def async_get_users(self): await self._async_needs_authentication(); resp=await self._async_rest_get_json(ApiUrl.USERS); return resp or []
    async def async_get_years(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.YEARS, params)
    async def async_play(self, sid, params=None): await self._async_needs_authentication(); url=f"{ApiUrl.SESSIONS}/{sid}{ApiUrl.PLAYING}"; return await self._async_rest_post_get_text(url,params=params)
    async def async_play_command(self, sid, cmd, params=None): await self._async_needs_authentication(); url=f"{ApiUrl.SESSIONS}/{sid}{ApiUrl.PLAYING}/{cmd}"; return await self._async_rest_post_get_text(url,params=params)
    async def async_rescan(self): await self._async_needs_authentication(); await self._async_rest_post_get_text(ApiUrl.LIBRARY_REFRESH)
    async def async_restart(self): await self._async_needs_authentication(); await self._async_rest_post_get_text(ApiUrl.RESTART)
    async def async_shutdown(self): await self._async_needs_authentication(); await self._async_rest_post_get_text(ApiUrl.SHUTDOWN)
    async def async_start(self, websocket: bool):
        if not self.api_key: raise ConfigEntryAuthFailed("API Key missing")
        await self._async_needs_server_verification(); await self._async_needs_authentication(); await self._async_needs_sessions()
        if websocket:
            if self._ws_loop is None or self._ws_loop.done(): self._ws_loop = asyncio.create_task(self._async_ws_loop())
    async def async_test_api_key(self):
        if not self.api_key: raise ConfigEntryAuthFailed("API Key missing for validation")
        try:
            info=await self._async_rest_get_json(ApiUrl.INFO) # Use GET now
            if info and isinstance(info,dict) and "Id" in info and "ServerName" in info:
                self._is_api_key_validated=True;
                try:
                    if not self.user_id: _LOGGER.debug("API Key ok via Info. UserID TBD.") # Simplified user fetch logic
                except Exception as user_err: _LOGGER.warning("Could not fetch user info: %s", user_err)
                return info
            else: _LOGGER.warning("API test fail: Bad Info resp: %s", info); raise ConfigEntryAuthFailed("API test fail: Bad response")
        except ConfigEntryAuthFailed: raise
        except aiohttp.ClientResponseError as err:
            if err.status==401: raise ConfigEntryAuthFailed("Invalid API Key") from err
            if err.status==403: raise ConfigEntryAuthFailed("API Key permissions error") from err
            _LOGGER.error("API test fail HTTP %d: %s",err.status,err.message); raise RequestError(f"API test fail: {err.status} {err.message}") from err
        except Exception as err: _LOGGER.exception("Unexpected API test error: %s",err); raise ConfigEntryAuthFailed(f"API test fail: {err}") from err
    # --- Internal Helpers ---
    async def _async_get_activity_log_entries(self, params): await self._async_needs_authentication(); return await self._async_rest_get_json(ApiUrl.ACTIVITY_LOG_ENTRIES, params)
    async def _async_needs_authentication(self):
        if not self.api_key: raise ConfigEntryAuthFailed("API Key missing")
        if not self._is_api_key_validated:
            _LOGGER.debug("API key test...")
            try: await self.async_test_api_key(); _LOGGER.debug("API key validation successful.")
            except ConfigEntryAuthFailed as err: _LOGGER.warning("API key validation failed: %s", err); self._set_available(False); raise
            except Exception as err: _LOGGER.error("Unexpected API valid err: %s", err, exc_info=True); self._set_available(False); raise ConfigEntryAuthFailed(f"API valid err: {err}") from err
    async def _async_needs_sessions(self):
        if not self._sessions:
            try: sl=await self.async_get_sessions(); self._sessions={s[Session.ID]:s for s in sl if Session.ID in s}; self._raw_sessions={s[Session.ID]:get_session_event_data(s) for s in sl if Session.ID in s}; _LOGGER.debug("Fetched initial sessions: %d", len(self._sessions))
            except Exception as e: _LOGGER.warning("Failed to fetch initial sessions: %s", e)
    async def _async_determine_server_type(self):
        _LOGGER.debug("Determine type..."); pr=self.server_ping
        if not pr: try: pr=await self._async_ping() except Exception as e: _LOGGER.debug("Ping fail type detect: %s",e); pr=None
        if pr=="Jellyfin Server": self.server_type=ServerType.JELLYFIN
        elif pr=="Emby Server": self.server_type=ServerType.EMBY
        else:
            try:
                self._auth_update(); info=await self._async_rest_get_json(ApiUrl.INFO) # Use GET now
                if info and isinstance(info,dict):
                    if "ProductName" in info and "Jellyfin" in info["ProductName"]: self.server_type=ServerType.JELLYFIN
                    elif "ServerName" in info and "Emby" in info["ServerName"]: self.server_type=ServerType.EMBY
                    else: _LOGGER.warning("Default type Jellyfin."); self.server_type=ServerType.JELLYFIN
                else: _LOGGER.warning("Bad Info, default type Jellyfin."); self.server_type=ServerType.JELLYFIN
            except Exception as e: _LOGGER.warning("Err Info type detect (%s), default Jellyfin.",e); self.server_type=ServerType.JELLYFIN
        _LOGGER.debug("Determined type: %s", self.server_type); self._auth_update()
    async def _async_needs_server_verification(self):
        if self.server_type==ServerType.UNKNOWN: await self._async_determine_server_type()
        try:
            info=await self._async_rest_get_json(ApiUrl.INFO) # Use GET now
            if not info or not isinstance(info,dict): raise ConnectError("Bad Info resp")
            sid=info.get("Id"); if not sid: raise ConnectError("No server ID")
            if self.server_id is not None and self.server_id!=sid: raise ClientMismatchError(f"ID mismatch: {self.server_id} vs {sid}")
            self.server_id=sid; self.server_name=info.get("ServerName",self.server_name); self.server_version=info.get("Version",self.server_version)
            if self.server_type==ServerType.UNKNOWN: # Refine type if still unknown
                if "ProductName" in info and "Jellyfin" in info["ProductName"]: self.server_type=ServerType.JELLYFIN
                elif "ServerName" in info and "Emby" in info["ServerName"]: self.server_type=ServerType.EMBY
                else: self.server_type=ServerType.JELLYFIN
                self._auth_update()
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Err fetch info: %s (%s)",err.status,err.message)
            if err.status==401: raise ConfigEntryAuthFailed("Auth fail fetch info") from err
            if err.status==403: raise ConfigEntryAuthFailed("Perms err fetch info") from err
            raise ConnectError(f"Fail fetch info: {err.status}") from err
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, ConnectError) as err: _LOGGER.error("Conn err fetch info: %s",err); raise ConnectError(f"Conn err fetch info: {err}") from err
        except Exception as e: _LOGGER.exception("Unexpected err fetch info: %s",e); raise ConnectError(f"Unexpected err fetch info: {e}") from e
    async def _async_ping(self):
        try: pr=await self._async_rest_get_text(ApiUrl.PING); self.server_ping=pr
            if self.server_type==ServerType.UNKNOWN:
                if pr=="Jellyfin Server": self.server_type=ServerType.JELLYFIN
                elif pr=="Emby Server": self.server_type=ServerType.EMBY
            return pr
        except Exception as e: _LOGGER.debug("Ping fail: %s",e); return None
    def _auth_update(self):
        self._default_params={}; self._default_headers={"Content-Type":"application/json","Accept":"application/json, text/plain, */*"}; sws="wss" if self._use_ssl else "ws"; bws_url=self.server_url.replace("http","ws",1); self._ws_url=""
        if not self.api_key: _LOGGER.debug("No API key for auth."); self._ws_url=f"{bws_url}/socket"; return
        st=self.server_type if self.server_type!=ServerType.UNKNOWN else ServerType.JELLYFIN; did=self.device_id or "ha-mediabrowser-unknown"
        if st==ServerType.JELLYFIN:
            self._ws_url=f"{bws_url}/socket?api_key={self.api_key}&deviceId={did}"
            ah=f'MediaBrowser Client="{self.client_name}", Device="{self.device_name}", DeviceId="{did}", Version="{self.device_version}", Token="{self.api_key}"'
            self._default_headers["X-Emby-Authorization"]=ah; self._default_headers.pop("X-Emby-Token",None)
        else: # Emby style
            self._ws_url=f"{bws_url}/embywebsocket?api_key={self.api_key}&deviceId={did}"
            self._default_headers["X-Emby-Token"]=self.api_key; self._default_headers["X-Emby-Device-Id"]=did; self._default_headers["X-Emby-Device"]=self.device_name; self._default_headers["X-Emby-Client"]=self.client_name; self._default_headers.pop("X-Emby-Authorization",None)
        _LOGGER.debug("Auth update %s. WS: %s",st,self._ws_url); _LOGGER.debug("Headers: %s",self._default_headers)
    # --- REST Helpers ---
    async def _async_rest_post_response(self, url, data=None, params=None):
        tu=self.server_url+url; rp=self._default_params.copy(); if params: rp.update(params); _LOGGER.debug("POST %s | P: %s | H: %s | B: %s",tu,rp or None,self._default_headers,str(data)[:200]); sslv=None if self._use_ssl else False
        async with async_timeout.timeout(self.timeout): r=await self._rest.post(tu,json=data,params=rp or None,headers=self._default_headers,ssl=sslv,raise_for_status=False)
        if not r.ok: _LOGGER.warning("POST %s fail %d",tu,r.status); r.raise_for_status(); return r
    async def _async_rest_get_response(self, url, params=None):
        tu=self.server_url+url; rp=self._default_params.copy(); if params: rp.update(params); _LOGGER.debug("GET %s | P: %s | H: %s",tu,rp or None,self._default_headers); sslv=None if self._use_ssl else False
        async with async_timeout.timeout(self.timeout): r=await self._rest.get(tu,params=rp or None,headers=self._default_headers,ssl=sslv,raise_for_status=False)
        if not r.ok: _LOGGER.warning("GET %s fail %d",tu,r.status); r.raise_for_status(); return r
    async def _async_rest_post_get_json(self, url, data=None, params=None): r=await self._async_rest_post_response(url,data,params); ct=r.headers.get('Content-Type',''); if 'json' in ct: try: return await r.json() or {}; except Exception as e: _LOGGER.warning("Fail JSON POST %s: %s",url,e); return {}; else: _LOGGER.warning("Non-JSON POST %s (%s)",url,ct); return {}
    async def _async_rest_post_get_text(self, url, data=None, params=None): r=await self._async_rest_post_response(url,data,params); return await r.text()
    async def _async_rest_get_json(self, url, params=None): r=await self._async_rest_get_response(url,params); ct=r.headers.get('Content-Type',''); if 'json' in ct: if r.content_length==0: return {}; try: return await r.json() or {}; except Exception as e: _LOGGER.warning("Fail JSON GET %s: %s",url,e); return None; else: _LOGGER.warning("Non-JSON GET %s (%s)",url,ct); return None
    async def _async_rest_get_text(self, url, params=None): r=await self._async_rest_get_response(url,params); return await r.text()
    # --- Websocket Methods ---
    async def _async_ws_connect(self):
        if not self._ws_url: raise ConnectionError("WS URL missing"); _LOGGER.debug("Connect WS: %s",self._ws_url); self._abort=False; sslv=None if self._use_ssl else False
        async with async_timeout.timeout(self.timeout): self._ws=await self._rest.ws_connect(self._ws_url,headers=self._default_headers,ssl=sslv)
        await self._ws.send_json({"MessageType":"SessionsStart","Data":"0,1500"});
        if self.send_activity_events: await self._ws.send_json({"MessageType":"ActivityLogEntryStart","Data":"0,1000"})
        if self.send_task_events: await self._ws.send_json({"MessageType":"ScheduledTasksInfoStart","Data":"0,1500"})
        _LOGGER.debug("WS connected and initial messages sent.")
    async def _async_ws_disconnect(self): if self._ws is not None and not self._ws.closed: await self._ws.close(); self._ws=None
    async def _async_ws_loop(self):
        delay=1
        while not self._abort:
            try: await self._async_needs_server_verification(); await self._async_needs_authentication(); await self._async_needs_sessions(); await self._async_ws_connect()
            except ClientMismatchError as err: _LOGGER.error("Mismatch stop: %s",err); self._set_available(False); break
            except ConfigEntryAuthFailed as err: _LOGGER.error("Auth fail stop: %s",err); self._set_available(False); break
            except (aiohttp.ClientConnectionError,aiohttp.WSServerHandshakeError) as err: _LOGGER.warning("WS conn fail: %s",err)
            except aiohttp.ClientResponseError as err: _LOGGER.warning("WS req err: %s (%s)",err.status,err.message); if err.status in (401,403): _LOGGER.error("WS unauth stop."); self._set_available(False); break
            except (asyncio.TimeoutError,TimeoutError) as err: _LOGGER.warning("Timeout WS conn: %s",err)
            except asyncio.CancelledError: _LOGGER.debug("WS loop cancel setup."); break
            except Exception as err: _LOGGER.exception("Unexpected WS setup err: %s",err)
            else: # Connection successful
                self._set_available(True); delay=1; _LOGGER.info("WS connection established to %s",self.name)
                _LOGGER.debug("WS: Entering message receive loop.")
                while not self._abort and self._ws is not None and not self._ws.closed:
                    try:
                        _LOGGER.debug("WS: Waiting for message...")
                        msg=await self._ws.receive(timeout=KEEP_ALIVE_TIMEOUT*1.5); _LOGGER.debug("WS: Received message type: %s",msg.type)
                        if msg.type==aiohttp.WSMsgType.TEXT: _LOGGER.debug("WS: TEXT msg: %s",msg.data[:200]+("..."if len(msg.data)>200 else "")); asyncio.create_task(self._handle_message(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.CLOSING,aiohttp.WSMsgType.CLOSED): _LOGGER.warning("WS: Conn closed by server (Type: %s, Data: %s). Breaking inner loop.",msg.type,msg.data); break
                        elif msg.type==aiohttp.WSMsgType.ERROR: _LOGGER.error("WS: Error received: %s. Breaking inner loop.",self._ws.exception()); break
                        elif msg.type==aiohttp.WSMsgType.PING: _LOGGER.debug("WS: PING received, sending PONG."); await self._ws.pong()
                        elif msg.type==aiohttp.WSMsgType.PONG: _LOGGER.debug("WS: PONG received.")
                    except asyncio.TimeoutError: _LOGGER.debug("WS: Receive timeout."); if self._keep_alive_timeout is None: now=datetime.utcnow(); if (now-self._last_keep_alive).total_seconds()>=KEEP_ALIVE_TIMEOUT: self._send_keep_alive()
                    except asyncio.CancelledError: _LOGGER.debug("WS: Loop task cancelled during message handling. Breaking inner loop."); self._abort=True; break
                    except Exception as err: _LOGGER.exception("WS: Error handling message: %s. Breaking inner loop.",err); break
                _LOGGER.debug("WS: Exited inner message receive loop.")
                if not self._abort: _LOGGER.warning("WS: Disconnected unexpectedly. Reconnecting...")
                self._set_available(False); await self._async_ws_disconnect()
            if not self._abort: _LOGGER.debug("Attempting WS reconnect in %d seconds",delay); await asyncio.sleep(delay); delay=min(delay*2,60)
        _LOGGER.debug("Websocket loop finished.")
    # --- Listener Calling Methods ---
    async def _call_availability_listeners(self, available):
        listeners_to_call=list(self._availability_listeners); _LOGGER.debug("Calling %d availability listeners: %s",len(listeners_to_call),available)
        for listener in listeners_to_call: try: await listener(available); except Exception as err: _LOGGER.error("Error calling avail listener %s: %s",getattr(listener,'__name__',repr(listener)),err,exc_info=True)
    async def _call_sessions_listeners(self, sessions):
        listeners_to_call=list(self._sessions_listeners); _LOGGER.debug("Calling %d sessions listeners.",len(listeners_to_call))
        for listener in listeners_to_call: try: await listener(sessions); except Exception as err: _LOGGER.error("Error calling sessions listener %s: %s",getattr(listener,'__name__',repr(listener)),err,exc_info=True)
    async def _call_session_changed_listeners(self, added, removed, updated):
        listeners_to_call=list(self._session_changed_listeners); events=([(None,s) for s in added]+[(s,None) for s in removed]+updated); _LOGGER.debug("Calling %d session changed listeners for %d events.",len(listeners_to_call),len(events))
        for event in events: for listener in listeners_to_call: try: await listener(event[0],event[1]); except Exception as err: _LOGGER.error("Error calling session changed listener %s: %s",getattr(listener,'__name__',repr(listener)),err,exc_info=True)
    async def _call_library_listeners(self, library_ids):
        listeners_dict_copy=self._library_listeners.copy(); infos_copy=self._library_infos.copy(); listeners_to_call_map={}
        for key,listeners in listeners_dict_copy.items(): lid,_,_=key; if lid in library_ids or lid==KEY_ALL: listeners_to_call_map[key]=list(listeners)
        _LOGGER.debug("Calling library listeners for keys: %s",list(listeners_to_call_map.keys()))
        for key,listeners in listeners_to_call_map.items():
            info_to_pass=infos_copy.get(key);
            if info_to_pass is None: _LOGGER.warning("No library info for key %s",key); continue
            for listener in listeners: try: await listener(info_to_pass); except Exception as err: _LOGGER.error("Error calling library listener %s key %s: %s",getattr(listener,'__name__',repr(listener)),key,err,exc_info=True)
    async def _call_websocket_listeners(self, message_type, data):
        listeners_to_call=list(self._websocket_listeners)
        for listener in listeners_to_call: try: await listener(message_type,data); except Exception as err: _LOGGER.error("Error calling WS listener %s type %s: %s",getattr(listener,'__name__',repr(listener)),message_type,err,exc_info=True)
    async def _call_websocket_listeners_for_list(self, messages):
        listeners_to_call=list(self._websocket_listeners);
        if not listeners_to_call: return; _LOGGER.debug("Calling %d WS listeners for %d messages.",len(listeners_to_call),len(messages))
        for message_type,data in messages: for listener in listeners_to_call: try: await listener(message_type,data); except Exception as err: _LOGGER.error("Error calling WS listener %s type %s: %s",getattr(listener,'__name__',repr(listener)),message_type,err,exc_info=True)
    # --- Other Internal Methods ---
    def _set_available(self, availability):
        if self.is_available==availability: return; _LOGGER.info("%s server became %s",self.name,"available" if availability else "unavailable"); self.is_available=availability
        if self._availability_listeners: if self._availability_task and not self._availability_task.done(): self._availability_task.cancel(); self._availability_task=asyncio.create_task(self._call_availability_listeners(availability))
    async def _handle_message(self, message_str):
        try: msg=json.loads(message_str); except json.JSONDecodeError: _LOGGER.warning("Invalid JSON WS: %s",message_str[:200]); return
        msg_type=msg.get("MessageType"); data=msg.get("Data")
        if not msg_type: _LOGGER.debug("WS no type: %s",msg); return
        call_listeners=self.send_other_events
        if msg_type==WebsocketMessage.SESSIONS: if isinstance(data,list): asyncio.create_task(self._handle_sessions_message(data)); else: _LOGGER.warning("SESSIONS !list: %s",type(data)); call_listeners=False
        elif msg_type==WebsocketMessage.KEEP_ALIVE: self._last_keep_alive=datetime.utcnow(); call_listeners=False
        elif msg_type==WebsocketMessage.FORCE_KEEP_ALIVE: try: nt=max(5.0,(int(data)/1000.0)*0.8); self._keep_alive_timeout=nt; _LOGGER.debug("Dyn keep-alive: %.1fs",nt); except Exception as e: _LOGGER.warning("Bad FORCE_KA (%s): %s",data,e); self._keep_alive_timeout=KEEP_ALIVE_TIMEOUT; call_listeners=False
        elif msg_type==WebsocketMessage.LIBRARY_CHANGED: if self._library_listeners: if isinstance(data,dict): asyncio.create_task(self._handle_library_changed_message(data)); data=get_library_changed_event_data(data); else: _LOGGER.warning("LIB_CHG !dict: %s",type(data)); data={}; else: call_listeners=False
        elif msg_type==WebsocketMessage.ACTIVITY_LOG_ENTRY: asyncio.create_task(self._handle_activity_log_message()); call_listeners=False
        elif msg_type==WebsocketMessage.SCHEDULED_TASK_INFO: call_listeners=self.send_task_events
        elif msg_type==WebsocketMessage.USER_DATA_CHANGED: if isinstance(data,dict): data=get_user_data_changed_event_data(data); else: _LOGGER.warning("USER_DATA !dict: %s",type(data)); data={}
        if call_listeners and self._websocket_listeners: event_data={"server_id":self.server_id,snake_case(msg_type):data or {}}; asyncio.create_task(self._call_websocket_listeners(snake_case(msg_type),event_data))
        if self._keep_alive_timeout is None: now=datetime.utcnow(); if (now-self._last_keep_alive).total_seconds()>=KEEP_ALIVE_TIMEOUT: self._send_keep_alive()
    async def _handle_sessions_message(self, sessions):
        _LOGGER.debug("Proc sess msg %d",len(sessions)); nr={s["Id"]:get_session_event_data(deepcopy(s)) for s in sessions if "Id" in s}; np={s["Id"]:s for s in self._preprocess_sessions(deepcopy(sessions)) if "Id" in s}; ar,rr,ur=self._get_changed_sessions(self._raw_sessions,nr); a,r,u=self._get_changed_sessions(self._sessions,np); self._raw_sessions=nr; self._sessions=np
        if a or r or u: if self._sessions_listeners: asyncio.create_task(self._call_sessions_listeners(list(np.values()))); if self._session_changed_listeners: asyncio.create_task(self._call_session_changed_listeners(a,r,u))
        if self.send_session_events and self._websocket_listeners and (ar or rr or ur): ms=[("session_changed",{"old":None,"new":s}) for s in ar]+[("session_changed",{"old":s,"new":None}) for s in rr]+[("session_changed",{"old":o,"new":n}) for o,n in ur]; asyncio.create_task(self._call_websocket_listeners_for_list(ms))
    async def _handle_activity_log_message(self):
        if not self.send_activity_events or not self._websocket_listeners: return; _LOGGER.debug("Handle activity log"); p={}; ls=self._last_activity_log_entry; if ls: p[Query.MIN_DATE]=ls; else: p[Query.LIMIT]=1
        try: rsp=await self._async_get_activity_log_entries(p); except Exception as e: _LOGGER.error("Fail fetch activity: %s",e); return
        es=rsp.get(Response.ITEMS); nlt=None; if not es: _LOGGER.debug("No new activity."); return
        try: nlt=max((e.get(Item.DATE) for e in es if e.get(Item.DATE)),default=ls); except ValueError: nlt=ls
        se=sorted((e for e in es if e.get(Item.DATE)),key=lambda x:x.get(Item.DATE,"")); tf=[]; if ls: tf=[e for e in se if e.get(Item.DATE,"")>ls]; else: tf=se
        if tf and nlt and nlt>(ls or ""): self._last_activity_log_entry=nlt
        if tf: _LOGGER.debug("Fire %d activity evts.",len(tf)); ms=[("activity_log_entry",e|{"server_id":self.server_id}) for e in tf]; asyncio.create_task(self._call_websocket_listeners_for_list(ms)); else: _LOGGER.debug("No activity to fire.")
    async def _handle_library_changed_message(self, data, force_updates=False):
        if not self._library_listeners: return; _LOGGER.debug("Handle lib change (Force:%s): %s",force_updates,data.get(Item.COLLECTION_FOLDERS)); fs=data.get(Item.COLLECTION_FOLDERS,[]); ak=set()
        if not fs and not force_updates: ak={k for k in self._library_listeners if k[0]==KEY_ALL}; if not ak: return
        else: ak={k for k in self._library_listeners if force_updates or k[0]==KEY_ALL or k[0] in fs}
        if not ak: _LOGGER.debug("No sensors match folders."); return
        _LOGGER.debug("Update lib sensors: %s",ak); oi=self._library_infos.copy(); ni=self._library_infos.copy(); fts={k:asyncio.create_task(self._fetch_library_info(*k)) for k in ak}; await asyncio.gather(*fts.values(),return_exceptions=True); uk=set()
        for k,t in fts.items(): try: r=t.result(); if r is not None: ni[k]=r; if force_updates or oi.get(k)!=r: uk.add(k); else: ni[k]={Response.ITEMS:[],Response.TOTAL_RECORD_COUNT:0}; if force_updates or oi.get(k)!=ni[k]: uk.add(k); except Exception as e: _LOGGER.error("Fail fetch lib %s: %s",k,e); ni.pop(k,None)
        self._library_infos=ni
        if uk: lm={k:list(self._library_listeners.get(k,[])) for k in uk if k in self._library_listeners}; for k,ls in lm.items(): ip=ni.get(k); if ip is None: continue; for l in ls: try: asyncio.create_task(l(ip)); except Exception as e: _LOGGER.error("Err sched lib listener %s: %s",k,e,exc_info=True)
    async def _fetch_library_info(self, library_id, user_id, item_type):
        k=(library_id,user_id,item_type); try: p=LATEST_QUERY_PARAMS.copy(); p[Query.INCLUDE_ITEM_TYPES]=item_type; if library_id!=KEY_ALL: p[Query.PARENT_ID]=library_id; if user_id!=KEY_ALL: if not user_id: _LOGGER.warning("Bad UserID %s key %s",user_id,k); return None; return await self.async_get_user_items(user_id,p); else: return await self.async_get_items(p); except Exception as e: _LOGGER.error("Exc fetch lib %s: %s",k,e); raise
    def force_library_change(self, library_id): _LOGGER.debug("Force lib update ID: %s",library_id); asyncio.create_task(self._handle_library_changed_message({Item.COLLECTION_FOLDERS:[library_id]},force_updates=True))
    def _get_changed_sessions(self,o,n): a=[s for k,s in n.items() if k not in o]; r=[s for k,s in o.items() if k not in n]; u=[(o[k],s) for k,s in n.items() if k in o and o[k]!=s]; return a,r,u
    def _preprocess_sessions(self, sessions):
        return [s for s in sessions if s.get(Item.DEVICE_ID)!=self.device_id and s.get(Item.CLIENT)!=self.client_name and (not self.ignore_web_players or s.get(Item.CLIENT) not in WEB_PLAYERS) and (not self.ignore_dlna_players or s.get(Item.CLIENT) not in DLNA_PLAYERS) and (not self.ignore_mobile_players or s.get(Item.CLIENT) not in MOBILE_PLAYERS) and (not self.ignore_app_players or s.get(Item.CLIENT) not in APP_PLAYERS)]
    def _send_keep_alive(self): if not self._abort and self._ws is not None and not self._ws.closed: _LOGGER.debug("Send KA %s",self.name); try: asyncio.create_task(self._ws.send_json({"MessageType":"KeepAlive"})); self._last_keep_alive=datetime.utcnow(); except Exception as e: _LOGGER.warning("Fail send KA: %s",e)
    async def async_stop(self): _LOGGER.debug("Stop hub %s",self.name); self._abort=True; if self._ws_loop is not None and not self._ws_loop.done(): self._ws_loop.cancel(); try: await self._ws_loop; except asyncio.CancelledError: _LOGGER.debug("WS loop cancel stop."); await self._async_ws_disconnect(); self._ws_loop=None; if self._rest and not self._rest.closed: await self._rest.close(); _LOGGER.debug("aiohttp closed."); _LOGGER.debug("Hub stop %s complete.",self.name)

class ClientMismatchError(ConnectError):
    """Error to indicate that server unique id mismatch."""

# --- End of MediaBrowserHub class ---