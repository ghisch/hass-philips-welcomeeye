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
from .glnklib.protocol import (CH_CAM, CH_DOOR, RESULT_CODES, UdtSession, discover,
                               find_punch_port, parse_owsp, tlv)
from .glnklib.tsmux import TsMuxer

_LOGGER = logging.getLogger(__name__)

CHANNEL_MAP = {CHANNEL_DOOR: (CH_DOOR, AV_DOOR), CHANNEL_CAM: (CH_CAM, AV_CAM)}

V_HDR, V_KEY, V_INTER, A_HDR, A_DATA = 99, 100, 101, 97, 98
VIDEO_TLVS = frozenset({V_KEY, V_INTER})
PRIV_INFO_TLV = 469          # asking this returns the device's version JSON

#: how long to wait for door video before deciding an unlock cannot work
AV_WAIT_SECONDS = 10.0
#: a streaming session with no packet at all for this long is dead; the
#: device never goes quiet while it serves video
SILENCE_SECONDS = 15.0

#: The quiet spells were self-inflicted. Left alone, the device answers every
#: discovery probe: 180 of 180 over three minutes with the integration disabled.
#: Probed once a second - which a 25 to 150 second retry window amounts to - it
#: stops answering for about 90 seconds at a time, and each reply allocates a
#: fresh UDT port on the device that then dies within a minute under that churn.
#:
#: So: a few spaced probes right before connecting, never a window, and a pause
#: after any failure so Home Assistant's own retries cannot turn into a hammer.
DISCOVERY_WINDOW_LONG = 9.0
DISCOVERY_WINDOW_SHORT = 9.0
FAILURE_BACKOFF = 30.0

#: A discovered port is short-lived. In the debug log, every port that answered a
#: handshake 2 to 5 seconds after discovery had stopped answering 37 to 60
#: seconds later, five times out of five. So a port is reused only while it is
#: young; otherwise discovery runs again, immediately before connecting.
PORT_REUSE_SECONDS = 20.0
#: A burst of handshakes to a dead port right before a probe seemed to cost the
#: probe its reply. A short pause between the two is cheap insurance.
SETTLE_AFTER_FAILED_HANDSHAKE = 1.5

#: What the device actually does, read off a full debug log:
#:
#:   discovery hands a client ONE port, and keeps re-advertising the same one
#:   for as long as nobody uses it (20 probes, one port, all answered);
#:   a session on that port consumes it - a second login still works for a few
#:   seconds, then the port stops answering;
#:   and from that session until roughly 100 seconds later, discovery from the
#:   same client gets no reply at all. Three times in one log: answers at
#:   22:47:04, 22:48:43 and 22:50:28, silence in between, with a session having
#:   been opened after each answer.
#:
#: So every session costs about 100 seconds of lockout. The design here opens as
#: few as it can: setup only discovers, the stream is one shared session, and
#: the gate rides on the live stream session when there is one.


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
    #: true only while frames are actually being written to a viewer
    streaming: bool = False
    frames: int = 0
    bytes_sent: int = 0
    started_at: float | None = None
    last_error: str | None = None


class WelcomeEyeDevice:
    """One intercom. Owns its credentials, its state and its local TS server."""

    def __init__(self, host: str, gid: str, username: str, password: str,
                 punch_port: int | None = None,
                 lock_channel: int = LOCK_CHANNEL_GATE,
                 hint_port: int | None = None) -> None:
        self.host = host
        self.gid = gid
        self.username = username
        self._password = password
        #: A fixed port, only for paths where discovery cannot reach the device.
        #: Normally left unset: the port is randomised and rotates often (observed
        #: moving twice in one evening), so it is rediscovered per connection.
        self.fixed_port = punch_port
        #: Last port known to work, from the config entry. A hint only, and only
        #: trusted while young - see PORT_REUSE_SECONDS.
        self.punch_port: int | None = punch_port or hint_port
        self._port_time: float = 0.0
        self.lock_channel = lock_channel
        self.av_wait = AV_WAIT_SECONDS

        self.channel: str = CHANNEL_DOOR
        self.stream_enabled: bool = DEFAULT_STREAM_ENABLED
        self.stats = StreamStats()
        self.info: dict[str, object] = {}
        self.last_unlock: dict[str, object] | None = None

        self._server: _TsServer | None = None
        self._lock = threading.Lock()
        #: Set while an unlock is in flight. Viewers stop and new ones are refused,
        #: because the unlock needs the door channel and the device only serves one.
        self._unlocking = threading.Event()
        self._unlock_lock = threading.Lock()
        #: One session setup at a time. Several stream viewers arriving together
        #: must not each run their own discovery.
        self._session_lock = threading.Lock()
        #: The stream's session while frames are flowing, so the gate can use it
        #: instead of paying for a session of its own.
        self._live: UdtSession | None = None
        self._live_channel: str | None = None
        self._live_lock = threading.Lock()
        self._lock_rsp: dict | None = None
        self._lock_rsp_event = threading.Event()
        #: Called with a new port so it can be persisted. Set by the integration.
        self.on_port_change: object = None
        #: Called after the camera source changed, so the camera entity can make
        #: Home Assistant restart its stream at once instead of on its own retry.
        self.on_channel_change: object = None
        #: called with True/False when the stream switch moves
        self.on_stream_enabled_change = None
        #: Set after a discovery failure. Until it passes, session attempts fail
        #: at once instead of probing again - Home Assistant retries a camera
        #: stream every few seconds, and each retry must not cost the device.
        self._backoff_until: float = 0.0

    # ------------------------------------------------------------------ session
    def _connect_once(self, channel_key: str, port: int | None) -> tuple[UdtSession, dict]:
        chan, _sel = CHANNEL_MAP[channel_key]
        try:
            session = UdtSession(self.host, self.gid, port)
        except RuntimeError as err:
            raise WelcomeEyeError(str(err)) from err
        try:
            # a port that no longer answers should not cost the full 8 s handshake
            res = session.connect(self.username, self._password, chan=chan,
                                  raw_pwd=True, timeout=4.0)
        except OSError as err:
            session.close()
            raise WelcomeEyeError(f"cannot reach {self.host}: {err}") from err
        if not res.get("ok"):
            session.close()
            code = res.get("result")
            if code is not None:
                raise WelcomeEyeError(f"login rejected: {RESULT_CODES.get(code, code)}")
            raise WelcomeEyeError(f"{res.get('error', 'no login response')} on port {port}")
        _LOGGER.debug("logged in on port %s, device time %s", session.port, res.get("time"))
        self.punch_port = session.port
        return session, res

    def _discover_port(self, window: float) -> int:
        """Ask the device which UDT port to use."""
        found = discover(self.host, timeout=window)
        if not found:
            raise WelcomeEyeError(
                f"{self.host} did not answer a discovery probe on UDP 1500 within "
                f"{window:.0f}s. It answers reliably when left alone, so a single "
                f"failure right after other traffic is usually nothing. If it never "
                f"answers, check that it is powered on, on the same subnet as Home "
                f"Assistant, and that nothing between them blocks UDP 1500.")
        port = found[0]["port2"]
        self._port_time = time.time()
        if port != self.punch_port and callable(self.on_port_change):
            self.on_port_change(port)
        return port

    def _session(self, channel_key: str,
                 discover_window: float = DISCOVERY_WINDOW_SHORT
                 ) -> tuple[UdtSession, dict]:
        """Log in on the channel's own mask. Caller must close the session.

        A known port is tried before discovery, and that order matters. The port
        outlives a session and stays usable for minutes, while discovery goes
        quiet in bursts; rediscovering on every session meant landing in one of
        those bursts and failing for no reason.
        """
        with self._session_lock:
            remaining = self._backoff_until - time.time()
            if remaining > 0:
                raise WelcomeEyeError(
                    f"not probing the device again for {remaining:.0f}s after the "
                    f"last discovery failure, so repeated retries cannot starve it")
            port = self.fixed_port
            age = (time.time() - self._port_time) if self._port_time else float("inf")
            if port is None and self.punch_port is not None and age < PORT_REUSE_SECONDS:
                port = self.punch_port
            if port is not None:
                _LOGGER.debug("session on %s: trying port %s (age %.0fs)",
                              channel_key, port, age)
                try:
                    return self._connect_once(channel_key, port)
                except WelcomeEyeError as err:
                    if port == self.fixed_port:
                        raise       # the user pinned this port on purpose
                    _LOGGER.warning("port %s failed after %.0fs (%s); rediscovering",
                                    port, age, err)
                    time.sleep(SETTLE_AFTER_FAILED_HANDSHAKE)
            elif self.punch_port is not None:
                _LOGGER.debug("session on %s: port %s is %s, discovering afresh",
                              channel_key, self.punch_port,
                              "of unknown age" if age == float("inf") else f"{age:.0f}s old")
            try:
                port = self._discover_port(discover_window)
            except WelcomeEyeError:
                self._backoff_until = time.time() + FAILURE_BACKOFF
                raise
            self.punch_port = port
            _LOGGER.info("session on %s: discovered port %s", channel_key, port)
            return self._connect_once(channel_key, port)

    def _start_av(self, session: UdtSession, channel_key: str) -> None:
        _chan, sel = CHANNEL_MAP[channel_key]
        session.send_tlv(TLV_START_AV_ENC,
                         wrap_cmd(self.gid, start_av_body(session.device_now(), sel),
                                  TLV_START_AV_ENC))

    # -------------------------------------------------------------- operations
    def discover_only(self) -> dict[str, object]:
        """Find the device without logging in. Blocking.

        Discovery keeps re-advertising an unused port; a login consumes it and
        locks discovery out for about 100 seconds. Setup therefore stops here, and
        the first session is whatever the user asks for next - which then gets a
        port that is still fresh.
        """
        with self._session_lock:
            port = self.fixed_port or self._discover_port(DISCOVERY_WINDOW_LONG)
            self.punch_port = port
        info: dict[str, object] = {"punch_port": port}
        if self.info:
            info = {**self.info, **info}
        self.info = info
        return info

    def probe(self) -> dict[str, object]:
        """Log in and collect what the device volunteers. Blocking."""
        session, res = self._session(self.channel,
                                     discover_window=DISCOVERY_WINDOW_LONG)
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

        Borrows the door channel for the duration and gives it straight back. The
        device serves one channel at a time, so an unlock attempted while something
        is watching the second camera would get no door video and be refused. Any
        viewers are therefore dropped first, new ones are held off, and afterwards
        the configured channel is untouched so they reconnect exactly where they
        were - or stay off, if streaming was off to begin with.

        Refuses unless door video is actually flowing: without it the device
        answers result=1 and does nothing, which would otherwise be reported as
        success.
        """
        with self._unlock_lock:
            with self._live_lock:
                live = self._live if (self._live_channel == CHANNEL_DOOR
                                      and self.stats.streaming) else None
            if live is not None:
                # door video is already flowing on this session: that is the
                # precondition, and no new session means no 100 s lockout
                _LOGGER.info("gate: using the live door stream session")
                return self._send_lock(live)
            self._unlocking.set()
            try:
                self._wait_for_viewers_to_release()
                return self._unlock_locked()
            finally:
                # nothing to restore: the channel selection was never changed, so
                # viewers reconnecting land back on whatever is configured
                self._unlocking.clear()

    def _send_lock(self, session: UdtSession) -> dict[str, object]:
        """Send TLV 505 on a session whose message loop runs elsewhere, and wait
        for the 506 that loop hands back."""
        self._lock_rsp, self._lock_rsp_event = None, threading.Event()
        body = lock_body(self._password, channel=self.lock_channel, action=1,
                         delay=0, dev_time=session.device_now())
        session.send_tlv(TLV_LOCK_REQ_ENC, wrap_cmd(self.gid, body, TLV_LOCK_REQ_ENC))
        result: dict[str, object] = {"acknowledged": False, "at": time.time()}
        if self._lock_rsp_event.wait(8.0) and self._lock_rsp:
            result.update(acknowledged=True, **self._lock_rsp)
        self.last_unlock = result
        return result

    def _wait_for_viewers_to_release(self, timeout: float = 4.0) -> None:
        """Let current viewers notice they have been stopped and close their
        sessions, so the channel is free."""
        deadline = time.time() + timeout
        while self.stats.clients > 0 and time.time() < deadline:
            time.sleep(0.1)

    def _unlock_locked(self) -> dict[str, object]:
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
        if enabled == self.stream_enabled:
            return
        self.stream_enabled = enabled
        _LOGGER.info("streaming switched %s", "on" if enabled else "off")
        if not enabled:
            self._drop_viewers("streaming was switched off")
        if callable(self.on_stream_enabled_change):
            self.on_stream_enabled_change(enabled)

    def _drop_viewers(self, reason: str) -> None:
        """End the device session but keep the listening port.

        Home Assistant's stream worker remembers the URL it was given and keeps
        retrying it; it does not ask for a new one. A server that goes away takes
        its port with it, and the worker then retries a dead port for good. That
        is what "stuck after switching camera" was.
        """
        with self._lock:
            if self._server is not None:
                self._server.drop_viewers(reason, join=True)

    def set_channel(self, channel: str) -> None:
        if channel not in CHANNEL_MAP:
            raise ValueError(channel)
        if channel == self.channel:
            return
        self.channel = channel
        _LOGGER.info("camera source -> %s", channel)
        # end the old session first - the device serves one channel at a time and
        # the incumbent keeps it - then let viewers reconnect to the SAME port
        self._drop_viewers("the camera source changed")
        if callable(self.on_channel_change):
            self.on_channel_change()

    def pump_frames(self, on_frame, should_stop) -> None:
        """Run ONE device session and hand every video frame to `on_frame(payload,
        pts_90k, keyframe)` until `should_stop()`. Runs on its own thread.

        One session serves every viewer. The device allows one session per
        discovered port and ports are short-lived, so a session per viewer meant
        the second viewer - which Home Assistant opens for stills - fought the
        first for a port and lost.
        """
        channel_key = self.channel
        session, _res = self._session(channel_key)
        with self._live_lock:
            self._live, self._live_channel = session, channel_key
        frames0 = self.stats.frames
        try:
            self._start_av(session, channel_key)
            started, ts0 = False, None
            self.stats.started_at = time.time()
            _LOGGER.info("stream: session up on %s, waiting for a keyframe", channel_key)
            for msg in session.messages(1e9, should_stop=should_stop,
                                        silence=SILENCE_SECONDS):
                if should_stop():
                    break
                kind, payload, ts_ms = _media(msg)
                if kind is None:
                    # not media: the gate's reply arrives here when it used this session
                    for _flag, tlv_type, value in parse_owsp(msg)[0]:
                        if tlv_type == 506:
                            rsp = parse_lock_rsp(self.gid, value)
                            self._lock_rsp = {"result": rsp["result"], "extra": rsp["extra"]}
                            self._lock_rsp_event.set()
                    continue
                if kind not in ("key", "inter"):
                    continue
                if not started:
                    if kind != "key":
                        continue          # never hand a decoder a mid-GOP start
                    started = True
                    self.stats.streaming = True
                    _LOGGER.info("stream: first keyframe, frames are flowing")
                if ts0 is None:
                    ts0 = ts_ms if ts_ms is not None else 0
                pts = ((ts_ms - ts0) if ts_ms is not None else 0) * 90
                on_frame(payload, pts + 90000, kind == "key")
                self.stats.frames += 1
                self.stats.bytes_sent += len(payload)
        finally:
            with self._live_lock:
                self._live, self._live_channel = None, None
            self.stats.streaming = False
            # should_stop() names its reason; otherwise the session knows why
            # its message loop returned
            reason = should_stop() or session.end_reason or "the session ended"
            _LOGGER.info("stream: session on %s ended after %d frames: %s",
                         channel_key, self.stats.frames - frames0, reason)
            session.close()


class _Viewer:
    """One TCP client. Gets its own muxer so its continuity counters are its
    own, and starts receiving at the first keyframe after it joined."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.mux = TsMuxer(sock.sendall)
        self.started = False
        self.gone = threading.Event()


class _Handler(socketserver.BaseRequestHandler):
    """Register the viewer, then wait here until it hangs up. Frames are written
    by the server's pump thread, not by this one."""

    def handle(self) -> None:  # noqa: D102
        server: _TsServer = self.server  # type: ignore[assignment]
        device = server.device
        if not device.stream_enabled or device._unlocking.is_set():
            _LOGGER.debug("stream: viewer refused (%s)",
                          "streaming is switched off" if not device.stream_enabled
                          else "the gate is using the device")
            return
        viewer = server.add_viewer(self.request)
        self.request.settimeout(1.0)
        try:
            while not viewer.gone.is_set():
                try:
                    if self.request.recv(1) == b"":
                        break                       # viewer closed the connection
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            server.remove_viewer(viewer)


class _TsServer(socketserver.ThreadingTCPServer):
    """Loopback-only MPEG-TS server. Bound to 127.0.0.1 so nothing off-host can
    reach an unauthenticated video feed. Runs a single device session while at
    least one viewer is connected and fans the frames out to all of them."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, device: WelcomeEyeDevice) -> None:
        self.device = device
        super().__init__(("127.0.0.1", 0), _Handler)
        self.port: int = self.socket.getsockname()[1]
        self._thread: threading.Thread | None = None
        self._viewers: list[_Viewer] = []
        self._vlock = threading.Lock()
        self._pump: threading.Thread | None = None
        self._stopping = False
        self._drop_reason = ""           # why the viewers were last let go

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True,
                                        name="welcomeeye-ts")
        self._thread.start()
        _LOGGER.debug("TS server listening on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        self._stopping = True
        with self._vlock:
            for v in self._viewers:
                v.gone.set()
            self._viewers.clear()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # Wait for the device session to actually close. Switching camera source
        # stops this server and starts a new one on the other channel; the device
        # serves one channel at a time and the incumbent keeps it, so a new session
        # opened while the old one is still winding down gets no video.
        pump = self._pump
        if pump is not None and pump.is_alive() and pump is not threading.current_thread():
            pump.join(timeout=6)
            if pump.is_alive():
                _LOGGER.warning("stream: previous session did not close in time")

    # --------------------------------------------------------------- viewers
    def add_viewer(self, sock: socket.socket) -> _Viewer:
        viewer = _Viewer(sock)
        with self._vlock:
            self._viewers.append(viewer)
            self._drop_reason = ""
            self.device.stats.clients = len(self._viewers)
            _LOGGER.info("stream: viewer connected (%d now)", len(self._viewers))
            if self._pump is None or not self._pump.is_alive():
                self._pump = threading.Thread(target=self._run_pump, daemon=True,
                                              name="welcomeeye-pump")
                self._pump.start()
        return viewer

    def remove_viewer(self, viewer: _Viewer) -> None:
        with self._vlock:
            if viewer in self._viewers:
                self._viewers.remove(viewer)
                if not self._viewers and not self._drop_reason:
                    self._drop_reason = "the last viewer hung up"
            viewer.gone.set()
            self.device.stats.clients = len(self._viewers)
            _LOGGER.info("stream: viewer gone (%d left)", len(self._viewers))

    def _drop_all(self, reason: str) -> None:
        """Let every viewer go so Home Assistant reconnects; `reason` is what
        the pump then logs."""
        with self._vlock:
            self._drop_reason = reason
            for v in self._viewers:
                v.gone.set()
            self._viewers.clear()
            self.device.stats.clients = 0

    def drop_viewers(self, reason: str, join: bool = False) -> None:
        """Disconnect every viewer and end the session; keep listening."""
        self._drop_all(reason)
        pump = self._pump
        if join and pump is not None and pump.is_alive() \
                and pump is not threading.current_thread():
            pump.join(timeout=6)
            if pump.is_alive():
                _LOGGER.warning("stream: previous session did not close in time")

    # ------------------------------------------------------------------ pump
    def _should_stop(self) -> str:
        """Why the pump must end now, or "" to keep going."""
        if self._stopping:
            return "the integration is unloading"
        if self.device._unlocking.is_set():
            return "the gate needs the door channel"
        with self._vlock:
            if not self._viewers:
                return self._drop_reason or "the last viewer hung up"
        return ""

    def _on_frame(self, payload, pts: int, keyframe: bool) -> None:
        with self._vlock:
            viewers = list(self._viewers)
        for v in viewers:
            if v.gone.is_set():
                continue
            if not v.started:
                if not keyframe:
                    continue                    # join at a keyframe, never mid-GOP
                v.started = True
            try:
                v.mux.write_frame(payload, pts, keyframe=keyframe)
            except OSError:
                self.remove_viewer(v)

    def _run_pump(self) -> None:
        try:
            self.device.pump_frames(self._on_frame, self._should_stop)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:        # noqa: BLE001 - surfaced as a diagnostic
            self.device.stats.last_error = str(err)
            _LOGGER.warning("stream failed: %s", err)
        finally:
            self._drop_all("the session ended")


def locate_punch_port(host: str) -> list[int]:
    """Find the UDT port when discovery cannot reach us. Blocking and slow."""
    return find_punch_port(host, verbose=False)
