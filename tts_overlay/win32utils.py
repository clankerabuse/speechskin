"""Thin, defensive wrappers around the Win32 APIs we need.

Every function here is written to *never raise*: focus management on
Windows is inherently racy (especially while alt-tabbing), so a failed
SetForegroundWindow must degrade gracefully instead of crashing the app.

The module is importable on other platforms — ``ctypes.windll`` does not exist
off Windows, so the handles stay ``None`` and the functions become no-ops.
:mod:`tts_overlay.focus` is the dispatcher that picks the right backend.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from . import logutil

log = logutil.get(__name__)

_IS_WIN = sys.platform == "win32"

user32: Any
kernel32: Any
if _IS_WIN:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
else:  # non-Windows: keep the import working, make the calls no-ops
    user32 = kernel32 = None

_VK_MENU = 0x12          # ALT key
_KEYEVENTF_KEYUP = 0x0002


def nudge_alt_key() -> None:
    """Tap ALT to unstick Windows' foreground-lock heuristics.

    Wrapped so a failed synthetic key event can never propagate.
    """
    if not _IS_WIN:
        return
    try:
        user32.keybd_event(_VK_MENU, 0, 0, 0)
        user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("nudge_alt_key failed: %s", exc)


def force_set_foreground(hwnd: int) -> None:
    """Best-effort: force *hwnd* to the foreground on Windows 10/11.

    Uses AttachThreadInput to work around foreground-lock restrictions. All
    failures are swallowed because this races with the OS focus state and is
    allowed to fail without consequence.
    """
    if not _IS_WIN or not hwnd:
        return

    attached_fg = False
    attached_target = False
    fg_thread = current_thread = target_thread = 0

    try:
        current_thread = kernel32.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)

        if fg_thread and fg_thread != current_thread:
            attached_fg = bool(
                user32.AttachThreadInput(fg_thread, current_thread, True)
            )
        if (target_thread and target_thread != current_thread
                and target_thread != fg_thread):
            attached_target = bool(
                user32.AttachThreadInput(target_thread, current_thread, True)
            )

        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    except Exception as exc:  # noqa: BLE001
        log.debug("force_set_foreground failed: %s", exc)
    finally:
        try:
            if attached_fg:
                user32.AttachThreadInput(fg_thread, current_thread, False)
            if attached_target:
                user32.AttachThreadInput(target_thread, current_thread, False)
        except Exception as exc:  # noqa: BLE001
            log.debug("detach thread input failed: %s", exc)
