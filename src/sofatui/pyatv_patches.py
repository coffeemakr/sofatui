"""Runtime fixes for pyatv 0.18.0 so it works with current tvOS.

pyatv 0.18.0 has a few gaps on recent Apple TVs, which are patched in here when
sofatui starts (see apply):

* AirPlay 2 passwords: the device answers SETUP with a digest challenge that pyatv
  only handles for AirPlay 1.
* Mute: pyatv has no command for the remote's mute key, so one is added.
* HLS playlists: the device does not fetch them itself but asks the sender to, which
  lets the sender use the cookies and headers a site expects. pyatv ignores the request.
* Playing a URL: the device ignores the old "POST /play". Media is instead queued
  over a media control stream with "POST /command", and playback state is reported
  on the event channel. This follows https://github.com/postlund/pyatv/pull/2846.

The patches replace pyatv internals, so the dependency is pinned to that version.
"""

import asyncio
from importlib.metadata import version
import logging
import plistlib
from typing import Any, Dict, Optional, Tuple
import urllib.error
import urllib.request
from uuid import uuid4

from pyatv import exceptions
from pyatv.auth.hap_channel import setup_channel
from pyatv.const import InputAction, Protocol
from pyatv.protocols import mrp
from pyatv.protocols.airplay import player
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.protocols.airplay.channels import EventChannel
from pyatv.protocols.airplay.utils import decode_plist_body
from pyatv.protocols.raop.protocols import airplayv2
from pyatv.support.http import decode_bplist_from_body
from pyatv.support.rtsp import DigestInfo, RtspSession

_LOGGER = logging.getLogger(__name__)

PYATV_VERSION = "0.18.0"
MEDIA_CONTROL_STREAM_TYPE = 130  # stream used to play media from a URL
MUTE_KEY = (12, 0xE2)  # HID consumer page, "Mute": the mute button on the remote
START_TIMEOUT = 30  # seconds to wait for playback to start

FETCH_TIMEOUT = 20  # seconds for a playlist the device asked us to fetch

_passwords: Dict[str, str] = {}  # device address -> AirPlay password
_fetch_headers: Dict[str, str] = {}  # sent when fetching playlists for the device
_playing_listener = None  # called once the device reports that playback started
_applied = False


def set_password(address: str, password: str) -> None:
    """Register the AirPlay password to use for a device."""
    _passwords[address] = password


def set_playing_listener(listener) -> None:
    """Set a function to call when the device has actually started playing."""
    global _playing_listener  # pylint: disable=global-statement
    _playing_listener = listener


def set_fetch_headers(headers: Dict[str, str]) -> None:
    """Set the HTTP headers to use when the device asks us to fetch a playlist."""
    _fetch_headers.clear()
    _fetch_headers.update(headers)


def _fetch(url: str, headers: Dict[str, str]) -> Tuple[int, str, bytes]:
    """Fetch a URL; returns status, content type and body."""
    # No compressed transfer: the body is handed on as it is
    headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            return response.status, content_type, response.read()
    except urllib.error.HTTPError as ex:
        return ex.code, "", b""
    except OSError:
        return 504, "", b""


async def _setup(self, headers=None, body=None):
    """Send SETUP, answering a digest challenge if a password is registered."""
    password = _passwords.get(self.connection.remote_ip)
    authenticate = password is not None and self.digest_info is None

    response = await self.exchange(
        "SETUP", headers=headers, body=body, allow_error=authenticate
    )
    challenge = response.headers.get("www-authenticate")
    if response.code == 401 and challenge and authenticate:
        _, realm, _, nonce, _ = challenge.split('"')
        self.digest_info = DigestInfo("pyatv", realm, password, nonce)
        response = await self.exchange("SETUP", headers=headers, body=body)
    return response


class _EventChannel(EventChannel):
    """Event channel that passes the commands of the device on to a listener."""

    command_listener = None

    def _handle_command(self, request) -> None:
        # Commands from the receiver are wrapped as a plist inside a plist
        outer = decode_plist_body(request.body) if request.body else None
        if not isinstance(outer, dict):
            return

        data = outer.get("params", {}).get("data")
        command = decode_plist_body(data) if data else None
        _LOGGER.debug("Command on event channel: %s", command or outer)

        if isinstance(command, dict) and self.command_listener is not None:
            self.command_listener(command)

    def handle_received(self) -> None:
        # Look at the complete requests first; the base class then answers them
        data = self.buffer
        while data:
            try:
                request, _, data = self.parse_request(data)
            except Exception:  # pylint: disable=broad-except
                break
            if request is None:
                break
            self._handle_command(request)
        super().handle_received()


class _AirPlayV2(airplayv2.AirPlayV2):
    """AirPlay 2 stream protocol that plays URLs the way current tvOS expects."""

    def __init__(self, context, rtsp) -> None:
        super().__init__(context, rtsp)
        self._session_id = str(uuid4()).upper()
        self._command_headers: Dict[str, object] = {
            "User-Agent": "AirPlay/870.14.1",
            "Content-Type": "application/x-apple-binary-plist",
            "X-Apple-ProtocolVersion": "1",
            "X-Apple-Session-ID": self._session_id,
            "X-Apple-StreamID": "1",
            "CSeq": "1",
        }
        self._playback_state: Optional[Dict[str, Any]] = None
        self._tasks: set = set()

    def _command_received(self, command: Dict[str, Any]) -> None:
        if command.get("type") == "playbackState":
            self._playback_state = command
        elif command.get("type") == "unhandledURL" and command.get("kind") == "request":
            task = asyncio.ensure_future(self._answer_url_request(command))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _answer_url_request(self, command: Dict[str, Any]) -> None:
        """Fetch a playlist (or key) for the device and send it back."""
        request = command["request"]
        url = request["FCUP_Response_URL"]
        headers = _fetch_headers or request.get("FCUP_Response_Headers", {})
        status, content_type, data = await asyncio.to_thread(_fetch, url, headers)
        _LOGGER.debug("Fetched %s for the device: %d, %d bytes", url, status, len(data))
        await self._send_command(
            {
                "kind": "response",
                "type": "unhandledURL",
                "messageID": command.get("messageID"),
                "response": {
                    "FCUP_Response_RequestID": request.get("FCUP_Response_RequestID"),
                    "FCUP_Response_URL": url,
                    "FCUP_Response_StatusCode": status,
                    "FCUP_Response_Data": data,
                    "FCUP_Response_Headers": {"Content-Type": content_type},
                },
            }
        )

    async def _setup_base(self, timing_server_port: int) -> None:
        self._verifier = await verify_connection(
            self.context.credentials, self.rtsp.connection
        )

        setup_resp = await self.rtsp.setup(
            body={
                "deviceID": "AA:BB:CC:DD:EE:FF",
                "sessionUUID": self._session_id,
                "sessionCorrelationUUID": str(uuid4()).upper(),
                "timingPort": timing_server_port,
                "timingProtocol": "NTP",
                "isMultiSelectAirPlay": True,
                "groupContainsGroupLeader": False,
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "model": "iPhone14,3",
                "name": "pyatv",
                "osBuildVersion": "20F66",
                "osName": "iPhone OS",
                "osVersion": "16.5",
                "senderSupportsRelay": False,
                "sourceVersion": "690.7.1",
                "statsCollectionEnabled": False,
            }
        )
        resp = decode_bplist_from_body(setup_resp)
        _LOGGER.debug("Setup response body: %s", resp)

        # The event channel may come up a moment after the device announced its port
        retries = 5
        transport = None
        while transport is None:
            try:
                transport, channel = await setup_channel(
                    _EventChannel,
                    self._verifier,
                    self.rtsp.connection.remote_ip,
                    resp.get("eventPort", 0),
                    airplayv2.EVENTS_SALT,
                    airplayv2.EVENTS_READ_INFO,
                    airplayv2.EVENTS_WRITE_INFO,
                )
            except OSError:
                retries -= 1
                if retries == 0:
                    raise
                _LOGGER.debug("Connect failed, retrying")
                await asyncio.sleep(1.0)

        self.event_channel = transport
        channel.command_listener = self._command_received

    async def _send_command(self, command: Dict[str, Any]):
        body = {
            "params": {
                "data": plistlib.dumps(
                    command, fmt=plistlib.FMT_BINARY, sort_keys=False
                )
            }
        }
        return await self.rtsp.connection.post(
            "/command",
            headers=self._command_headers,
            body=plistlib.dumps(body, fmt=plistlib.FMT_BINARY),
            allow_error=True,
        )

    async def _setup_media_control_stream(self) -> None:
        setup_resp = await self.rtsp.setup(
            body={
                "streams": [
                    {
                        "clientUUID": str(uuid4()).upper(),
                        "clientTypeUUID": "A6B27562-B43A-4F2D-B75F-82391E250194",
                        "channelID": "AA:BB:CC:DD:EE:FF-RCS-1",
                        "controlType": 1,
                        "type": MEDIA_CONTROL_STREAM_TYPE,
                    }
                ]
            }
        )
        resp = decode_bplist_from_body(setup_resp)
        _LOGGER.debug("Setup media control stream response: %s", resp)
        self._command_headers["X-Apple-StreamID"] = resp["streams"][0]["streamID"]

    async def play_url(self, timing_server_port: int, url: str, position: float = 0.0):
        """Play media from a URL."""
        await self._setup_base(timing_server_port)
        await self.start_feedback()
        await self.rtsp.info()
        await self.rtsp.record()
        await self._setup_media_control_stream()

        item: Dict[str, Any] = {
            "uuid": self.uuid.upper(),
            "mediaType": "file",
            "Content-Location": url,
        }
        if position:
            item["Start-Position-Seconds"] = position

        # Actually start the stream
        resp = await self._send_command({"type": "insertPlayQueueItem", "item": item})

        # Playback starts paused, so rate must be set to 100% for it to start
        await self._send_command(
            {
                "type": "setProperty",
                "value": True,
                "property": "isInterestedInDateRange",
                "item": {"uuid": item["uuid"]},
            }
        )
        await self._send_command(
            {"type": "setProperty", "value": 1, "property": "actionAtItemEnd"}
        )
        await self._send_command({"type": "setRate", "rate": 1.0})
        return resp

    def playback_state(self) -> Optional[str]:
        """Return the last playback state reported on the event channel."""
        state = self._playback_state
        if state is None:
            return None
        if "params" in state:
            return state["params"].get("playbackState")
        return state.get("name")


async def _wait_for_playback_state(self, playback_state) -> None:
    waited = 0
    video_started = False

    while True:
        if self.rtsp.connection.transport is None:
            _LOGGER.debug("Connection was lost, assuming video playback stopped")
            break

        state = playback_state()
        if state == "playing":
            if not video_started and _playing_listener:
                _playing_listener()
            video_started = True
        elif state == "stopped" and video_started:
            _LOGGER.debug("media playback ended")
            break
        elif not video_started and waited >= START_TIMEOUT:
            raise exceptions.PlaybackError(
                f"media did not start playing (state: {state})"
            )

        waited += 1
        await asyncio.sleep(1)


async def press_mute(atv) -> None:
    """Press the mute key, which pyatv has no command for."""
    control = atv.remote_control.get(Protocol.MRP)
    if control is None:
        raise exceptions.NotSupportedError("mute needs the remote control connection")
    # pylint: disable-next=protected-access
    await mrp._send_hid_key(control.protocol, "mute", InputAction.SingleTap)


def apply() -> None:
    """Patch pyatv; safe to call more than once."""
    global _applied  # pylint: disable=global-statement
    if _applied:
        return
    if version("pyatv") != PYATV_VERSION:
        raise RuntimeError(
            f"sofatui patches pyatv {PYATV_VERSION}, but {version('pyatv')} is installed"
        )

    RtspSession.setup = _setup
    mrp._KEY_LOOKUP["mute"] = MUTE_KEY  # pylint: disable=protected-access
    airplayv2.AirPlayV2 = _AirPlayV2

    # AirPlay 2 reports playback state on the event channel instead of /playback-info
    poll_playback_info = player.AirPlayPlayer._wait_for_media_to_end

    async def _wait_for_media_to_end(self) -> None:
        playback_state = getattr(self.stream_protocol, "playback_state", None)
        if playback_state is None:
            await poll_playback_info(self)
        else:
            await _wait_for_playback_state(self, playback_state)

    player.AirPlayPlayer._wait_for_media_to_end = _wait_for_media_to_end
    _applied = True
