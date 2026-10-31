"""System-level microphone mute/unmute, per platform.

Used by Skin mode to mute the physical input device while TTS is playing back,
preventing the program from accidentally transcribing its own speech output.

Windows
-------
``pycaw`` (via ``comtypes``/COM) mutates the ``IAudioEndpointVolume`` of the
capture endpoint whose FriendlyName matches the PortAudio device name.

Linux
-----
The same state lives in PipeWire/pulse, so ``pactl`` does the job: read the
source's current mute flag from ``pactl -f json list sources``, then
``pactl set-source-mute <name> 1``. Sources are addressed by *name* (indices
renumber whenever nodes come and go), and the PortAudio device name is matched
against the source's ``description`` because that is what clients display.

If the backend is unavailable (missing pycaw on Windows, no pactl/PipeWire on
Linux) every call is a silent no-op so the rest of the application keeps
working.

Typical usage::

    token = mute(device_index)   # mute before TTS plays
    try:
        play_tts(...)
    finally:
        unmute(token)            # always restore, even on error
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from . import logutil

log = logutil.get(__name__)

_IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_endpoint_volume(device_index: int | None):
    """Return the ``IAudioEndpointVolume`` COM pointer for the PortAudio input
    device at *device_index*, or ``None`` on any failure.

    PortAudio device names are truncated to ~31 characters.  We match them as
    a prefix against the full Windows ``FriendlyName`` of each capture
    endpoint.
    """
    try:
        import sounddevice as sd
        from pycaw.pycaw import AudioUtilities, EDataFlow  # type: ignore[import]
    except ImportError:
        return None

    try:
        # Resolve the PortAudio device name.
        if device_index is None:
            pa_name = ""
        else:
            pa_name = sd.query_devices(device_index).get("name", "").strip().lower()

        capture_devs = AudioUtilities.GetAllDevices(data_flow=EDataFlow.eCapture.value)

        # First pass: look for a device whose FriendlyName starts with the
        # (possibly truncated) PortAudio name.
        for dev in capture_devs:
            fn = (dev.FriendlyName or "").strip().lower()
            if pa_name and fn.startswith(pa_name):
                return dev.EndpointVolume

        # Second pass: substring match (less precise, avoids missing on
        # slightly different truncation boundaries).
        for dev in capture_devs:
            fn = (dev.FriendlyName or "").strip().lower()
            if pa_name and pa_name in fn:
                return dev.EndpointVolume

        # Fallback: mute the Windows default capture device.
        log.debug(
            "micmute: could not match PortAudio device %s to a Windows "
            "endpoint; falling back to system default microphone.",
            device_index,
        )
        default_mic = AudioUtilities.GetMicrophone()
        from pycaw.pycaw import IAudioEndpointVolume  # type: ignore[import]
        from comtypes import CLSCTX_ALL  # type: ignore[import]
        from ctypes import cast, POINTER
        iface = default_mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))

    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: failed to get endpoint volume: %s", exc)
        return None


def _pactl(*args: str, timeout: float = 5.0):
    """Run pactl with *args*. Raises only when pactl itself is missing."""
    exe = shutil.which("pactl")
    if exe is None:
        raise FileNotFoundError("pactl not found")
    return subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=timeout, check=False,
    )


def _sources_from_json(payload: str) -> list[dict]:
    """Parse ``pactl -f json list sources`` output. Never raises."""
    try:
        data = json.loads(payload or "[]")
    except (ValueError, TypeError):
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def _capture_sources(sources: list[dict]) -> list[dict]:
    """Drop monitor sources; they are outputs being replayed, not microphones."""
    return [s for s in sources if not str(s.get("name", "")).endswith(".monitor")]


def pick_source(sources: list[dict], portaudio_name: str) -> str | None:
    """Return the pulse source name best matching *portaudio_name*.

    PortAudio shows the source *description*, sometimes truncated, so compare
    against the description: exact, then prefix, then substring.
    """
    wanted = (portaudio_name or "").strip().lower()
    candidates = _capture_sources(sources)
    if not candidates or not wanted:
        return None

    def desc(src: dict) -> str:
        return str(src.get("description", "")).strip().lower()

    for match in (
        lambda d: d == wanted,
        lambda d: d.startswith(wanted),
        lambda d: wanted in d,
        # PortAudio truncates names to ~31 chars, so a long description never
        # prefix-matches its own truncated form.
        lambda d: d.startswith(wanted[:24]),
    ):
        for src in candidates:
            if match(desc(src)):
                return str(src.get("name"))
    return None


def _linux_source_name(device_index: int | None) -> str | None:
    """Resolve a PortAudio input index to a pulse source name."""
    pa_name = ""
    if device_index is not None:
        try:
            import sounddevice as sd
            pa_name = str(sd.query_devices(device_index).get("name", ""))
        except Exception as exc:  # noqa: BLE001
            log.debug("micmute: could not resolve PortAudio device %s: %s",
                      device_index, exc)

    try:
        proc = _pactl("-f", "json", "list", "sources")
        if proc.returncode == 0:
            name = pick_source(_sources_from_json(proc.stdout), pa_name)
            if name:
                return name
        else:
            log.debug("micmute: pactl list sources failed: %s",
                      (proc.stderr or "").strip())
    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: could not list sources: %s", exc)
        return None

    try:
        proc = _pactl("get-default-source")
        name = (proc.stdout or "").strip()
        if name:
            log.debug("micmute: falling back to the default source %s", name)
            return name
    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: could not read the default source: %s", exc)
    return None


def _linux_is_muted(source: str) -> bool | None:
    """Current mute state of *source*, or None when it cannot be read."""
    try:
        proc = _pactl("-f", "json", "list", "sources")
        if proc.returncode != 0:
            return None
        for src in _sources_from_json(proc.stdout):
            if src.get("name") == source:
                return bool(src.get("mute"))
    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: could not read mute state: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class MuteToken:
    """Opaque token returned by :func:`mute`; pass to :func:`unmute`."""

    def __init__(self, endpoint_volume, was_already_muted: bool) -> None:
        self._vol = endpoint_volume
        self._was_already_muted = was_already_muted


class LinuxMuteToken(MuteToken):
    """Token for the pactl backend: ``_vol`` holds the pulse source name."""

    def __init__(self, source: str, was_already_muted: bool) -> None:
        super().__init__(source, was_already_muted)
        self.source = source


def _win_mute(device_index: int | None) -> MuteToken | None:
    vol = _get_endpoint_volume(device_index)
    if vol is None:
        return None
    try:
        was_already_muted = bool(vol.GetMute())
        if not was_already_muted:
            vol.SetMute(1, None)
            log.debug("micmute: muted device index=%s", device_index)
        else:
            log.debug(
                "micmute: device index=%s was already muted; skipping", device_index
            )
        return MuteToken(vol, was_already_muted)
    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: SetMute failed: %s", exc)
        return None


def _linux_mute(device_index: int | None) -> MuteToken | None:
    source = _linux_source_name(device_index)
    if not source:
        return None
    state = _linux_is_muted(source)
    if state is None:
        return None
    if state:
        log.debug("micmute: source %s was already muted; skipping", source)
        return LinuxMuteToken(source, True)
    try:
        proc = _pactl("set-source-mute", source, "1")
        if proc.returncode != 0:
            log.debug("micmute: set-source-mute failed: %s",
                      (proc.stderr or "").strip())
            return None
    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: set-source-mute failed: %s", exc)
        return None
    log.debug("micmute: muted source %s", source)
    return LinuxMuteToken(source, False)


def mute(device_index: int | None = None) -> MuteToken | None:
    """Mute the microphone endpoint corresponding to *device_index*.

    Returns a :class:`MuteToken` that should be passed to :func:`unmute` to
    restore the previous state.  Returns ``None`` if the backend is unavailable
    or the device could not be found (the caller can still safely pass ``None``
    to :func:`unmute`).
    """
    if _IS_LINUX:
        return _linux_mute(device_index)
    return _win_mute(device_index)


def unmute(token: MuteToken | None) -> None:
    """Restore the microphone to its state before :func:`mute` was called.

    If the device was already muted before we touched it, we leave it muted.
    Safe to call with ``None`` (no-op).
    """
    if token is None:
        return
    if token._was_already_muted:
        log.debug("micmute: device was pre-muted; leaving muted")
        return

    if isinstance(token, LinuxMuteToken):
        try:
            proc = _pactl("set-source-mute", token.source, "0")
            if proc.returncode != 0:
                log.debug("micmute: unmute failed: %s", (proc.stderr or "").strip())
                return
            log.debug("micmute: unmuted source %s", token.source)
        except Exception as exc:  # noqa: BLE001
            log.debug("micmute: unmute failed: %s", exc)
        return

    try:
        token._vol.SetMute(0, None)
        log.debug("micmute: unmuted device")
    except Exception as exc:  # noqa: BLE001
        log.debug("micmute: SetMute(0) failed: %s", exc)
