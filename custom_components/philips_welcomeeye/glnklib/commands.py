"""Encrypted command envelope for glnk devices (TLV 505 unlock, 5007 startAV, ...).

Reversed from DataChannelIOCtrl::lockReq in libglnkio.so and confirmed against a live
capture of the Philips WelcomeEye app.

Envelope, then RC4 with the device GID:

    tokenA(16) | AES-128-CFB(body) | ... no -- field order is:
    tokenA(16) | tokenB(16) | AES-128-CFB(body) | tokenC(16)

    key = tokenA[8:14] + tokenB[4:9]     (11 bytes, zero padded to 16)
    iv  = tokenC[4:15]                   (11 bytes, zero padded to 16)

Tokens are 16-byte LangTao random strings satisfying checkRandData(posA=5, posB=9,
mode=1 uppercase) -- distinct from the login's checkRandDataForLogin(6, 10, 1).

Command body = u32 device_time | u32 zero | <command struct>
For the unlock the struct is the 44-byte _TLV_V_Lock_Req.
"""
import struct, time
from .protocol import aes_cfb, rc4, gen_rand, check_rand, tlv, owsp

CMD_POSA, CMD_POSB, CMD_MODE = 5, 9, 1

TLV_LOCK_REQ_ENC = 505
TLV_LOCK_RSP_ENC = 506
TLV_START_AV_ENC = 5007
TLV_PRE_STOP = 5005
TLV_PRIVATE_REQ = 509
TLV_PRIVATE_RSP = 510

#: Each message type slices its AES key out of different byte ranges of its tokens.
#: (key_a_slice, key_b_slice, iv_c_slice). Recovered by known-plaintext search over
#: the capture - see crack_recipe.py; the body always starts u32 time | u32 zero.
RECIPES = {
    TLV_LOCK_REQ_ENC:  ((8, 14), (4, 9),  (4, 15)),
    TLV_START_AV_ENC:  ((3, 12), (6, 11), (4, 15)),
}


def cmd_token():
    return gen_rand(16, CMD_POSA, CMD_POSB, CMD_MODE)


def cmd_keys(a, b, c, tlv_type=TLV_LOCK_REQ_ENC):
    (ka, kb), (kc, kd), (ie, if_) = RECIPES[tlv_type]
    return ((a[ka:kb] + b[kc:kd]).ljust(16, b"\x00")[:16],
            c[ie:if_].ljust(16, b"\x00")[:16])


def wrap_cmd(gid, body, tlv_type=TLV_LOCK_REQ_ENC):
    """Build the RC4+AES envelope for an encrypted command payload."""
    a, b, c = cmd_token(), cmd_token(), cmd_token()
    key, iv = cmd_keys(a, b, c, tlv_type)
    return rc4(gid.encode(), a + b + aes_cfb(key, iv, body) + c)


def unwrap_cmd(gid, v, tlv_type=TLV_LOCK_REQ_ENC):
    """Inverse of wrap_cmd - returns (tokens, plaintext body)."""
    inner = rc4(gid.encode(), v)
    a, b, c = inner[0:16], inner[16:32], inner[-16:]
    key, iv = cmd_keys(a, b, c, tlv_type)
    return (a, b, c), aes_cfb(key, iv, inner[32:-16], decrypt=True)


#: startAV selector seen in the capture. The app alternates the two; both captured
#: unlocks were issued on a session opened with DOOR.
AV_DOOR, AV_CAM = 0x10, 0x12


def start_av_body(dev_time, sel=AV_DOOR):
    """u32 time | u32 0 | u8 sel | u8 1 | u8 2 | u8 0  == 12 bytes."""
    return struct.pack("<IIBBBB", int(dev_time), 0, sel, 1, 2, 0)


def start_av_frame(gid, dev_time, sel=AV_DOOR):
    body = start_av_body(dev_time, sel)
    return owsp(tlv(TLV_START_AV_ENC, wrap_cmd(gid, body, TLV_START_AV_ENC))), body


def lock_body(lock_pwd, channel=1, action=1, delay=0, device_id=0, dev_time=None):
    """u32 time | u32 0 | 44-byte _TLV_V_Lock_Req  == 52 bytes.

    channel 1 is the gate, 0 the door (absent on this unit). `action` is INERT on
    this hardware: action=1 sent twice opened then closed the gate, so the command
    just pulses the relay and the controller toggles. Kept for fidelity with the
    struct, not because it selects a direction.
    """
    body = bytearray(52)
    struct.pack_into("<I", body, 0, int(dev_time if dev_time is not None else time.time()))
    struct.pack_into("<I", body, 8, device_id)
    p = lock_pwd.encode()[:32]
    body[12:12 + len(p)] = p
    struct.pack_into("<I", body, 44, delay)
    body[48] = channel
    body[49] = action
    return bytes(body)


def unlock_frame(gid, lock_pwd, channel=1, action=1, delay=0, dev_time=None):
    body = lock_body(lock_pwd, channel, action, delay, dev_time=dev_time)
    return owsp(tlv(TLV_LOCK_REQ_ENC, wrap_cmd(gid, body))), body


#: The response envelope carries only TWO tokens - u32 | A(16) | ct | C(16) - so the
#: request recipe cannot apply. Recovered from the live 506 and the captured one
#: together; only this recipe satisfies both.
RSP_RECIPE = ((9, 12), (2, 12), (4, 13))   # key = A[9:12] + C[2:12], iv = A[4:13]


def parse_lock_rsp(gid, v):
    """TLV 506. Returns {time, result, extra}.

    CAUTION: result=1 only means the device accepted and decrypted the command. It
    is NOT proof the gate moved - an unlock sent without a live A/V stream on the
    door channel returns result=1 and does nothing at all. Verified 2026-08-31.
    """
    inner = rc4(gid.encode(), v)
    a, c = inner[4:20], inner[-16:]
    ct = inner[20:-16]
    (ka, kb), (kc, kd), (ie, if_) = RSP_RECIPE
    key = (a[ka:kb] + c[kc:kd]).ljust(16, b"\x00")[:16]
    iv = a[ie:if_].ljust(16, b"\x00")[:16]
    body = aes_cfb(key, iv, ct, decrypt=True)
    return {"time": struct.unpack_from("<I", body, 0)[0],
            "result": struct.unpack_from("<H", body, 8)[0],
            "extra": struct.unpack_from("<H", body, 10)[0],
            "body": body}


def describe_lock_body(b):
    return {
        "time": struct.unpack_from("<I", b, 0)[0],
        "deviceId": struct.unpack_from("<I", b, 8)[0],
        "lockPwd": b[12:44].split(b"\x00")[0].decode(errors="replace"),
        "delay": struct.unpack_from("<I", b, 44)[0],
        "channel": b[48],
        "action": b[49],
    }


# ---------------------------------------------------------------- private channel
#: TLV 509/510 carry only TWO tokens, so the 3-token command recipe does not apply.
#: 509 = A(16)|ct|C(16);  510 = u32|A(16)|ct|C(16). Recovered by known-plaintext
#: search over four captured samples each; exactly one recipe fits all of them.
PRIV_REQ_RECIPE = ((3, 11), (6, 11), (3, 11))   # key = A[3:11]+C[6:11], iv = C[3:11]
PRIV_RSP_RECIPE = ((4, 13), (5, 10), (3, 9))    # key = A[4:13]+C[5:10], iv = C[3:9]

#: The body is a nested sub-protocol, not a flat struct:
#:     u32 device_time | u32 0 | u32 payload_len | <inner OWSP TLVs>
#: Inner types seen: 437/438 (a get/answer pair, arg `01 04 <id> 00`) and 469/470
#: (470 returns the device JSON). The app only ever asks ids 02, 03, 05, 07, 0a.
PRIV_GET, PRIV_GET_RSP = 437, 438
PRIV_INFO, PRIV_INFO_RSP = 469, 470
PRIV_KNOWN_IDS = (0x02, 0x03, 0x05, 0x07, 0x0a)


def _priv_keys(a, c, recipe):
    (ka, kb), (kc, kd), (ie, if_) = recipe
    return ((a[ka:kb] + c[kc:kd]).ljust(16, b"\x00")[:16],
            c[ie:if_].ljust(16, b"\x00")[:16])


def priv_body(dev_time, inner):
    """u32 time | u32 0 | u32 len | inner TLV bytes."""
    return struct.pack("<III", int(dev_time), 0, len(inner)) + inner


def priv_get(dev_time, item_id):
    """Build the body for the 437 'get' the app uses, for one item id."""
    return priv_body(dev_time, tlv(PRIV_GET, bytes([1, 4, item_id, 0])))


def wrap_priv(gid, body):
    a, c = cmd_token(), cmd_token()
    key, iv = _priv_keys(a, c, PRIV_REQ_RECIPE)
    return rc4(gid.encode(), a + aes_cfb(key, iv, body) + c)


def unwrap_priv_rsp(gid, v):
    """Decode TLV 510 -> (time, [(inner_tlv_type, value), ...])."""
    inner = rc4(gid.encode(), v)
    a, c, ct = inner[4:20], inner[-16:], inner[20:-16]
    key, iv = _priv_keys(a, c, PRIV_RSP_RECIPE)
    body = aes_cfb(key, iv, ct, decrypt=True)
    t, _z, ln = struct.unpack_from("<III", body, 0)
    payload, out, off = body[12:12 + ln], [], 0
    while len(payload) - off >= 4:
        ty, l = struct.unpack_from("<HH", payload, off)
        if off + 4 + l > len(payload):
            break
        out.append((ty, payload[off + 4:off + 4 + l]))
        off += 4 + l
    return t, out
