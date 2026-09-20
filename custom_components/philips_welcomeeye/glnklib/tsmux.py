"""Minimal MPEG-TS muxer, just enough to carry H264 with real timestamps.

The device gives every frame a millisecond timestamp in TLV 99. A raw Annex-B
elementary stream has nowhere to put it, so consumers invent a constant frame rate
and get the duration wrong - measured 38% short over 20 s, because the stream is
variable rate (intervals cluster at 40 ms and 80 ms, i.e. 25 fps with drops).

MPEG-TS has a PTS field, and go2rtc's pipe transport accepts MPEG-TS, so muxing here
keeps the device's own clock end to end and still never re-encodes anything.
"""
import struct

PID_PAT, PID_PMT, PID_VIDEO = 0x0000, 0x1000, 0x0100
STREAM_TYPE_H264 = 0x1B
PACKET = 188


def crc32_mpeg(data):
    """MPEG-2 CRC-32: poly 0x04C11DB7, init all ones, MSB first, no final xor."""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
    return crc


def _pts_field(prefix, pts):
    """The 5-byte PTS/DTS encoding: 4 marker bits then 33 value bits split by markers."""
    pts &= (1 << 33) - 1
    return bytes([
        (prefix << 4) | (((pts >> 30) & 0x07) << 1) | 1,
        (pts >> 22) & 0xFF,
        (((pts >> 15) & 0x7F) << 1) | 1,
        (pts >> 7) & 0xFF,
        ((pts & 0x7F) << 1) | 1,
    ])


class TsMuxer:
    def __init__(self, write):
        self._write = write
        self._cc = {PID_PAT: 0, PID_PMT: 0, PID_VIDEO: 0}
        self._since_si = 1 << 30      # force PAT/PMT before the first frame

    # ------------------------------------------------------------- TS packets
    def _next_cc(self, pid):
        c = self._cc[pid]
        self._cc[pid] = (c + 1) & 0x0F
        return c

    def _section(self, pid, section):
        """One PSI section in a single TS packet (they are small enough)."""
        payload = b"\x00" + section          # pointer_field
        body = payload.ljust(PACKET - 4, b"\xff")
        self._write(bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF,
                           0x10 | self._next_cc(pid)]) + body)

    def _pat(self):
        s = bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
                   0x00, 0x01, 0xE0 | (PID_PMT >> 8), PID_PMT & 0xFF])
        self._section(PID_PAT, s + struct.pack(">I", crc32_mpeg(s)))

    def _pmt(self):
        s = bytes([0x02, 0xB0, 0x12, 0x00, 0x01, 0xC1, 0x00, 0x00,
                   0xE0 | (PID_VIDEO >> 8), PID_VIDEO & 0xFF,   # PCR PID
                   0xF0, 0x00,                                   # program_info_length
                   STREAM_TYPE_H264,
                   0xE0 | (PID_VIDEO >> 8), PID_VIDEO & 0xFF,
                   0xF0, 0x00])                                  # ES_info_length
        self._section(PID_PMT, s + struct.pack(">I", crc32_mpeg(s)))

    @staticmethod
    def _pcr(pcr90k):
        base = pcr90k & ((1 << 33) - 1)
        return bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                      (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E, 0x00])

    # ------------------------------------------------------------------ frame
    def write_frame(self, payload, pts_90k, keyframe=False):
        """One access unit as a PES packet, split across TS packets."""
        if keyframe or self._since_si >= 20:
            self._pat(); self._pmt(); self._since_si = 0
        self._since_si += 1

        # PES header. Length 0 = unbounded, which is normal and legal for video.
        pes = (b"\x00\x00\x01\xe0" + b"\x00\x00" + b"\x80\x80\x05"
               + _pts_field(0b0010, pts_90k) + bytes(payload))

        first, off = True, 0
        while off < len(pes):
            hdr3 = bytes([0x47, (0x40 if first else 0x00) | (PID_VIDEO >> 8),
                          PID_VIDEO & 0xFF])
            room = PACKET - 4
            af = b""
            if first:
                af = b"\x50" + self._pcr(pts_90k)      # flags: PCR present
                room -= len(af) + 1
            left = len(pes) - off
            if left < room:
                # pad with an adaptation field so the packet stays exactly 188 bytes
                need = room - left
                if af:
                    af = bytes([af[0]]) + af[1:] + b"\xff" * need
                elif need == 1:
                    af = b""            # a lone stuffing byte is the length byte itself
                    room -= 1
                else:
                    af = b"\x00" + b"\xff" * (need - 2)
                    room -= 1
                chunk = pes[off:off + min(left, room)]
            else:
                chunk = pes[off:off + room]
            afc = 0x30 if af else 0x10
            pkt = hdr3 + bytes([afc | self._next_cc(PID_VIDEO)])
            if af:
                pkt += bytes([len(af)]) + af
            pkt += chunk
            pkt = pkt.ljust(PACKET, b"\xff")[:PACKET]
            self._write(pkt)
            off += len(chunk)
            first = False
