"""Bring the overlay to the foreground, per platform.

Windows needs the AttachThreadInput coercion dance (``win32utils``) because the
OS hands foreground rights to one window at a time. Wayland compositors do not
let a client raise itself at all, so the Linux path *asks the compositor*
(Hyprland) and otherwise relies on Tk's ``focus_force`` — with
``misc:focus_on_activate = true`` (the Hyprland default) an activate request is
honoured.

Every call is best-effort: focus is inherently racy, and failing to raise the
window must never take the application down.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import logutil

log = logutil.get(__name__)

_IS_WIN = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

# The window title is also what the compositor matching uses, so keep it in one
# place and let the overlay import it.
WINDOW_TITLE = "SpeechSkin"


def _window_handle(root) -> int:
    """Return the native window id Tk is using, or 0 if it cannot be read."""
    try:
        return int(root.wm_frame(), 16)
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return int(root.winfo_id())
    except Exception:  # noqa: BLE001
        return 0


def _hyprland_focus() -> None:
    """Ask Hyprland to focus the overlay window. Never raises."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return
    hyprctl = shutil.which("hyprctl")
    if hyprctl is None:
        return
    try:
        proc = subprocess.run(
            [hyprctl, "dispatch", "focuswindow", f"title:^{WINDOW_TITLE}$"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        # hyprctl answers "ok" on success and an error string otherwise.
        if proc.returncode != 0 or proc.stdout.strip() != "ok":
            log.debug("hyprctl focuswindow: %s", (proc.stdout or proc.stderr).strip())
    except Exception as exc:  # noqa: BLE001
        log.debug("hyprctl focuswindow failed: %s", exc)


def raise_window(root) -> None:
    """Make *root* visible and focused. Never raises."""
    if _IS_WIN:
        try:
            from . import win32utils
            win32utils.nudge_alt_key()
            win32utils.force_set_foreground(_window_handle(root))
        except Exception as exc:  # noqa: BLE001
            log.debug("Windows foreground coercion failed: %s", exc)
    elif _IS_LINUX:
        _hyprland_focus()

    try:
        root.lift()
        root.focus_force()
    except Exception as exc:  # noqa: BLE001
        # TclError is routine while the window is being mapped.
        log.debug("Tk lift/focus_force failed: %s", exc)
