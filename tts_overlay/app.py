"""Application orchestration: startup banner, wiring, and the main loop.

The main loop is the single most important piece of hardening. Previously a
single transient exception (very common while alt-tabbing) terminated the
whole process. Now every iteration is wrapped so the program *keeps running*
no matter what, and only exits when explicitly asked to.
"""

from __future__ import annotations

import time

import tkinter as tk

from . import cable, config, hotkey, logutil, settings, tts
from . import shutdown as sd
from .overlay import OverlayWindow
from .settings_window import SettingsWindow

log = logutil.get(__name__)


def _print_banner(engine, devices) -> None:
    print()
    devices_dict = dict(devices)
    cable_idx = config.CABLE_DEVICE_INDEX
    speaker_idx = config.SPEAKER_DEVICE_INDEX
    cable_name = devices_dict.get(cable_idx, "(none detected)") if cable_idx is not None else "(none detected)"
    speaker_name = devices_dict.get(speaker_idx, "(default)") if speaker_idx is not None else "(default)"
    print(f"  Engine:  {engine.name}")
    print(f"  Voice:   {engine.current_voice_description()}")
    print(f"  Input:   [{cable_idx}] {cable_name}" if cable_idx is not None
          else f"  Input:   {cable_name}")
    print(f"  Output:  [{speaker_idx}] {speaker_name}" if speaker_idx is not None
          else f"  Output:  {speaker_name}")
    print(f"  Speakers: {'on' if config.PLAY_ON_SPEAKERS else 'off'}")
    print()
    print(f"  Press {config.HOTKEY.upper()} to bring up the overlay")
    print()


def _main_loop(overlay: OverlayWindow, settings_window: SettingsWindow) -> None:
    """Pump Tk events and dispatch cross-thread events until shutdown.

    This loop is intentionally bulletproof: anything that goes wrong in a
    single iteration is logged and the loop continues. The ONLY ways out are
    an explicit shutdown request or the Tk window being destroyed.

    The settings window is a ``Toplevel`` of the overlay's root, so a single
    ``overlay.pump()`` (which calls ``root.update()``) drives both windows.
    """
    consecutive_errors = 0
    while not sd.shutdown_event.is_set():
        try:
            if not overlay.pump():
                log.info("Overlay window closed - exiting loop.")
                break

            if sd.toggle_event.is_set():
                sd.toggle_event.clear()
                overlay.toggle()
            if sd.hide_event.is_set():
                sd.hide_event.clear()
                overlay.hide()
            if sd.post_speak_event.is_set():
                sd.post_speak_event.clear()
                overlay.post_speak_cleanup()
            settings_window.pump_worker_events()

            consecutive_errors = 0
            time.sleep(config.MAIN_LOOP_INTERVAL)
        except Exception as exc:  # noqa: BLE001
            # Never die on a transient error. Log, back off briefly, continue.
            consecutive_errors += 1
            log.warning("Main loop error #%d (recovering): %s",
                        consecutive_errors, exc)
            # If something is persistently and rapidly failing, back off more
            # so we don't spin the CPU, but still don't exit.
            time.sleep(min(0.05 * consecutive_errors, 1.0))


def main() -> None:
    logutil.setup()
    settings.apply()  # load persisted runtime overrides onto config

    engine = tts.get_engine()
    try:
        engine.startup_check()
    except Exception as exc:  # noqa: BLE001
        log.error("TTS engine '%s' is not ready: %s", engine.name, exc)
        print()
        print("==================================================")
        print(f"  [!] {engine.name} engine cannot start:")
        print(f"      {exc}")
        print("==================================================")
        print()
        return

    devices = tts.enumerate_audio_devices()

    # Linux has no VB-Cable driver: make sure the PipeWire null sink exists,
    # then re-enumerate so the new device shows up in the list below.
    if cable.ensure():
        devices = tts.enumerate_audio_devices()

    # Auto-detect the virtual cable if not already pinned by /audio input.
    if config.CABLE_DEVICE_INDEX is None:
        detected = tts.find_cable_device(config.CABLE_DEVICE_KEYWORD)
        if detected is not None:
            config.CABLE_DEVICE_INDEX = detected
            log.info("Auto-detected cable device: [%d] %s",
                     detected, dict(devices).get(detected, "?"))

    _print_banner(engine, devices)

    overlay = OverlayWindow()
    settings_window = SettingsWindow(devices, master=overlay.root)

    hotkey.install_hotkey()
    hotkey.start_watchdog()

    try:
        _main_loop(overlay, settings_window)
    finally:
        hotkey.teardown()
        # Destroy the child Toplevel before its parent root.
        try:
            settings_window.destroy()
        except tk.TclError:
            pass
        try:
            overlay.destroy()
        except tk.TclError:
            pass
        log.info("Done.")
