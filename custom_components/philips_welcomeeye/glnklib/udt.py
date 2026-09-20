"""Minimal UDT4 client - the transport the WelcomeEye app uses for the LAN channel.

All UDT headers and the handshake CIF are big-endian (network order).

  control:  [1|type:15|ext:16] [additional info] [timestamp] [dest sockid] [CIF...]
  data:     [0|seq:31]         [FF:2|O:1|msg:29] [timestamp] [dest sockid] [payload]

Handshake CIF (48 bytes): version, sockType, ISN, MSS, flightFlagSize, reqType,
sockID, cookie, peerIP[16].  Cookie exchange: req=1/cookie=0 -> req=1/cookie=N ->
req=-1/cookie=N -> req=-1 + the peer's socket id.
"""
import socket, struct, time, random

CTRL = 0x80000000
# UDT4 control packet types (RFC draft / udt4 packet.h). Getting SHUTDOWN and ACK2
# the wrong way round makes every ACK answer a connection teardown.
T_HANDSHAKE, T_KEEPALIVE, T_ACK, T_NAK = 0, 1, 2, 3
T_CONGEST, T_SHUTDOWN, T_ACK2, T_DROPREQ, T_ERR = 4, 5, 6, 7, 8
CTRL_NAMES = {0: "HANDSHAKE", 1: "KEEPALIVE", 2: "ACK", 3: "NAK", 4: "CONGEST",
              5: "SHUTDOWN", 6: "ACK2", 7: "DROPREQ", 8: "ERR", 0x7FFF: "USER"}
MSS = 1386
FLIGHT = 1000


def _ipfield(ip):
    """UDT stores the peer IP as 4 host-order u32; for IPv4 that is the address reversed."""
    return bytes(reversed(socket.inet_aton(ip))) + b"\x00" * 12


class UdtClient:
    def __init__(self, ip, port, verbose=False):
        self.ip, self.port, self.verbose = ip, port, verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.5)
        self.sockid = random.randrange(1, 0x7FFFFFFF)
        self.isn = random.randrange(1, 0x7FFFFFF)
        self.peer_sockid = 0
        self.seq = self.isn
        self.msgno = 1
        self.t0 = time.time()
        self.rx = bytearray()
        self.last_ack = None

    # ---------------------------------------------------------------- helpers
    def _ts(self):
        return int((time.time() - self.t0) * 1e6) & 0xFFFFFFFF

    def _ctrl(self, ctype, cif=b"", info=0, dest=None):
        return (struct.pack(">IIII", CTRL | (ctype << 16), info, self._ts(),
                            self.peer_sockid if dest is None else dest) + cif)

    def _handshake(self, req, cookie, dest=0):
        cif = struct.pack(">8i", 4, 1, self.isn, MSS, FLIGHT, req, self.sockid, cookie)
        return self._ctrl(T_HANDSHAKE, cif + _ipfield(self.ip), dest=dest)

    def _log(self, *a):
        if self.verbose:
            print("  [udt]", *a, flush=True)

    # ---------------------------------------------------------------- connect
    def connect(self, timeout=8.0):
        deadline = time.time() + timeout
        cookie, stage = 0, 1
        last_send = 0.0
        while time.time() < deadline:
            if time.time() - last_send > 0.4:
                self.sock.sendto(self._handshake(1 if stage == 1 else -1, cookie, dest=0),
                                 (self.ip, self.port))
                last_send = time.time()
            try:
                d, _a = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            if len(d) < 64:
                continue
            w0 = struct.unpack_from(">I", d, 0)[0]
            if not (w0 & CTRL) or ((w0 >> 16) & 0x7FFF) != T_HANDSHAKE:
                continue
            cif = struct.unpack_from(">8i", d, 16)
            req, psock, ck = cif[5], cif[6], cif[7]
            self._log(f"rx handshake req={req} sock={psock:#x} cookie={ck}")
            if req == 1 and ck != 0:
                cookie, stage = ck, 2
                last_send = 0.0
            elif req == -1:
                self.peer_sockid = psock & 0xFFFFFFFF
                self._log(f"connected: peer sockid {self.peer_sockid:#x}")
                return True
        return False

    # ------------------------------------------------------------------- data
    def send(self, payload):
        """Send one payload as a solo UDT message (fits in one packet)."""
        hdr = struct.pack(">IIII", self.seq & 0x7FFFFFFF,
                          0xC0000000 | (self.msgno & 0x1FFFFFFF),
                          self._ts(), self.peer_sockid)
        self.sock.sendto(hdr + payload, (self.ip, self.port))
        self._log(f"tx data seq={self.seq} msg={self.msgno} len={len(payload)}")
        self.seq = (self.seq + 1) & 0x7FFFFFFF
        self.msgno += 1

    def pump(self, secs):
        """Collect incoming data payloads for `secs`, answering ACKs. Returns bytes."""
        out = bytearray()
        end = time.time() + secs
        while time.time() < end:
            try:
                d, _a = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            if len(d) < 16:
                continue
            w0, w1, _ts, _dst = struct.unpack_from(">IIII", d, 0)
            if w0 & CTRL:
                ctype = (w0 >> 16) & 0x7FFF
                if ctype == T_ACK:
                    # acknowledge the ACK so the peer stops retransmitting
                    self.sock.sendto(self._ctrl(T_ACK2, info=w1), (self.ip, self.port))
                elif ctype == T_SHUTDOWN:
                    self._log("peer shutdown")
                    break
                elif ctype == T_KEEPALIVE:
                    self.sock.sendto(self._ctrl(T_KEEPALIVE), (self.ip, self.port))
                continue
            payload = d[16:]
            out += payload
            self.last_ack = (w0 & 0x7FFFFFFF) + 1
            self._log(f"rx data seq={w0 & 0x7FFFFFFF} len={len(payload)}")
            # ACK it
            cif = struct.pack(">iiiiii", self.last_ack, 100000, 50000, 8192, 1000, 0)
            self.sock.sendto(self._ctrl(T_ACK, cif, info=1), (self.ip, self.port))
        return bytes(out)

    def keepalive(self):
        self.sock.sendto(self._ctrl(T_KEEPALIVE), (self.ip, self.port))

    def close(self):
        try:
            self.sock.sendto(self._ctrl(T_SHUTDOWN), (self.ip, self.port))
        except Exception:
            pass
        self.sock.close()
