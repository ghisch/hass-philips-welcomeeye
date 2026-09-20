"""Talking to the WelcomeEye, and serving its video to Home Assistant.

Everything here is blocking socket work and must be called from an executor, never
from the event loop. The one exception is the local TS server, which owns its own
threads.

Two device behaviours shape this whole module:

* The device encodes **one channel at a time** and the incumbent keeps it. A second
  viewer asking for the other channel gets nothing - and that includes the indoor
  monitor, which is why streaming is off by default and explicitly enabled.
* The gate unlock needs live **video** on the door channel. Audio keeps flowing even
  when the device is serving the other channel, so audio is not proof; and TLV 506
  answers ``result=1`` whether or not the relay fired. Success is only credible when
  door video was seen first.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field

from .const import (CHANNEL_CAM, CHANNEL_DOOR, DEFAULT_STREAM_ENABLED,
                    LOCK_CHANNEL_GATE)
from .glnklib.commands import (AV_CAM, AV_DOOR, TLV_LOCK_REQ_ENC, TLV_PRIVATE_REQ,
                               TLV_START_AV_ENC, lock_body, parse_lock_rsp, priv_body,
                               start_av_body, unwrap_priv_rsp, wrap_priv, wrap_cmd)
from .glnklib.protocol import (CH_CAM, CH_DOOR, RESULT_CODES, UdtSession, find_punch_port,
                               tlv)
from .glnklib.tsmux import TsMuxer

_LOGGER = logging.getLogger(__name__)

CHANNEL_MAP = {CHANNEL_DOOR: (CH_DOOR, AV_DOOR), CHANNEL_CAM: (CH_CAM, AV_CAM)}

V_HDR, V_KEY, V_INTER, A_HDR, A_DATA = 99, 100, 101, 97, 98
VIDEO_TLVS = frozenset({V_KEY, V_INTER})
PRIV_INFO_TLV = 469          # asking this returns the device's version JSON

#: how long to wait for door video before deciding an unlock cannot work
AV_WAIT_SECONDS = 10.0


class WelcomeEyeError(Exception):
    """Something went wrong talking to the device."""


def _media(msg: bytes) -> tuple[str | None, memoryview | None, int | None]:
    """(kind, payload, timestamp_ms) for one reassembled message.

    TLV 99 holds the frame's millisecond timestamp and precedes the payload TLV.
    TLV 100 is a keyframe payload and 101 an inter-frame; their length field reads 1
    and is meaningless, the payload runs to the end of the frame.
    """
    if len(msg) < 12 or struct.unpack_from(">I", msg, 0)[0] + 4 != len(msg):
        return None, None, None
    off, ts = 8, None
    while len(msg) - off >= 4:
        t, l = struct.unpack_from("<HH", msg, off)
        if t in (V_HDR, A_HDR) and l >= 8 and off + 4 + l <= len(msg):
            ts = struct.unpack_from("<I", msg, off + 4 + (8 if t == V_HDR else 4))[0]
        elif t == V_KEY:
            return "key", memoryview(msg)[off + 4:], ts
        elif t == V_INTER:
            return "inter", memoryview(msg)[off + 4:], ts
        elif t == A_DATA:
            return "audio", memoryview(msg)[off + 4:off + 4 + l], ts
        if off + 4 + l > len(msg):
            return None, None, None
        off += 4 + l
    return None, None, None


@dataclass
class StreamStats:
    """What the diagnostic sensors report about streaming."""

    clients: int = 0
    frames: int = 0
    bytes_sent: int = 0
    started_at: float | None = None
    last_error: str | None = None


class WelcomeEyeDevice:
    """One intercom. Owns its credentials, its state and its local TS server."""

    def __init__(self, host: str, gid: str, username: str, password: str,
                 punch_port: int | None = None,
                 lock_channel: int = LOCK_CHANNEL_GATE) -> None:
        self.host = host
        self.gid = gid
        self.username = username
        self._password = password
        #: A fixed port, only for paths where discovery cannot reach the device.
        #: Normally left unset: the port is randomised and rotates often (observed
        #: moving twice in one evening), so it is rediscovered per connection.
        self.fixed_port = punch_port
        self.punch_port: int | None = punch_port
        self.lock_channel = lock_channel
        self.av_wait = AV_WAIT_SECONDS

        self.channel: str = CHANNEL_DOOR
        self.stream_enabled: bool = DEFAULT_STREAM_ENABLED
        self.stats = StreamStats()
        self.info: dict[str, object] = {}
        self.last_unlock: dict[str, object] | None = None

        self._server: _TsServer | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ session
    def _connect_once(self, channel_key: str, port: int | None) -> tuple[UdtSession, dict]:
        chan, _sel = CHANNEL_MAP[channel_key]
        try:
            session = UdtSession(self.host, self.gid, port)
        except RuntimeError as err:
            raise WelcomeEyeError(str(err)) from err
        try:
            res = session.connect(self.username, self._password, chan=chan, raw_pwd=True)
        except OSError as err:
            session.close()
            raise WelcomeEyeError(f"cannot reach {self.host}: {err}") from err
        if not res.get("ok"):
            session.close()
            code = res.get("result")
            if code is not None:
                raise WelcomeEyeError(f"login rejected: {RESULT_CODES.get(code, code)}")
            raise WelcomeEyeError(res.get("error", "no login response"))
        self.punch_port = session.port
        return session, res

    def _session(self, channel_key: str) -> tuple[UdtSession, dict]:
        """Log in on the channel's own mask. Caller must close the session.

        The punch port rotates - observed changing twice in one evening - so a
        cached value goes stale and the handshake simply times out. On failure,
        forget it and let discovery find the current one before giving up.
        """
        port = self.fixed_port or self.punch_port
        try:
            return self._connect_once(channel_key, port)
        except WelcomeEyeError:
            if port is None:
                raise                       # discovery already ran and failed
            _LOGGER.debug("port %s stale, rediscovering", port)
            self.punch_port = None
            return self._connect_once(channel_key, self.fixed_port)

    def _start_av(self, session: UdtSession, channel_key: str) -> None:
        _chan, sel = CHANNEL_MAP[channel_key]
        session.send_tlv(TLV_START_AV_ENC,
                         wrap_cmd(self.gid, start_av_body(session.device_now(), sel),
                                  TLV_START_AV_ENC))

    # -------------------------------------------------------------- operations
    def probe(self) -> dict[str, object]:
        """Log in and collect what the device volunteers. Blocking."""
        session, res = self._session(self.channel)
        try:
            info: dict[str, object] = {
                "device_time": res.get("time"),
                "punch_port": session.port,
                "login_json": (res.get("json") or b"").decode(errors="replace"),
            }
            for _flag, tlv_type, value in session.login_frames:
                if tlv_type == 70 and len(value) > 1:
                    info["model"] = value[1:].split(b"\x00")[0].decode(errors="replace")
                elif tlv_type == 40 and len(value) >= 2:
                    info["protocol_version"] = struct.unpack_from("<H", value, 0)[0]
            info.update(self._query_versions(session))
            self.info = info
            return info
        finally:
            session.close()

    def _query_versions(self, session: UdtSession) -> dict[str, object]:
        """Ask the private channel for the device's version JSON. Best effort."""
        try:
            body = priv_body(session.device_now(), tlv(PRIV_INFO_TLV, b"\x00" * 4))
            session.send_tlv(TLV_PRIVATE_REQ, wrap_priv(self.gid, body))
            deadline = time.time() + 4.0
            while time.time() < deadline:
                for _flag, tlv_type, value in session.recv(1.0):
                    if tlv_type != 510:
                        continue
                    _ts, items = unwrap_priv_rsp(self.gid, value)
                    for inner_type, raw in items:
                        text = raw.split(b"\x00")[0].decode(errors="replace")
                        if text.startswith("{"):
                            return {"version_json": text}
        except Exception as err:  # best effort only, never fail a probe on this
            _LOGGER.debug("version query failed: %s", err)
        return {}

    def unlock(self) -> dict[str, object]:
        """Open the gate. Blocking, and it physically moves the gate.

        Refuses unless door video is actually flowing: without it the device
        answers result=1 and does nothing, which would otherwise be reported as
        success.
        """
        session, _res = self._session(CHANNEL_DOOR)
        try:
            self._start_av(session, CHANNEL_DOOR)
            deadline, live = time.time() + self.av_wait, False
            while time.time() < deadline and not live:
                for _flag, tlv_type, _value in session.recv(1.0):
                    if tlv_type in (V_KEY, V_INTER, 100):
                        live = True
                        break
            if not live:
                raise WelcomeEyeError(
                    "no video on the door channel, so the unlock would be ignored. "
                    "The device serves one channel at a time and the incumbent keeps "
                    "it - something else is probably viewing the other camera.")

            body = lock_body(self._password, channel=self.lock_channel, action=1,
                             delay=0, dev_time=session.device_now())
            session.send_tlv(TLV_LOCK_REQ_ENC,
                             wrap_cmd(self.gid, body, TLV_LOCK_REQ_ENC))
            result: dict[str, object] = {"acknowledged": False, "at": time.time()}
            deadline = time.time() + 8.0
            while time.time() < deadline and not result["acknowledged"]:
                for _flag, tlv_type, value in session.recv(1.0):
                    if tlv_type == 506:
                        rsp = parse_lock_rsp(self.gid, value)
                        result.update(acknowledged=True, result=rsp["result"],
                                      extra=rsp["extra"])
                        break
            self.last_unlock = result
            return result
        finally:
            session.close()

    # ------------------------------------------------------------------ stream
    def stream_url(self) -> str | None:
        """ffmpeg-openable URL for the local TS server, starting it if needed."""
        if not self.stream_enabled:
            return None
        with self._lock:
            if self._server is None:
                self._server = _TsServer(self)
                self._server.start()
            return f"tcp://127.0.0.1:{self._server.port}"

    def stop_stream_server(self) -> None:
        with self._lock:
            if self._server is not None:
                self._server.stop()
                self._server = None

    def set_stream_enabled(self, enabled: bool) -> None:
        self.stream_enabled = enabled
        if not enabled:
            self.stop_stream_server()

    def set_channel(self, channel: str) -> None:
        if channel not in CHANNEL_MAP:
            raise ValueError(channel)
        if channel == self.channel:
            return
        self.channel = channel
        # drop the server so viewers reconnect onto the new channel
        self.stop_stream_server()

    def pump_to(self, write, should_stop) -> None:
        """Stream MPEG-TS to `write` until `should_stop()`. Runs on its own thread."""
        channel_key = self.channel
        session, _res = self._session(channel_key)
        try:
            self._start_av(session, channel_key)
            mux = TsMuxer(write)
            started, ts0 = False, None
            self.stats.started_at = time.time()
            for msg in session.messages(1e9):
                if should_stop():
                    break
                kind, payload, ts_ms = _media(msg)
                if kind not in ("key", "inter"):
                    continue
                if not started:
                    if kind != "key":
                        continue          # never hand a decoder a mid-GOP start
                    started = True
                if ts0 is None:
                    ts0 = ts_ms if ts_ms is not None else 0
                pts = ((ts_ms - ts0) if ts_ms is not None else 0) * 90
                mux.write_frame(payload, pts + 90000, keyframe=(kind == "key"))
                self.stats.frames += 1
                self.stats.bytes_sent += len(payload)
        finally:
            session.close()


class _Handler(socketserver.BaseRequestHandler):
    """One viewer. Opens its own device session and streams until it disconnects."""

    def handle(self) -> None:  # noqa: D102
        device: WelcomeEyeDevice = self.server.device  # type: ignore[attr-defined]
        if not device.stream_enabled:
            return
        stop = threading.Event()
        device.stats.clients += 1

        def write(data: bytes) -> None:
            self.request.sendall(data)

        try:
            device.pump_to(write, stop.is_set)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                       # viewer went away, which is the normal exit
        except Exception as err:       # noqa: BLE001 - surfaced as a diagnostic
            device.stats.last_error = str(err)
            _LOGGER.warning("stream failed: %s", err)
        finally:
            stop.set()
            device.stats.clients = max(0, device.stats.clients - 1)


class _TsServer(socketserver.ThreadingTCPServer):
    """Loopback-only MPEG-TS server. Bound to 127.0.0.1 so nothing off-host can
    reach an unauthenticated video feed."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, device: WelcomeEyeDevice) -> None:
        self.device = device
        super().__init__(("127.0.0.1", 0), _Handler)
        self.port: int = self.socket.getsockname()[1]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True,
                                        name="welcomeeye-ts")
        self._thread.start()
        _LOGGER.debug("TS server listening on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def locate_punch_port(host: str) -> list[int]:
    """Find the UDT port when discovery cannot reach us. Blocking and slow."""
    return find_punch_port(host, verbose=False)
