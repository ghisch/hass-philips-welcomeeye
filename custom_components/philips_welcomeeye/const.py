"""Constants for the Philips WelcomeEye integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "philips_welcomeeye"

CONF_GID: Final = "gid"
CONF_PUNCH_PORT: Final = "punch_port"

#: The device's own channel numbering. The login mask is 1 << channel and the
#: startAV selector is the same number; a session that logs in on one channel and
#: asks for another has every encrypted command dropped, silently.
CHANNEL_DOOR: Final = "door"
CHANNEL_CAM: Final = "cam"
CHANNELS: Final = [CHANNEL_DOOR, CHANNEL_CAM]

#: The gate relay. Channel 0 is the door strike, which this hardware does not wire.
LOCK_CHANNEL_GATE: Final = 1

DEFAULT_USERNAME: Final = "admin"

#: The device serves ONE channel at a time and the incumbent keeps it, so a second
#: viewer on the other channel gets nothing - including the indoor monitor.
#: Streaming therefore stays off until explicitly enabled.
DEFAULT_STREAM_ENABLED: Final = False

CONF_PASSWORD_IS_DERIVED: Final = "password_is_derived"
CONF_LOCK_CHANNEL: Final = "lock_channel"
CONF_AV_WAIT: Final = "av_wait"

#: Seconds to wait for door video before refusing to send an unlock. This is a
#: deadline, not a delay: the unlock fires the moment video arrives, which measured
#: consistently at about 1.3 s. It only bounds how long a doomed attempt waits
#: before failing, so a few seconds of headroom costs nothing on success.
DEFAULT_AV_WAIT: Final = 5.0

#: Last punch port that worked, remembered in the config entry. The port is
#: randomised, so this is a hint and never a guarantee: a session tries it first
#: and falls back to discovery when it is stale. It makes a restart survive a
#: discovery probe that goes missing.
CONF_LAST_PORT: Final = "last_punch_port"
