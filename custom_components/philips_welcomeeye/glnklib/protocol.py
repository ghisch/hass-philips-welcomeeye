"""GooLink / LangTao 'glnk' OWSP client - reverse engineered from libglnkav.so / libglnkio.so."""
import logging, struct, random, socket, time

_LOGGER = logging.getLogger(__name__)


from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

# CFB is being moved: it is deprecated in primitives and lives in `decrepit` from
# cryptography 49, but `decrepit.ciphers.modes` does not exist in the version Home
# Assistant currently ships. Take whichever is present - importing only the new
# location broke every module in the integration, only the old one warns.
try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # cryptography < 45
    from cryptography.hazmat.primitives.ciphers.modes import CFB

# ---------------- framing ----------------
def tlv(t, v):            return struct.pack("<HH", t, len(v)) + v
def owsp(payload, flag=0): return struct.pack(">I", len(payload)+4) + struct.pack("<I", flag) + payload

def parse_owsp(buf):
    out, off = [], 0
    while len(buf)-off >= 8:
        (blen,) = struct.unpack_from(">I", buf, off)
        (flag,) = struct.unpack_from("<I", buf, off+4)
        total = blen+4
        if blen < 4 or len(buf)-off < total: break
        body = buf[off+8:off+total]; b = 0
        while len(body)-b >= 4:
            t,l = struct.unpack_from("<HH", body, b)
            if b+4+l > len(body): break          # truncated/!TLV - do not emit garbage
            out.append((flag, t, body[b+4:b+4+l])); b += 4+l
        off += total
    return out, buf[off:]

# ---------------- LangTao random-token scheme ----------------
def _cls_idx(c):
    if 0x41 <= c <= 0x5A: return c-0x41
    if 0x61 <= c <= 0x7A: return c-0x61
    if 0x30 <= c <= 0x39: return c-0x30
    return None

def check_rand(buf, posA, posB, mode):
    """Reimplementation of checkRandDataLT()."""
    n = len(buf)
    a, b = _cls_idx(buf[posA]), _cls_idx(buf[posB])
    k1 = (a % 3) if a is not None else 0
    k2 = (b % 3) if b is not None else 0
    if k1 != k2: return False
    base, lo, hi = {1:(0x41,0x41,0x5A), 2:(0x61,0x61,0x7A), 3:(0x30,0x30,0x39)}[mode]
    for i in range(n-1):
        if i in (posA, posB): continue
        if i % 3 != k2: continue
        c = buf[i]
        if not (lo <= c <= hi) or ((c-base) & 3) != 0: return False
    return True

def gen_rand(length, posA, posB, mode):
    """Reimplementation of genRandomStringLT(): last byte NUL, markers consistent."""
    base = {1:0x41, 2:0x61, 3:0x30}[mode]
    span = {1:26, 2:26, 3:10}[mode]
    while True:
        buf = bytearray(length)
        c = random.choice([random.randrange(0x41,0x5B), random.randrange(0x61,0x7B),
                           random.randrange(0x30,0x3A)])
        buf[posA] = c
        k = _cls_idx(c) % 3
        # posB must land in the same residue class
        cands = [x for x in list(range(0x41,0x5B))+list(range(0x61,0x7B))+list(range(0x30,0x3A))
                 if _cls_idx(x) % 3 == k]
        buf[posB] = random.choice(cands)
        for i in range(length-1):
            if i in (posA, posB): continue
            if i % 3 == k:
                buf[i] = base + 4*random.randrange((span+3)//4)
            else:
                buf[i] = random.choice([random.randrange(0x41,0x5B),
                                        random.randrange(0x61,0x7B),
                                        random.randrange(0x30,0x3A)])
        buf[length-1] = 0
        if check_rand(bytes(buf), posA, posB, mode): return bytes(buf)

# ---------------- crypto ----------------
def aes_cfb(key, iv, data, decrypt=False):
    c = Cipher(algorithms.AES(key), CFB(iv))
    op = c.decryptor() if decrypt else c.encryptor()
    return op.update(data) + op.finalize()

def rc4(key, data):
    S = list(range(256)); j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(); i = j = 0
    for ch in data:
        i = (i+1) & 0xFF; j = (j+S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(ch ^ S[(S[i]+S[j]) & 0xFF])
    return bytes(out)

# ---------------- login ----------------
#: The login's channel mask is 1 << chan, and the app uses THREE different roles.
#: Channels are the SDK's internal numbering: 16 + physical channel for A/V, 0 for
#: the control/heartbeat socket. The startAV selector is the same number, so a
#: session must log in on the channel it later asks for or the device silently
#: drops every encrypted command.
CH_CTRL = 0    # heartbeat socket: mask 1<<0,  b65=0, carries TLV 509
CH_DOOR = 16   # door station:     mask 1<<16, b65=2, startAV sel 0x10 -- unlocks here
CH_CAM = 18    # second camera:    mask 1<<18, b65=2, startAV sel 0x12

APP_MASK_LO = 1 << CH_DOOR
APP_MASK_HI = 0
APP_B64 = 7
APP_B65 = 2


def login_struct(user, pwd, chan=0, b64=APP_B64, b65=APP_B65,
                 mask_lo=APP_MASK_LO, mask_hi=APP_MASK_HI):
    """68-byte _TLV_V_Login value.

    mask_lo defaults to the value the official app sends. Pass mask_lo=None to
    compute 1 << chan the way the SDK does instead.
    """
    v = bytearray(68)
    u = user.encode()[:32]; p = pwd.encode()[:16]
    v[1:1+len(u)] = u
    v[33:33+len(p)] = p
    if mask_lo is None:
        mask_lo = 1 << (chan & 0x1F)
    struct.pack_into("<I", v, 56, mask_hi)      # channel mask high
    struct.pack_into("<I", v, 60, mask_lo)      # channel mask low
    v[64] = b64
    v[65] = b65
    return bytes(v)

def plain_login(user, pwd, chan=0, ver=5, b64=0, b65=0):
    return owsp(tlv(40, struct.pack("<HH", ver, 0)) +
                tlv(41, login_struct(user, pwd, chan, b64, b65)))

def enc_login_tlv(gid, user, pwd, chan=0, b64=APP_B64, b65=APP_B65, login_id=None,
                  mask_lo=APP_MASK_LO):
    """TLV 501 - encrypted login (packLoginPacketEnc)."""
    A = gen_rand(16, 6, 10, 1)
    B = gen_rand(16, 6, 10, 1)
    C = gen_rand(16, 6, 10, 1)
    key = (A[0:8] + B[4:11]).ljust(16, b"\x00")   # 15 bytes + pad
    iv  = C[4:15].ljust(16, b"\x00")              # 11 bytes + pad
    enc_login = aes_cfb(key, iv, login_struct(user, pwd, chan, b64, b65, mask_lo=mask_lo))
    js = ('{"login_id":"%s"}' % (login_id if login_id is not None else "(null)")).encode()
    padded = ((len(js) + 5) & ~0xF) + 0x10
    enc_json = aes_cfb(key, iv, js.ljust(padded, b"\x00"))
    inner = A + enc_login + B + C + struct.pack("<I", padded) + enc_json
    return tlv(501, rc4(gid.encode(), inner))

def enc_login_frame(gid, user, pwd, chan=0, ver=5, b64=APP_B64, b65=APP_B65,
                    login_id=None, mask_lo=APP_MASK_LO):
    return owsp(tlv(40, struct.pack("<HH", ver, 0)) +
                enc_login_tlv(gid, user, pwd, chan, b64, b65, login_id, mask_lo))

# ---------------- discovery ----------------
def discover(target="255.255.255.255", timeout=9.0, gid=b"", retry_every=3.0):
    """Probe for devices on UDP 1500. Gently.

    The device answers every probe when left alone: 180 of 180 over three minutes
    with nothing else talking to it. Probed continuously - once a second, as a
    client with a long retry window does - it stops answering for about 90 seconds
    at a time. Each reply also allocates a fresh UDT port on the device, so a
    stream of probes is a stream of allocations. Three probes, three seconds
    apart, is enough to survive a lost datagram without starving the device.

    Note the device answers from an EPHEMERAL source port, not from 1500. The
    socket is therefore left unconnected so it accepts a reply from any source -
    and any NAT in the path will drop that reply, because it does not match the
    flow that went out. That is why discovery does not survive a routed or VPN
    path even when the device is perfectly reachable.
    """
    pkt = tlv(0xA007, gid.ljust(32, b"\x00"))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    s.bind(("0.0.0.0", 0))
    single = target not in ("255.255.255.255", "<broadcast>")
    found, seen = [], set()
    sent = received = unparsed = 0
    send_error = None
    deadline, last_probe = time.time() + timeout, 0.0
    while time.time() < deadline:
        if time.time() - last_probe >= retry_every:
            try:
                s.sendto(pkt, (target, 1500))
                sent += 1
            except OSError as err:
                # never swallow this: a send that always fails looks exactly like
                # a device that never answers, and sends the search somewhere else
                send_error = err
            last_probe = time.time()
        try:
            d, a = s.recvfrom(4096)
        except socket.timeout:
            continue
        except ConnectionRefusedError:
            continue                      # ICMP unreachable from an earlier probe
        received += 1
        r = parse_discovery(d)
        if not r:
            unparsed += 1
            continue
        if r["gid"] in seen:
            continue
        seen.add(r["gid"])
        r["from"] = a[0]
        found.append(r)
        if single:
            break                         # asked about one device, and it answered
    local = None
    try:
        local = s.getsockname()
    except OSError:
        pass
    s.close()
    if not found:
        _LOGGER.warning(
            "discovery found nothing for %s: %d probes sent, %d datagrams received, "
            "%d unparsable, local socket %s, send error %r",
            target, sent, received, unparsed, local, send_error)
    return found

def parse_discovery(d):
    if len(d) < 8: return None
    t, l = struct.unpack_from("<HH", d, 0)
    val = d[4:4+l]
    if t == 0xA036 and l == 100:
        t1, ct, t2 = val[0:16], val[16:84], val[84:100]
        key = t1[5:14].ljust(16, b"\x00"); iv = t2[3:9].ljust(16, b"\x00")
        pt = aes_cfb(key, iv, ct, decrypt=True)
    elif t == 0xA008 and l == 68:
        pt = val
    else:
        return None
    gid = pt[:32].split(b"\x00")[0].decode(errors="replace")
    p1, p2 = struct.unpack_from("<HH", pt, 32)
    ip = pt[36:52].split(b"\x00")[0].decode(errors="replace")
    ver = pt[64:68].split(b"\x00")[0].decode(errors="replace")
    return {"gid": gid, "port": p2, "port2": p1, "ip": ip, "ver": ver, "raw": pt.hex()}

def find_punch_port(ip, ranges=((10000, 32767), (32768, 49151), (49152, 65535)),
                    verbose=True):
    """Find the device's UDT listener by probing with real handshakes.

    Discovery is the normal way to learn this port, but it does not answer across
    some routed/VPN paths, and the port is randomised so a stale value is useless.
    A closed UDP port answers with ICMP unreachable (invisible on an unconnected
    socket); an open UDT port answers with a handshake. Paced, not a flood.
    """
    import random as _r
    from .udt import CTRL as _C, MSS as _M, FLIGHT as _F, _ipfield as _ipf
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    cif = struct.pack(">8i", 4, 1, _r.randrange(1, 0x7FFFFFF), _M, _F, 1,
                      _r.randrange(1, 0x7FFFFFFF), 0) + _ipf(ip)
    pkt = struct.pack(">IIII", _C, 0, 0, 0) + cif
    found = []

    def drain():
        while True:
            try:
                d, a = s.recvfrom(4096)
            except (BlockingIOError, OSError):
                return
            if len(d) >= 16 and struct.unpack_from(">I", d, 0)[0] & _C:
                found.append(a[1])

    for lo, hi in ranges:
        if verbose:
            print(f"  probing udp {lo}-{hi}...", flush=True)
        for i, p in enumerate(range(lo, hi + 1)):
            try:
                s.sendto(pkt, (ip, p))
            except OSError:
                time.sleep(0.005)
            if i % 500 == 0:
                drain(); time.sleep(0.05)
        for _ in range(40):
            drain(); time.sleep(0.1)
        if found:
            break
    s.close()
    return sorted(set(found))


def hexdump(d, w=16):
    for i in range(0, len(d), w):
        c = d[i:i+w]
        print("  %04x  %-*s  %s" % (i, w*3, " ".join("%02x"%x for x in c),
              "".join(chr(x) if 32<=x<127 else "." for x in c)))

def parse_login_rsp(gid, v):
    """Decode TLV 502 (TLV_T_LOGIN_RSP2)."""
    pt = rc4(gid.encode(), v)
    A, B, C = pt[0:16], pt[0x1c:0x2c], pt[0x2c:0x3c]
    key = (A[0:11] + B[4:8]).ljust(16, b"\x00")   # response derivation
    iv  = C[4:12].ljust(16, b"\x00")
    body = aes_cfb(key, iv, pt[0x10:0x1c], decrypt=True)
    jlen = struct.unpack_from("<I", pt, 0x3c)[0]
    js   = aes_cfb(key, iv, pt[0x40:0x40+jlen], decrypt=True).rstrip(b"\x00")
    # body = { u32 device_unix_time; u32 zero; u16 result; u16 extra }
    return {"tokens": (A, B, C),
            "result": struct.unpack_from("<H", body, 8)[0],
            "extra":  struct.unpack_from("<H", body, 10)[0],
            "time":   struct.unpack_from("<I", body, 0)[0],
            "body": body, "json": js}

# ---------------- QvEncrypt.encodeWithHashAndMd5 ----------------
import hashlib
def device_password(plain):
    """QvEncrypt.encodeWithHashAndMd5: sha256 -> md5 -> 8-char compress."""
    if not plain: return ""
    sha = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    md5 = hashlib.md5(sha.encode()).hexdigest()
    ints = [int(md5[i*2:i*2+2], 16) for i in range(16)]
    out = bytearray(8)
    for i in range(8):
        b = (ints[i*2] + ints[i*2+1]) % 62
        if   0 <= b <= 9:  b += 48   # '0'-'9'
        elif 10 <= b <= 35: b += 55  # 'A'-'Z'
        else:               b += 61  # 'a'-'z'
        out[i] = b
    return out.decode()

RESULT_CODES = {1:"SUCCESS", 2:"USER_PWD_ERROR", 4:"PDA_VERSION_ERROR", 5:"MAX_USER_ERROR",
    6:"DEVICE_OFFLINE", 7:"DEVICE_HAS_EXIST", 8:"DEVICE_OVERLOAD", 9:"INVALID_CHANNEL",
    10:"PROTOCOL_ERROR", 11:"NOT_START_ENCODE", 12:"TASK_DISPOSE_ERROR", 13:"CONFIG_ERROR",
    14:"NOT_SUPPORT_TALK", 15:"TIME_ERROR", 16:"OVER_INDEX_ERROR", 17:"MEMORY_ERROR",
    18:"QUERY_ERROR", 19:"NO_USER_ERROR", 20:"NOW_EXITING", 21:"GET_DATA_FAIL",
    22:"RIGHT_ERROR", 23:"OPEN_LOCK_PWD_ERROR", 24:"NO_VIDEO", 25:"WIFI_CONFIG_FAILED",
    32:"CRYPT_ERROR", 2561:"ERROR_OLD_VERSION"}

def lock_req_payload(lock_pwd, channel=0, action=1, delay=0, device_id=0):
    """44-byte _TLV_V_Lock_Req."""
    v = bytearray(44)
    struct.pack_into("<I", v, 0, device_id)
    p = lock_pwd.encode()[:32]
    v[4:4+len(p)] = p
    struct.pack_into("<I", v, 36, delay)
    v[40] = channel
    v[41] = action
    return bytes(v)

class Session:
    """Authenticated local-LAN session against a glnk device."""
    def __init__(self, ip, gid, port=6800):
        self.ip, self.gid, self.port = ip, gid, port
        self.sock = None; self.buf = b""
    def connect(self, user, pwd, chan=0, timeout=5.0, raw_pwd=False):
        self.sock = socket.socket(); self.sock.settimeout(timeout)
        self.sock.connect((self.ip, self.port))
        wire_pwd = pwd if raw_pwd else device_password(pwd)
        self.sock.sendall(enc_login_frame(self.gid, user, wire_pwd, chan=chan, login_id=self.gid))
        for _, t, v in self.recv(3.0):
            if t == 502:
                r = parse_login_rsp(self.gid, v)
                r["ok"] = (r["result"] == 1)
                self._dev_time, self._dev_t0 = r["time"], time.monotonic()
                return r
        return {"ok": False, "result": None, "error": "no TLV 502"}

    def device_now(self):
        """The device's clock, advanced locally. Commands carry a live timestamp -
        in the capture it tracks wall time, it is not the login value replayed."""
        if self._dev_time is None:
            return int(time.time())
        return int(self._dev_time + (time.monotonic() - self._dev_t0))
    def recv(self, secs=2.0):
        out = []; t0 = time.time()
        while time.time()-t0 < secs:
            try: d = self.sock.recv(65536)
            except socket.timeout: continue
            if not d: break
            self.buf += d
            fr, self.buf = parse_owsp(self.buf)
            out += fr
            if out: break
        return out
    def send_tlv(self, t, v): self.sock.sendall(owsp(tlv(t, v)))
    def close(self):
        if self.sock: self.sock.close(); self.sock = None


class UdtSession:
    """Authenticated LAN session over UDT4 - the transport the official app uses.

    TCP 6800 accepts the login but ignores every command afterwards; the app never
    touches it. Everything real happens as OWSP frames carried as UDT message
    payloads on the punch port advertised by discovery, which is randomised and
    rotates.
    """

    def __init__(self, ip, gid, port=None, verbose=False):
        from .udt import UdtClient
        if port is None:
            # use the module default: the probe is retried across the window,
            # and 3 seconds is thin on a lossy link
            d = discover(ip)
            if not d:
                raise RuntimeError(
                    f"{ip} did not answer a discovery probe on UDP 1500. The UDT "
                    f"port is randomised and rotates, so discovery is the only way "
                    f"to learn it. Check that the device is powered on, that it is "
                    f"on the same subnet as Home Assistant, and that nothing "
                    f"between them blocks UDP 1500.")
            port = d[0]["port2"]
        self.ip, self.gid, self.port = ip, gid, port
        self.c = UdtClient(ip, port, verbose=verbose)
        self.buf = b""             # leftover of the last completed message
        self.msgbuf = bytearray()  # partial UDT message being reassembled
        self.verbose = verbose
        self._dev_time = None      # device clock at login
        self._dev_t0 = None        # local monotonic at that moment
        self.login_frames = []     # every TLV that arrived with the login reply

    # -------------------------------------------------------------- transport
    def _ctrl_packet(self, w0, w1):
        """Answer one UDT control packet. Returns True if the peer shut us down."""
        from .udt import CTRL, T_ACK, T_ACK2, T_NAK, T_KEEPALIVE, T_SHUTDOWN
        ct = (w0 >> 16) & 0x7FFF
        if ct == T_ACK:
            self.c.sock.sendto(self.c._ctrl(T_ACK2, info=w1), (self.ip, self.port))
        elif ct == T_KEEPALIVE:
            self.c.keepalive()
        elif ct == T_SHUTDOWN:
            if self.verbose:
                print("  [udt] peer shutdown")
            return True
        elif ct == T_NAK and self.verbose:
            print("  [udt] NAK")
        return False

    def messages(self, secs, ack_every=0.01):
        """Yield complete UDT messages for `secs`, answering control traffic.

        UDT is message oriented: the FF bits say solo (3), first (2), middle (0),
        last (1). Payloads must never be concatenated across message boundaries -
        doing so desyncs the frame parser permanently the moment video starts.

        ACKs are rate limited to `ack_every` rather than sent per packet; UDT's own
        model is a periodic ACK timer, and one ACK per data packet doubles our
        outbound packet rate for nothing.
        """
        import socket as _s
        from .udt import CTRL, T_ACK
        end = time.time() + secs
        last_ack, last_seq = 0.0, 0
        while time.time() < end:
            try:
                pkt, _a = self.c.sock.recvfrom(65536)
            except _s.timeout:
                continue
            if len(pkt) < 16:
                continue
            w0, w1, _ts, _dst = struct.unpack_from(">IIII", pkt, 0)
            if w0 & CTRL:
                if self._ctrl_packet(w0, w1):
                    return
                continue
            last_seq = w0 & 0x7FFFFFFF
            now = time.time()
            if now - last_ack >= ack_every:
                cif = struct.pack(">iiiiii", (last_seq + 1) & 0x7FFFFFFF,
                                  100000, 50000, 8192, 1000, 0)
                self.c.sock.sendto(self.c._ctrl(T_ACK, cif, info=1), (self.ip, self.port))
                last_ack = now
            ff = (w1 >> 30) & 3
            if ff & 2:                      # 3 solo, 2 first
                self.msgbuf = bytearray()
            self.msgbuf += pkt[16:]
            if ff & 1:                      # 3 solo, 1 last
                m = bytes(self.msgbuf)
                self.msgbuf = bytearray()
                yield m

    def _service(self, secs):
        """Pump for `secs` and return any OWSP frames that completed."""
        out = []
        for m in self.messages(secs):
            frames, self.buf = parse_owsp(m)
            out += frames
            if out:
                break
        return out

    # ------------------------------------------------------------------- API
    def connect(self, user, pwd, chan=CH_DOOR, timeout=8.0, raw_pwd=False):
        if not self.c.connect(timeout):
            return {"ok": False, "result": None, "error": "UDT handshake failed"}
        wire_pwd = pwd if raw_pwd else device_password(pwd)
        # derive the mask from the channel and match b65 to the role, exactly as the app
        self.c.send(enc_login_frame(self.gid, user, wire_pwd, chan=chan,
                                    b65=2 if chan else 0, mask_lo=None,
                                    login_id=self.gid))
        # the login reply carries TLV 40 and 70 alongside 502; keep them, callers
        # that want device info should not have to ask for it again
        self.login_frames = self._service(5.0)
        for _f, t, v in self.login_frames:
            if t == 502:
                r = parse_login_rsp(self.gid, v)
                r["ok"] = (r["result"] == 1)
                self._dev_time, self._dev_t0 = r["time"], time.monotonic()
                return r
        return {"ok": False, "result": None, "error": "no TLV 502"}

    def device_now(self):
        """The device's clock, advanced locally. Commands carry a live timestamp -
        in the capture it tracks wall time, it is not the login value replayed."""
        if self._dev_time is None:
            return int(time.time())
        return int(self._dev_time + (time.monotonic() - self._dev_t0))

    def send_tlv(self, t, v):
        self.c.send(owsp(tlv(t, v)))

    def recv(self, secs=3.0):
        return self._service(secs)

    def close(self):
        self.c.close()
