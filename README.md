# Philips WelcomeEye for Home Assistant

Local control of a Philips WelcomeEye / CFI Extel video intercom — gate and camera —
without the vendor cloud.

The device speaks a proprietary protocol (GooLink/LangTao "glnk", wrapped by the
Quvii SDK) over UDT on a randomised UDP port. It was reverse engineered from the
Android app; `custom_components/philips_welcomeeye/glnklib/` carries the client and
documents the wire format in its docstrings.

## What you get

| Entity | Notes |
|---|---|
| **Gate** (button) | Pulses the gate relay |
| **Camera** | H.264, passed through untouched — nothing is transcoded |
| **Stream** (switch) | Master enable for streaming. **Off by default** |
| **Camera source** (select) | Door station or the second camera |
| Diagnostics (sensors) | Clock offset, UDT port, model, viewers, frames, last gate command, last stream error |

## Three device behaviours worth knowing

**The device serves one channel at a time, and whoever got there first keeps it.**
A stream held open by Home Assistant takes the picture away from your indoor
monitor. That is why the stream switch exists and why it defaults to off. Nothing
touches the device until you enable it, and then only while something is watching.

**The gate needs live video on the door channel.** Without it the device accepts the
command and does nothing. The integration waits for door video and refuses to send
otherwise, so a press either works or fails loudly.

Because of the one-channel rule above, opening the gate has to borrow the door
channel. It does that and gives it straight back: viewers are dropped for a couple
of seconds and reconnect to whatever source was selected, and if the stream was off
it stays off. Nothing you had set changes.

The wait for video is a **deadline, not a delay** — the gate fires as soon as video
arrives, about 1.3 s in testing. Lowering it does not make the gate open sooner, it
only makes a doomed attempt give up faster. The default is 5 s.

**There is no gate state to read.** The device has no relay of its own; it pulses an
external gate controller, which toggles. Hence a button, not a cover — an optimistic
open/closed entity would drift out of sync with the real gate.

## Requirements

Home Assistant must be able to reach the intercom by **UDP broadcast**, because the
UDT port is randomised and rotates often — in testing it moved twice in one evening.
Discovery is the only practical way to learn the current one. In practice this means
Home Assistant on the same LAN as the intercom. Across a VPN or a routed subnet the
broadcast does not arrive, and the fixed-port option in the integration's settings is
a stopgap that will stop working the next time the port changes.

## Installation

1. HACS → three-dot menu → **Custom repositories**, add this repository as an
   *Integration*.
2. Install, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Philips WelcomeEye.**

Pick your intercom from the discovered list, or enter its address and device id
(GID) manually. Then sign in with the same credentials you use in the app.

> If you recovered the 8-character *derived* password rather than the one you type
> into the app, tick "This password is already in the device's derived form".

## Options

- **Gate relay channel** — which relay the gate is wired to. 1 on the units seen so
  far; 0 is the door strike.
- **Seconds to wait for video** — how long to wait for door video before refusing.
- **Fixed UDT port** — only for paths where discovery cannot reach the device.

## Credits

Reverse engineered from `com.extel.philipswelcomeeye` (Quvii SDK wrapping the
GooLink/LangTao "glnk" stack). Not affiliated with Philips or CFI Extel.
