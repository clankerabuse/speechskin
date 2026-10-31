"""Tests for the Linux port layer: virtual cable, mute backend, focus, hotkey.

These stay pure: no pactl, no compositor, no audio device is touched. The
subprocess boundary is monkeypatched, so the suite keeps passing on Windows.
"""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from tts_overlay import cable, focus, hotkey, micmute, win32utils


# ---------------------------------------------------------------------------
# cable.py
# ---------------------------------------------------------------------------

_SINKS_JSON = json.dumps([
    {"index": 57, "name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
     "description": "Built-in Audio Analog Stereo"},
    {"index": 429, "name": "speechskin", "description": "SpeechSkin"},
])


def test_parse_sinks_reads_pactl_json():
    sinks = cable.parse_sinks(_SINKS_JSON)
    assert [s["name"] for s in sinks] == [
        "alsa_output.pci-0000_00_1f.3.analog-stereo", "speechskin",
    ]


@pytest.mark.parametrize("payload", ["", "not json", "{}", "null", None])
def test_parse_sinks_is_defensive(payload):
    assert cable.parse_sinks(payload) == []


def test_ensure_is_noop_when_sink_exists(monkeypatch):
    monkeypatch.setattr(cable, "_IS_LINUX", True)
    monkeypatch.setattr(cable, "list_sinks", lambda: cable.parse_sinks(_SINKS_JSON))
    called = []
    monkeypatch.setattr(cable, "_pactl", lambda *a, **k: called.append(a))
    assert cable.ensure() is False          # nothing was created
    assert called == []                      # and nothing was loaded


def test_ensure_creates_missing_sink(monkeypatch):
    monkeypatch.setattr(cable, "_IS_LINUX", True)
    monkeypatch.setattr(cable, "list_sinks", lambda: [])
    calls = []

    def fake_pactl(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="536870916\n", stderr="")

    monkeypatch.setattr(cable, "_pactl", fake_pactl)
    assert cable.ensure() is True
    args = calls[0]
    assert args[:2] == ("load-module", "module-null-sink")
    assert f"sink_name={cable.CABLE_SINK_NAME}" in args


def test_ensure_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(cable, "_IS_LINUX", True)
    monkeypatch.setattr(cable, "list_sinks", lambda: [])
    monkeypatch.setattr(
        cable, "_pactl",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
    )
    assert cable.ensure() is False


def test_ensure_is_inert_off_linux(monkeypatch):
    monkeypatch.setattr(cable, "_IS_LINUX", False)

    def explode(*a, **k):
        raise AssertionError("pactl must not be called off Linux")

    monkeypatch.setattr(cable, "_pactl", explode)
    assert cable.ensure() is False


# ---------------------------------------------------------------------------
# micmute.py (Linux matching)
# ---------------------------------------------------------------------------

_SOURCES = [
    {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
     "description": "Monitor of Built-in Audio Analog Stereo", "mute": False},
    {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
     "description": "Built-in Audio Analog Stereo", "mute": True},
    {"name": "speechskin.monitor", "description": "Monitor of SpeechSkin",
     "mute": False},
]


def test_pick_source_excludes_monitors():
    assert micmute.pick_source(_SOURCES, "Monitor of SpeechSkin") is None


def test_pick_source_exact_and_prefix():
    assert micmute.pick_source(
        _SOURCES, "Built-in Audio Analog Stereo"
    ) == "alsa_input.pci-0000_00_1f.3.analog-stereo"


def test_pick_source_handles_portaudio_truncation():
    long_desc = [{"name": "usb_mic", "description": "Rode NT-USB Mini Microphone"}]
    # PortAudio truncates to ~31 chars; the 24-char prefix still matches.
    assert micmute.pick_source(long_desc, "Rode NT-USB Mini Microphone (2-") == "usb_mic"


def test_pick_source_returns_none_without_candidates():
    assert micmute.pick_source([], "anything") is None
    assert micmute.pick_source(_SOURCES, "") is None


def test_linux_mute_restores_only_what_it_muted(monkeypatch):
    monkeypatch.setattr(micmute, "_IS_LINUX", True)
    monkeypatch.setattr(micmute, "_linux_source_name", lambda idx: "usb_mic")
    monkeypatch.setattr(micmute, "_linux_is_muted", lambda src: False)
    calls = []
    monkeypatch.setattr(
        micmute, "_pactl",
        lambda *a, **k: (calls.append(a),
                         subprocess.CompletedProcess(a, 0, "", ""))[1],
    )
    token = micmute.mute(0)
    assert isinstance(token, micmute.LinuxMuteToken)
    assert calls == [("set-source-mute", "usb_mic", "1")]
    micmute.unmute(token)
    assert calls[-1] == ("set-source-mute", "usb_mic", "0")


def test_linux_mute_leaves_a_preexisting_mute_alone(monkeypatch):
    monkeypatch.setattr(micmute, "_IS_LINUX", True)
    monkeypatch.setattr(micmute, "_linux_source_name", lambda idx: "usb_mic")
    monkeypatch.setattr(micmute, "_linux_is_muted", lambda src: True)
    calls = []
    monkeypatch.setattr(
        micmute, "_pactl",
        lambda *a, **k: (calls.append(a),
                         subprocess.CompletedProcess(a, 0, "", ""))[1],
    )
    token = micmute.mute(0)
    micmute.unmute(token)
    assert calls == []          # never toggled a mic the user had muted


# ---------------------------------------------------------------------------
# focus.py / win32utils.py
# ---------------------------------------------------------------------------

class _FakeRoot:
    def __init__(self, blow_up=False):
        self.lifted = False
        self.forced = False
        self._blow_up = blow_up

    def lift(self):
        self.lifted = True
        if self._blow_up:
            raise RuntimeError("window is being destroyed")

    def focus_force(self):
        self.forced = True


def test_raise_window_focuses_and_never_raises(monkeypatch):
    monkeypatch.setattr(focus, "_IS_LINUX", True)
    asked = []
    monkeypatch.setattr(focus, "_hyprland_focus", lambda: asked.append(True))
    root = _FakeRoot()
    focus.raise_window(root)
    assert (root.lifted, root.forced) == (True, True)
    assert asked == [True]


def test_raise_window_swallows_a_dying_window(monkeypatch):
    monkeypatch.setattr(focus, "_IS_LINUX", True)
    monkeypatch.setattr(focus, "_hyprland_focus", lambda: None)
    focus.raise_window(_FakeRoot(blow_up=True))     # must not propagate


def test_win32utils_is_inert_off_windows():
    assert win32utils._IS_WIN is False
    win32utils.nudge_alt_key()
    win32utils.force_set_foreground(1234)


# ---------------------------------------------------------------------------
# hotkey.py (pidfile side of the Linux trigger)
# ---------------------------------------------------------------------------

def test_pidfile_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(hotkey, "_IS_LINUX", True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert hotkey.pidfile_path() == tmp_path / "speechskin.pid"
    hotkey.write_pidfile()
    assert (tmp_path / "speechskin.pid").read_text().strip().isdigit()
    hotkey.teardown()
    assert not (tmp_path / "speechskin.pid").exists()


def test_pidfile_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert str(hotkey.pidfile_path()) == "/tmp/speechskin.pid"


def test_signal_handler_only_toggles_when_overlay_enabled(monkeypatch):
    from tts_overlay import config
    from tts_overlay import shutdown as sd

    monkeypatch.setattr(sd, "toggle_event", threading.Event())
    monkeypatch.setattr(config, "ALLOW_OVERLAY", False)
    hotkey._on_signal()
    assert not sd.toggle_event.is_set()
    monkeypatch.setattr(config, "ALLOW_OVERLAY", True)
    hotkey._on_signal()
    assert sd.toggle_event.is_set()
