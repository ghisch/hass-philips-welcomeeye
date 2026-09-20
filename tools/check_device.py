#!/usr/bin/env python3
"""Exercise the integration's device layer against a real intercom, without HA.

The integration package imports Home Assistant, which is far too heavy to install
just to test the protocol half. This loads the modules that do the device work
under a stub package, so relative imports still resolve.

    uv run --with cryptography python3 tools/check_device.py --ip 192.168.1.50 \
        --gid cgab12cd34
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "philips_welcomeeye"


def load_device_module() -> types.ModuleType:
    """Import device.py without running the package __init__ (which needs HA)."""
    pkg = types.ModuleType("pweye")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["pweye"] = pkg
    spec = importlib.util.spec_from_file_location("pweye.device", PKG_DIR / "device.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pweye.device"] = module
    spec.loader.exec_module(module)
    return module


def read_env(name: str) -> str | None:
    env = ROOT / ".env"
    if not env.is_file():
        return os.environ.get(name)
    for line in env.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default=read_env("GLNK_IP"))
    p.add_argument("--gid", default=read_env("GLNK_GID"))
    p.add_argument("--user", default=read_env("GLNK_USER") or "admin")
    p.add_argument("--punch-port", type=int, default=None)
    p.add_argument("--stream-seconds", type=int, default=6)
    a = p.parse_args()
    password = read_env("GLNK_PASSWORD")
    if not (a.ip and a.gid and password):
        return p.error("need --ip, --gid and GLNK_PASSWORD (in .env)")

    dev_mod = load_device_module()
    device = dev_mod.WelcomeEyeDevice(a.ip, a.gid, a.user, password,
                                      punch_port=a.punch_port)

    print("probe()")
    try:
        for key, value in device.probe().items():
            print(f"  {key}: {value}")
    except dev_mod.WelcomeEyeError as err:
        print(f"  FAILED: {err}")
        return 1

    print("\nstream_url() while the stream switch is off")
    print(f"  {device.stream_url()}   (None is correct: off by default)")

    print("\nstream_url() with streaming enabled")
    device.set_stream_enabled(True)
    url = device.stream_url()
    print(f"  {url}")
    out = ROOT / "tools" / "_check.ts"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", url, "-t", str(a.stream_seconds),
         "-c", "copy", "-f", "mpegts", "-y", str(out)],
        capture_output=True, text=True)
    print(f"  ffmpeg exit={result.returncode} {result.stderr.strip()[:200]}")
    print(f"  frames={device.stats.frames} bytes={device.stats.bytes_sent} "
          f"error={device.stats.last_error}")
    device.set_stream_enabled(False)
    print(f"  after switching off: stream_url() = {device.stream_url()}")

    if out.is_file():
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration",
             "-of", "default=noprint_wrappers=1", str(out)],
            capture_output=True, text=True)
        print("  " + probe.stdout.strip().replace("\n", "  "))
        out.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
