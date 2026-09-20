#!/usr/bin/env python3
"""Work out why Home Assistant cannot reach a WelcomeEye. Run it where HA runs.

Pure standard library on purpose - no cryptography, nothing to install - so it can
be run inside the Home Assistant container or on any box on the network:

    python3 diagnose.py 192.168.1.50

It checks, in order: whether the host answers at all, whether TCP reaches it,
whether anything is listening on the discovery port, and whether discovery actually
replies. That last one is what setup depends on, because the UDT port is randomised
and rotates, so discovery is the only way to learn the current one.
"""
from __future__ import annotations

import socket
import struct
import subprocess
import sys


def tlv(type_: int, value: bytes) -> bytes:
    return struct.pack("<HH", type_, len(value)) + value


DISCOVERY = tlv(0xA007, b"\x00" * 32)


def ping(host: str) -> bool | None:
    """True/False, or None if ping could not be run at all."""
    print("1. does the host answer ping?")
    try:
        r = subprocess.run(["ping", "-c", "2", "-W", "2", host],
                           capture_output=True, text=True, timeout=15)
    except Exception as err:                                   # noqa: BLE001
        print(f"   could not run ping ({err}); skipping")
        return None
    ok = r.returncode == 0
    print(f"   {'yes' if ok else 'NO - no reply'}")
    if not ok:
        print("   -> the device is off, on another subnet, or filtered.")
    return ok


def tcp(host: str) -> None:
    print("2. does TCP reach it? (6800 is its control port)")
    for port in (6800, 6683):
        s = socket.socket()
        s.settimeout(4)
        try:
            s.connect((host, port))
            print(f"   {port}: open")
        except Exception as err:                               # noqa: BLE001
            print(f"   {port}: {type(err).__name__}")
        finally:
            s.close()


def udp_listening(host: str, reachable: bool | None) -> None:
    print("3. is anything listening on UDP 1500?")
    if reachable is False:
        print("   skipped - the host did not answer at step 1, so silence here")
        print("   would tell us nothing.")
        return
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.connect((host, 1500))
        s.send(b"\x00" * 8)
        try:
            s.recv(2048)
            print("   yes - it even answered a junk probe")
        except ConnectionRefusedError:
            print("   NO - ICMP port unreachable, nothing is bound to UDP 1500")
        except socket.timeout:
            print("   probably - silence rather than an ICMP rejection usually means\n"
                  "   something is bound, given the host itself answered")
    except Exception as err:                                   # noqa: BLE001
        print(f"   inconclusive: {type(err).__name__} {err}")
    finally:
        s.close()


def discovery(host: str) -> bool:
    print("4. does discovery reply? (this is what setup needs)")
    answered = False
    for label, target, broadcast in (("unicast", host, False),
                                     ("broadcast", "255.255.255.255", True)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(4)
        if broadcast:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("0.0.0.0", 0))
            s.sendto(DISCOVERY, (target, 1500))
            data, addr = s.recvfrom(4096)
            answered = True
            print(f"   {label}: reply from {addr[0]}:{addr[1]}, {len(data)} bytes")
        except socket.timeout:
            print(f"   {label}: no reply")
        except Exception as err:                               # noqa: BLE001
            print(f"   {label}: {type(err).__name__} {err}")
        finally:
            s.close()
    return answered


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    print(f"Checking {host} from {socket.gethostname()}\n")
    reachable = ping(host)
    tcp(host)
    udp_listening(host, reachable)
    ok = discovery(host)
    print("\nVerdict:")
    if ok:
        print("  Discovery works from here, so the integration should set up.")
        print("  If it still fails, Home Assistant is on a different network than")
        print("  this shell - a container without host networking, for instance.")
    else:
        print("  Discovery does not reply from here, which is why setup fails.")
        print("  If steps 1-3 passed, the device is reachable but its discovery")
        print("  service is not answering: power-cycle it and run this again.")
        print("  If step 1 failed, Home Assistant cannot reach the device at all.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
