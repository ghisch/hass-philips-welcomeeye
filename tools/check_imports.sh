#!/bin/sh
# Import every integration module against a real Home Assistant.
#
# Worth running before any release: it caught the integration importing CFB from a
# cryptography path that exists locally but not in the version Home Assistant ships,
# which broke all eleven modules and would have failed for every user.
cd "$(dirname "$0")/.." || exit 1
uv run --with homeassistant --with numpy --with av python3 - <<'PY'
import importlib
import sys

sys.path.insert(0, "custom_components")
MODULES = [
    "philips_welcomeeye", "philips_welcomeeye.const", "philips_welcomeeye.device",
    "philips_welcomeeye.entity", "philips_welcomeeye.coordinator",
    "philips_welcomeeye.config_flow", "philips_welcomeeye.button",
    "philips_welcomeeye.camera", "philips_welcomeeye.switch",
    "philips_welcomeeye.select", "philips_welcomeeye.sensor",
    "philips_welcomeeye.diagnostics",
]
failures = 0
for name in MODULES:
    try:
        importlib.import_module(name)
        print(f"  ok   {name}")
    except Exception as err:  # noqa: BLE001 - reporting, not handling
        failures += 1
        print(f"  FAIL {name} -> {type(err).__name__}: {err}")
print(f"\n{failures} failure(s)")
raise SystemExit(1 if failures else 0)
PY
