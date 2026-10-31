"""The virtual audio cable: VB-Cable's job, done with PipeWire nodes.

On Windows the "cable" is a third-party driver the user installs, and the app
only has to *find* it. Linux has no such driver, so this module creates one on
demand:

    TTS output  ->  null sink "speechskin"  ->  its ".monitor" source
                                                (what chat apps pick as a mic)

``pactl`` is the control surface (pipewire-pulse implements it); the node it
creates is immediately visible to PortAudio, which is what the rest of the app
enumerates. Everything degrades to a silent no-op when pactl or PipeWire is
missing, so importing this module is always safe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from . import logutil

log = logutil.get(__name__)

_IS_LINUX = sys.platform.startswith("linux")

# Internal PipeWire node name. Clients display the *description*, which is what
# config.CABLE_DEVICE_KEYWORD is matched against.
CABLE_SINK_NAME = "speechskin"
CABLE_DESCRIPTION = "SpeechSkin"

# Displayed as "Monitor of <description>" in clients.
CABLE_MONITOR_SOURCE = f"{CABLE_SINK_NAME}.monitor"


def _pactl(*args: str, timeout: float = 5.0):
    """Run pactl with *args*. Raises only if pactl itself is missing."""
    exe = shutil.which("pactl")
    if exe is None:
        raise FileNotFoundError("pactl not found")
    return subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=timeout, check=False,
    )


def parse_sinks(payload: str) -> list[dict]:
    """Parse the JSON from ``pactl -f json list sinks``. Never raises."""
    try:
        data = json.loads(payload or "[]")
    except (ValueError, TypeError):
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def list_sinks() -> list[dict]:
    """Return the current sinks as parsed JSON dicts ([] when unavailable)."""
    if not _IS_LINUX:
        return []
    try:
        proc = _pactl("-f", "json", "list", "sinks")
    except Exception as exc:  # noqa: BLE001
        log.debug("could not list sinks: %s", exc)
        return []
    if proc.returncode != 0:
        log.debug("pactl list sinks failed: %s", (proc.stderr or "").strip())
        return []
    return parse_sinks(proc.stdout)


def is_present() -> bool:
    """True when a sink named :data:`CABLE_SINK_NAME` already exists."""
    return any(s.get("name") == CABLE_SINK_NAME for s in list_sinks())


def ensure() -> bool:
    """Create the virtual cable if it is missing.

    Returns ``True`` only when this call *created* a node, i.e. the caller
    should re-enumerate audio devices to see it. ``False`` means nothing
    changed: already present, not Linux, or pactl/PipeWire unavailable.
    """
    if not _IS_LINUX:
        return False
    if is_present():
        return False
    try:
        proc = _pactl(
            "load-module", "module-null-sink",
            f"sink_name={CABLE_SINK_NAME}",
            f"sink_properties=device.description={CABLE_DESCRIPTION}",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not create the virtual cable: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "creating the virtual cable failed (%s): %s",
            proc.returncode, (proc.stderr or "").strip(),
        )
        return False
    log.info(
        "created PipeWire null sink '%s' (%s); use '%s' as the microphone in "
        "your voice-chat app", CABLE_SINK_NAME, proc.stdout.strip(),
        CABLE_MONITOR_SOURCE,
    )
    return True
