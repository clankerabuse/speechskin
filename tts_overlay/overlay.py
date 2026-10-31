"""The floating, borderless, always-on-top text-entry overlay.

Hardening principles applied throughout:

* Tk is single-threaded. Worker threads NEVER touch widgets; they communicate
  via the events in ``shutdown``. Only the main loop (which owns Tk) reacts.
* Every Tk callback is wrapped in :func:`_safe` so a transient ``TclError``
  (very common while alt-tabbing) is logged and swallowed instead of bubbling
  up into the event loop and killing the process.
* Focus operations are best-effort and allowed to fail.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from . import commands, config, focus, logutil, speech, tts
from . import shutdown as sd
from .tkutil import safe as _safe

log = logutil.get(__name__)


class OverlayWindow:
    """A borderless, semi-transparent, always-on-top text entry bar."""

    def __init__(self):
        self._visible = False
        self._building = True
        self._speaking = False
        self._focus_locked = False  # suppresses focus-out auto-hide during speak

        # Autocomplete dropdown state.
        self._popup: tk.Toplevel | None = None
        self._listbox: tk.Listbox | None = None
        self._suggestions: list[commands.Suggestion] = []
        self._status_reset_id: str | None = None

        self.root = tk.Tk()
        self.root.title(focus.WINDOW_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", config.OVERLAY_ALPHA)
        self.root.configure(bg="#1a1a2e")
        self.root.withdraw()
        # Intercept the OS close/delete signal so the window is never destroyed
        # by accident (e.g. Alt+F4, taskbar close). The only intentional exits
        # are Ctrl+C in the terminal or the /exit command.
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        screen_w = self.root.winfo_screenwidth()
        x = (screen_w - config.OVERLAY_WIDTH_PX) // 2
        self.root.geometry(
            f"{config.OVERLAY_WIDTH_PX}x{config.OVERLAY_HEIGHT_PX}"
            f"+{x}+{config.OVERLAY_Y_OFFSET}"
        )

        self._build_widgets()
        self._bind_keys()
        self._building = False

    # -- construction --

    def _build_widgets(self) -> None:
        border = tk.Frame(self.root, bg="#7b2ff7", padx=2, pady=2)
        border.pack(fill="both", expand=True)

        inner = tk.Frame(border, bg="#1a1a2e")
        inner.pack(fill="both", expand=True)

        self._indicator = tk.Label(
            inner, text="", bg="#1a1a2e", fg="#6b7280",
            font=("Segoe UI", 8), anchor="w",
        )
        self._indicator.pack(fill="x", padx=(12, 12), pady=(4, 0))

        row = tk.Frame(inner, bg="#1a1a2e")
        row.pack(fill="both", expand=True)

        status_frame = tk.Frame(row, bg="#1a1a2e")
        status_frame.pack(side="left", padx=(12, 6))

        self._dot = tk.Canvas(
            status_frame, width=10, height=10,
            bg="#1a1a2e", highlightthickness=0,
        )
        self._dot.create_oval(1, 1, 9, 9, fill="#00e676", outline="")
        self._dot.pack(side="left")

        self._label = tk.Label(
            status_frame, text="SpeechSkin", bg="#1a1a2e", fg="#888ea8",
            font=("Segoe UI", 9, "bold"),
        )
        self._label.pack(side="left", padx=(4, 0))

        entry_font = tkfont.Font(family="Segoe UI", size=14)
        self.entry = tk.Entry(
            row, font=entry_font,
            bg="#16213e", fg="#e0e0e0",
            insertbackground="#7b2ff7",
            relief="flat", highlightthickness=0, border=0,
        )
        self.entry.pack(
            side="left", fill="both", expand=True,
            padx=(4, 12), pady=(6, 8),
        )

        self._build_popup()

    def _build_popup(self) -> None:
        """Create the (initially hidden) autocomplete dropdown."""
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="#7b2ff7")
        popup.withdraw()

        listbox = tk.Listbox(
            popup,
            font=("Segoe UI", 11),
            bg="#16213e", fg="#e0e0e0",
            selectbackground="#7b2ff7", selectforeground="#ffffff",
            activestyle="none", relief="flat",
            highlightthickness=0, borderwidth=0,
            height=1,
        )
        listbox.pack(fill="both", expand=True, padx=2, pady=2)
        listbox.bind("<ButtonRelease-1>", self._on_popup_click)

        self._popup = popup
        self._listbox = listbox

    def _bind_keys(self) -> None:
        self.entry.bind("<Return>", self._handle_submit)
        self.entry.bind("<Escape>", self._handle_escape)
        self.entry.bind("<KeyRelease>", self._on_key_release)
        self.entry.bind("<Down>", self._on_down)
        self.entry.bind("<Up>", self._on_up)
        self.entry.bind("<Tab>", self._on_tab)
        self.root.bind("<Control-c>", self._handle_quit)
        self.root.bind("<FocusOut>", self._handle_focus_out)

    # -- public (called only from the Tk thread / main loop) --

    @_safe
    def toggle(self) -> None:
        if self._visible:
            self._hide()
        else:
            self._show()

    @_safe
    def hide(self) -> None:
        self._hide()

    @_safe
    def post_speak_cleanup(self) -> None:
        self._focus_locked = False
        self.entry.config(state="normal")
        self.entry.delete(0, tk.END)
        self._set_status("SpeechSkin", "#00e676")

    @_safe
    def destroy(self) -> None:
        self.root.destroy()

    def pump(self) -> bool:
        """Process pending Tk events once.

        Returns ``False`` only when a deliberate shutdown has been requested
        AND the window is gone. Transient Tk errors are swallowed so the main
        loop never exits unexpectedly.
        """
        try:
            self.root.update()
            return True
        except tk.TclError as exc:
            # Only treat a Tcl error as fatal if we intentionally asked for
            # shutdown; otherwise log and keep going.
            if sd.shutdown_event.is_set():
                log.debug("Tk update error during shutdown: %s", exc)
                return False
            log.debug("Tk update error (ignored, not a shutdown): %s", exc)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Unexpected error pumping Tk events (ignored): %s", exc)
            return True

    # -- internal --

    @_safe
    def _set_status(self, text: str, colour: str = "#00e676") -> None:
        self._dot.delete("all")
        self._dot.create_oval(1, 1, 9, 9, fill=colour, outline="")
        self._label.config(text=text)

    def _update_indicator(self) -> None:
        try:
            engine = tts.current_engine_display_name()
            voice = tts.current_voice_description()
            text = f"{engine}  |  {voice}"
        except Exception:
            text = ""
        try:
            self._indicator.config(text=text)
        except tk.TclError:
            pass

    @_safe
    def _show(self) -> None:
        # Cancel any lingering "message" state from a prior command so the box
        # opens clean and editable.
        if self._status_reset_id is not None:
            try:
                self.root.after_cancel(self._status_reset_id)
            except tk.TclError:
                pass
            self._status_reset_id = None
        self.entry.config(state="normal", fg="#e0e0e0")

        self.root.deiconify()
        self.root.lift()
        self.root.update_idletasks()

        # Platform focus coercion: Win32 foreground lock on Windows, the
        # compositor on Wayland. Best-effort and never fatal.
        focus.raise_window(self.root)

        self.entry.focus_set()
        self.entry.delete(0, tk.END)
        self._update_indicator()

        status = "Speaking..." if self._speaking else "SpeechSkin"
        colour = "#42a5f5" if self._speaking else "#00e676"
        self._set_status(status, colour)
        self._visible = True

        self.root.after(50, self._ensure_focus)

    @_safe
    def _ensure_focus(self) -> None:
        if self._visible:
            self.root.lift()
            self.root.focus_force()
            self.entry.focus_set()

    @_safe
    def _hide(self) -> None:
        self._hide_popup()
        self.root.withdraw()
        self._visible = False

    # -- autocomplete dropdown --

    @_safe
    def _on_key_release(self, event=None) -> None:
        # Ignore navigation keys handled elsewhere.
        if event is not None and event.keysym in (
            "Up", "Down", "Tab", "Return", "Escape",
        ):
            return
        self._refresh_suggestions()

    def _refresh_suggestions(self) -> None:
        text = self.entry.get()
        if not commands.is_command(text):
            self._hide_popup()
            return
        self._suggestions = commands.suggest(text)
        if not self._suggestions:
            self._hide_popup()
            return
        self._show_popup()

    @_safe
    def _show_popup(self) -> None:
        if self._popup is None or self._listbox is None:
            return
        lb = self._listbox
        lb.delete(0, tk.END)
        for s in self._suggestions:
            lb.insert(tk.END, s.label)

        rows = min(len(self._suggestions), 8)
        lb.config(height=rows)

        # Position the popup directly beneath the overlay.
        self.root.update_idletasks()
        x = self.root.winfo_rootx()
        y = self.root.winfo_rooty() + self.root.winfo_height() + 2
        w = self.root.winfo_width()
        self._popup.geometry(f"{w}x1+{x}+{y}")
        self._popup.deiconify()
        self._popup.lift()
        self._popup.update_idletasks()
        # Let the listbox size itself, then fix the popup height to match.
        self._popup.geometry(
            f"{w}x{self._popup.winfo_reqheight()}+{x}+{y}"
        )

        # Preselect the first item so Enter/Tab have a target.
        lb.selection_clear(0, tk.END)
        lb.selection_set(0)
        lb.activate(0)

    @_safe
    def _hide_popup(self) -> None:
        self._suggestions = []
        if self._popup is not None:
            self._popup.withdraw()

    def _popup_visible(self) -> bool:
        try:
            return bool(self._popup) and self._popup.winfo_viewable()
        except tk.TclError:
            return False

    def _move_selection(self, delta: int) -> None:
        if not self._popup_visible() or self._listbox is None:
            return
        lb = self._listbox
        size = lb.size()
        if size == 0:
            return
        cur = lb.curselection()
        idx = (cur[0] if cur else 0) + delta
        idx = max(0, min(size - 1, idx))
        lb.selection_clear(0, tk.END)
        lb.selection_set(idx)
        lb.activate(idx)
        lb.see(idx)

    @_safe
    def _on_down(self, _event=None):
        if self._popup_visible():
            self._move_selection(1)
            return "break"
        return None

    @_safe
    def _on_up(self, _event=None):
        if self._popup_visible():
            self._move_selection(-1)
            return "break"
        return None

    @_safe
    def _on_tab(self, _event=None):
        if self._accept_current_suggestion():
            return "break"
        return None

    @_safe
    def _on_popup_click(self, _event=None):
        if self._accept_current_suggestion(execute_if_complete=True):
            self.entry.focus_set()

    def _highlighted_suggestion(self) -> commands.Suggestion | None:
        if not self._popup_visible() or self._listbox is None:
            return None
        cur = self._listbox.curselection()
        if not cur:
            return None
        idx = cur[0]
        if not (0 <= idx < len(self._suggestions)):
            return None
        return self._suggestions[idx]

    def _is_complete_suggestion(self, sugg: commands.Suggestion) -> bool:
        """Check if a suggestion represents a complete, runnable command."""
        if not sugg.insert.endswith(" "):
            return True  # No trailing space = complete command with args
        # Trailing space = command name only. Check if command needs arguments.
        cmd_name, _, _ = commands._split(sugg.insert)
        cmd = commands._COMMANDS.get(cmd_name)
        if cmd is None:
            return False
        return not cmd.complete("")  # Complete if no argument completions

    def _accept_current_suggestion(self, execute_if_complete: bool = False) -> bool:
        """Fill the entry with the highlighted suggestion. Returns True if done."""
        sugg = self._highlighted_suggestion()
        if sugg is None:
            return False
        self.entry.delete(0, tk.END)
        self.entry.insert(0, sugg.insert)
        self.entry.icursor(tk.END)

        if execute_if_complete and self._is_complete_suggestion(sugg):
            self._hide_popup()
            self.root.after(10, self._handle_submit)
            return True

        # Offer the next level of completion (e.g. command -> its arguments).
        self._refresh_suggestions()
        return True

    # -- submit --

    @_safe
    def _handle_submit(self, _event=None) -> None:
        # A command result is currently shown in the box (readonly). Pressing
        # Enter on it must NOT speak it; just clear back to an empty box.
        if str(self.entry.cget("state")) == "readonly":
            self._clear_message()
            return

        # Dropdown is open: decide between autocompleting and running.
        if self._popup_visible():
            sugg = self._highlighted_suggestion()
            current = self.entry.get()
            # If the highlighted suggestion is not yet exactly in the box, Enter
            # autocompletes it (and keeps drilling into arguments). Note command
            # suggestions carry a trailing space (e.g. "/engine "), so a bare
            # "/engine" won't match and will autocomplete first. Only when the
            # text already equals the suggestion do we fall through and run.
            if sugg is not None and sugg.insert != current:
                self._accept_current_suggestion(execute_if_complete=True)
                return
            self._hide_popup()

        text = self.entry.get().strip()
        if not text:
            return

        self._hide_popup()

        # SAFETY: anything starting with "/" is a command, never speech. Even
        # if it is not a recognised command, it must NOT be sent to the TTS
        # engine. _run_command reports unknown commands gracefully.
        if commands.is_command(text):
            self._run_command(text)
            return

        self._focus_locked = True
        self._set_status("Speaking...", "#42a5f5")
        self.entry.config(state="disabled")
        self.root.update_idletasks()

        threading.Thread(
            target=self._do_speak, args=(text,), daemon=True
        ).start()

    @_safe
    def _run_command(self, text: str) -> None:
        result = commands.run(text)
        self._update_indicator()
        if result.quit:
            self._set_status("bye", "#ff5252")
            self._show_message(result.message or "Exiting...", "#ff5252")
            self.root.after(250, sd.request_shutdown)
            return
        # Immediately clear entry and keep it editable — no delay.
        self.entry.config(state="normal", fg="#e0e0e0")
        self.entry.delete(0, tk.END)
        colour = "#00e676" if result.ok else "#ff5252"
        self._set_status("done" if result.ok else "err", colour)
        self.root.after(800,
                        lambda: self._set_status("SpeechSkin", "#00e676"))
        try:
            self.entry.focus_set()
        except tk.TclError:
            pass

    @_safe
    def _show_message(self, text: str, colour: str, hold_ms: int = 3500) -> None:
        """Display a transient message in the entry box, then clear it.

        The entry is the only wide widget, so command feedback is shown there
        (greyed/non-editable) and reverts to an empty editable box afterwards.
        """
        self.entry.config(state="normal")
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)
        self.entry.config(fg=colour, state="readonly")

        if self._status_reset_id is not None:
            try:
                self.root.after_cancel(self._status_reset_id)
            except tk.TclError:
                pass
        self._status_reset_id = self.root.after(hold_ms, self._clear_message)

    @_safe
    def _clear_message(self) -> None:
        self._status_reset_id = None
        self.entry.config(state="normal", fg="#e0e0e0")
        self.entry.delete(0, tk.END)
        self._set_status("SpeechSkin", "#00e676")
        try:
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _do_speak(self, text: str) -> None:
        """Runs on a worker thread. Must NOT touch Tk widgets directly."""
        if sd.shutdown_event.is_set():
            return
        self._speaking = True
        try:
            sd.hide_event.set()
            time.sleep(0.05)
            speech.speak_to_devices(text)
        finally:
            self._speaking = False
            sd.post_speak_event.set()

    @_safe
    def _handle_escape(self, _event=None) -> None:
        # Escape peels back one layer at a time: dropdown -> message -> hide.
        if self._popup_visible():
            self._hide_popup()
            return
        if str(self.entry.cget("state")) == "readonly":
            self._clear_message()
            return
        self._hide()

    @_safe
    def _handle_quit(self, _event=None) -> None:
        sd.request_shutdown()

    @_safe
    def _handle_focus_out(self, _event=None) -> None:
        if self._building or not self._visible or self._focus_locked:
            return
        self.root.after(100, self._check_focus)

    @_safe
    def _check_focus(self) -> None:
        try:
            focused = self.root.focus_get()
        except (KeyError, tk.TclError):
            # focus_get raises KeyError when focus is on a foreign window
            # (extremely common during alt-tab). Treat as "not us".
            focused = None
        if focused is None and self._visible:
            self._hide()
