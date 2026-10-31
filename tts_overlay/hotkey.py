"""Global hotkey registration and a watchdog that keeps it alive.

Windows
-------
The ``keyboard`` module runs an internal listener thread. If that thread ever
dies (which is the most common cause of "the script just stops working"), the
hotkey silently stops firing. The watchdog detects this and re-registers.

Linux
-----
The same library refuses to work without root (it reads ``/dev/input``), and
grabbing every keystroke system-wide is not something a desktop app should ask
for. Global hotkeys are the compositor's job there, so the binding lives in the
Hyprland config and invokes ``scripts/speechskin-toggle``, which sends
``SIGUSR1`` to the PID we publish in ``$XDG_RUNTIME_DIR/speechskin.pid``. That
signal sets exactly the same ``toggle_event`` the Windows listener sets, so
nothing downstream changes. The watchdog is unnecessary: the compositor owns
the binding.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from . import config, logutil
from . import shutdown as sd

log = logutil.get(__name__)

_IS_WIN = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

# ``keyboard`` is Windows-only (on Linux it refuses to run without root, and it
# grabs every keystroke). It is imported inside the Windows branches instead of
# at module scope so this module imports everywhere.


# ---------------------------------------------------------------------------
# PID file (Linux): how the compositor binding reaches us
# ---------------------------------------------------------------------------

def pidfile_path() -> Path:
    """Where the Linux hotkey trigger looks for our PID."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "speechskin.pid"


def write_pidfile() -> None:
    """Publish our PID for ``scripts/speechskin-toggle``. Never raises."""
    try:
        path = pidfile_path()
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        log.debug("wrote pidfile %s", path)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not write pidfile: %s", exc)


def remove_pidfile() -> None:
    """Remove the PID file if it is ours. Never raises."""
    try:
        path = pidfile_path()
        if path.is_file() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except Exception as exc:  # noqa: BLE001
        log.debug("could not remove pidfile: %s", exc)


def _on_signal(_signum=None, _frame=None) -> None:
    """SIGUSR1 handler: same effect as a global hotkey press."""
    if config.ALLOW_OVERLAY:
        sd.toggle_event.set()
    else:
        log.debug("hotkey ignored: ALLOW_OVERLAY is off")


def _install_signal_hotkey() -> None:
    """Wire SIGUSR1 to the overlay toggle. Never raises."""
    try:
        signal.signal(signal.SIGUSR1, _on_signal)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to install SIGUSR1 hotkey handler: %s", exc)
        return
    try:
        signal.signal(signal.SIGTERM, lambda *_: sd.request_shutdown())
    except Exception as exc:  # noqa: BLE001
        log.debug("could not install SIGTERM handler: %s", exc)
    write_pidfile()
    log.info(
        "Hotkey: SIGUSR1 (bind it in Hyprland, e.g. CTRL ALT + T -> "
        "scripts/speechskin-toggle); pidfile %s", pidfile_path(),
    )


# ---------------------------------------------------------------------------
# Public API (unchanged for callers)
# ---------------------------------------------------------------------------

def install_hotkey() -> None:
    """Register (or re-register) the global hotkey. Never raises."""
    if _IS_LINUX:
        _install_signal_hotkey()
        return
    import keyboard
    try:
        keyboard.add_hotkey(
            config.HOTKEY,
            lambda: _on_hotkey_pressed(),
            suppress=True,
        )
        log.info("Hotkey registered: %s", config.HOTKEY.upper())
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to register hotkey '%s': %s", config.HOTKEY, exc)


def _on_hotkey_pressed() -> None:
    """Handle hotkey press; only toggle if overlay is enabled."""
    if config.ALLOW_OVERLAY:
        sd.toggle_event.set()


def _listener_alive() -> bool:
    """Best-effort check that the keyboard listener thread is alive."""
    import keyboard
    try:
        listener = getattr(keyboard, "_listener", None)
        if listener is None:
            return False
        # The listener exposes a `listening` flag and/or a thread.
        thread = getattr(listener, "listening_thread", None)
        if thread is not None:
            return bool(thread.is_alive())
        # Fall back to the `listening` attribute if present.
        return bool(getattr(listener, "listening", True))
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not inspect keyboard listener: %s", exc)
        # Assume alive to avoid pointless re-registration storms.
        return True


def start_watchdog() -> threading.Thread | None:
    """Start the background watchdog thread (Windows) or skip it (Linux)."""
    if _IS_LINUX:
        log.debug("no hotkey watchdog needed: the compositor owns the binding")
        return None

    import keyboard

    def _run() -> None:
        while not sd.shutdown_event.is_set():
            sd.shutdown_event.wait(timeout=config.WATCHDOG_INTERVAL)
            if sd.shutdown_event.is_set():
                break
            try:
                if not _listener_alive():
                    log.warning("Keyboard listener died - restarting...")
                    try:
                        keyboard.unhook_all_hotkeys()
                    except Exception:  # noqa: BLE001
                        pass
                    install_hotkey()
            except Exception as exc:  # noqa: BLE001
                log.debug("Watchdog iteration error (ignored): %s", exc)

    watchdog = threading.Thread(target=_run, name="hotkey-watchdog", daemon=True)
    watchdog.start()
    return watchdog


def teardown() -> None:
    """Remove all hotkeys. Never raises."""
    if _IS_LINUX:
        remove_pidfile()
        return
    import keyboard
    try:
        keyboard.unhook_all_hotkeys()
    except Exception as exc:  # noqa: BLE001
        log.debug("unhook_all_hotkeys failed: %s", exc)
