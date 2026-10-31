"""The main settings window shown on startup.

A streamlined control surface built entirely around **Skin** — the
continuous listen → transcribe → speak loop.

Now separated into:
1. **Main Window**: Dedicated to running the speech skin (Header with a gear icon, Skin button, mode toggles, PTT status indicator, transcript view, and quick play/clear commands).
2. **Settings Window**: A dedicated Toplevel window that opens smoothly over the main window, managing all device configurations, voice engines, profiles, and key bindings. Closing this window returns instantly to the main window.

Unified keyboard & mouse button global PTT captures are powered by `pynput`.
"""

from __future__ import annotations


from collections.abc import Callable
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from queue import Empty, Queue

from . import config, focus, logutil, micmute, recorder, secrets, settings, speech, tts
from . import shutdown as sd
from . import skin_ptt as _skin_ptt_mod
from .tkutil import safe as _safe, device_label as _device_label

log = logutil.get(__name__)

# Colour palette — refined dark-mode surface / accent system.
_BG = "#0b0b1a"
_PANEL = "#141428"
_FIELD = "#1a1a38"
_BORDER = "#232350"
_ACCENT = "#7b2ff7"
_ACCENT_GLOW = "#9d5cfa"
_ACCENT_DARK = "#6a1ee3"
_TEXT = "#e8e8f0"
_TEXT_DIM = "#a0a0c0"
_MUTED = "#6b6b90"
_OK = "#00e676"
_BUSY = "#42a5f5"
_ERR = "#ff5252"
_REC = "#ff3b3b"
_SKIN_IDLE = "#7b2ff7"
_SKIN_ACTIVE = "#ff3b3b"
_PILL_ACTIVE = "#9d5cfa"
_PILL_INACTIVE = "#1a1a38"


class SettingsWindow:
    """The main, on-startup control window."""

    # A sentinel option meaning "use the system default device".
    _DEFAULT_OPTION = "System default"


    def __init__(self, output_devices: list[tuple[int, str]] | None = None,
                 master: tk.Misc | None = None):
        # Cached device/voice lists: list[(index_or_None, label)].
        self._output_devices = output_devices
        self._input_devices: list[tuple[int, str]] = []
        self._voices: list[tuple[int, str]] = []

        self._speaking = False
        self._recorder: recorder.Recorder | None = None
        self._skin_active = False
        self._skin_ptt: _skin_ptt_mod.SkinPTT | None = None
        self._binding_key: str | None = None  # which PTT key we're binding
        self._ui_callbacks: Queue[Callable[[], None]] = Queue()
        self._building = True

        # Tk allows only one root per process. The overlay owns the root
        # ``Tk()`` instance, so this window is a ``Toplevel`` of it. When no
        # master is supplied (e.g. in isolated tests) we create our own root.
        self._owns_root = master is None
        if master is None:
            self.root = tk.Tk()
        else:
            self.root = tk.Toplevel(master)
            
        self.root.title("SpeechSkin")
        self.root.geometry("580x500")
        self.root.minsize(500, 420)
        self.root.configure(bg=_BG)
        # Linux/Wayland: tiling compositors ignore the geometry above, so ask
        # to be treated as a dialog — that is what keeps the layout intact.
        focus.make_dialog(self.root)

        # Create the Configuration Window as a child of MainWindow.
        # It stays hidden initially and transitions in when the gear icon is clicked.
        self.config_root = tk.Toplevel(self.root)
        self.config_root.title("SpeechSkin - Settings")
        self.config_root.geometry("580x640")
        self.config_root.minsize(500, 560)
        self.config_root.configure(bg=_BG)
        self.config_root.withdraw()  # Start hidden
        focus.make_dialog(self.config_root)

        # Intercept the OS close/delete signals so:
        # 1. Closing main window triggers application shutdown.
        # 2. Closing config window returns gracefully to main window.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.config_root.protocol("WM_DELETE_WINDOW", self._hide_config)

        self._configure_ttk_style()
        self._load_data()
        self._build_widgets()
        self._bind_keys()
        self._building = False
        self._center()

    # ------------------------------------------------------------------ data

    def _load_data(self) -> None:
        """Populate the device/voice caches (best-effort; never raises)."""
        try:
            if self._output_devices is None:
                self._output_devices = tts.enumerate_audio_devices()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list output devices: %s", exc)
            self._output_devices = []
        try:
            self._input_devices = recorder.enumerate_input_devices()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list input devices: %s", exc)
            self._input_devices = []
        try:
            self._voices = tts.enumerate_voices()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list voices: %s", exc)
            self._voices = []

    # ------------------------------------------------------------------ style

    def _configure_ttk_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "SpeechSkin.TCombobox",
            fieldbackground=_FIELD,
            background=_FIELD,
            foreground=_TEXT,
            arrowcolor=_ACCENT,
            borderwidth=1,
            relief="flat",
            padding=4,
        )
        style.map(
            "SpeechSkin.TCombobox",
            fieldbackground=[("readonly", _FIELD)],
            foreground=[("readonly", _TEXT)],
            selectbackground=[("readonly", _FIELD)],
            selectforeground=[("readonly", _TEXT)],
        )

    # --------------------------------------------------------------- widgets

    def _build_widgets(self) -> None:
        # 1. Build Main Window components
        self._build_main_header()

        main_body = tk.Frame(self.root, bg=_BG)
        main_body.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        self._build_skin_hero(main_body)
        self._build_text_area(main_body)
        self._build_status(main_body)

        # 2. Build Settings Window components
        self._build_config_header()

        config_body = tk.Frame(self.config_root, bg=_BG)
        config_body.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        self._build_ptt_config(config_body)
        self._build_options(config_body)
        self._build_config_status(config_body)

    # ------------------------------------------------------ main window widgets

    def _build_main_header(self) -> None:
        header = tk.Frame(self.root, bg=_BG)
        header.pack(fill="x", side="top")

        # Thin accent bar along the very top.
        accent_strip = tk.Frame(header, bg=_ACCENT, height=2)
        accent_strip.pack(fill="x", side="top")

        inner = tk.Frame(header, bg=_BG)
        inner.pack(fill="x", padx=24, pady=(14, 10))

        title = tk.Label(
            inner, text="SpeechSkin", bg=_BG, fg=_TEXT,
            font=("Segoe UI", 16, "bold"),
        )
        title.pack(side="left")

        subtitle = tk.Label(
            inner, text="Voice Skin Engine", bg=_BG, fg=_MUTED,
            font=("Segoe UI", 9),
        )
        subtitle.pack(side="left", padx=(10, 0))

        # Premium gear icon button on the top right
        self._gear_btn = tk.Button(
            inner, text="⚙", bg=_BG, fg=_MUTED, activebackground=_BG,
            activeforeground=_ACCENT, font=("Segoe UI", 16),
            relief="flat", cursor="hand2", bd=0, command=self._show_config,
        )
        self._gear_btn.pack(side="right")
        self._gear_btn.bind("<Enter>", lambda e: self._gear_btn.config(fg=_ACCENT))
        self._gear_btn.bind("<Leave>", lambda e: self._gear_btn.config(fg=_MUTED))

        # Thin separator.
        sep = tk.Frame(header, bg=_BORDER, height=1)
        sep.pack(fill="x")

    def _build_skin_hero(self, parent: tk.Frame) -> None:
        """Build the large Skin on/off switch with hotkey info underneath."""
        hero = tk.Frame(parent, bg=_BG)
        hero.pack(fill="x", pady=(18, 6))

        # Outer glow ring (simulated via a coloured frame behind the button).
        self._skin_ring = tk.Frame(hero, bg=_SKIN_IDLE, padx=3, pady=3)
        self._skin_ring.pack()

        self._skin_btn = tk.Button(
            self._skin_ring, text="▶  SKIN", command=self._on_skin,
            bg=_SKIN_IDLE, fg="#ffffff",
            font=("Segoe UI", 16, "bold"),
            padx=48, pady=14, relief="flat",
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
            cursor="hand2", bd=0,
        )
        self._skin_btn.pack()
        if not self._transcription_available():
            self._skin_btn.config(state="disabled")

        # Active binds as interactive buttons so users can click to bind in the main window
        self._main_binds_frame = tk.Frame(hero, bg=_BG)
        self._main_binds_frame.pack(pady=(12, 0))

        # Main window PTT (skin) bind button
        self._main_skin_ptt_bind_btn = tk.Button(
            self._main_binds_frame, text="PTT", command=lambda: self._start_key_bind("skin"),
            bg=_ACCENT, fg="#ffffff", font=("Segoe UI", 8, "bold"),
            padx=10, pady=3, relief="flat", cursor="hand2", bd=0,
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
        )
        self._main_skin_ptt_bind_btn.pack(side="left", padx=(0, 6))

        self._main_skin_ptt_label = tk.Label(
            self._main_binds_frame, text=config.SKIN_PTT_KEY or "(not set)",
            bg=_FIELD, fg=_TEXT, font=("Segoe UI", 9),
            padx=10, pady=3, relief="flat", anchor="center",
        )
        self._main_skin_ptt_label.pack(side="left", padx=(0, 16))

        # Main window Target PTT bind button
        self._main_target_ptt_bind_btn = tk.Button(
            self._main_binds_frame, text="Target", command=lambda: self._start_key_bind("target"),
            bg=_ACCENT, fg="#ffffff", font=("Segoe UI", 8, "bold"),
            padx=10, pady=3, relief="flat", cursor="hand2", bd=0,
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
        )
        self._main_target_ptt_bind_btn.pack(side="left", padx=(0, 6))

        self._main_target_ptt_label = tk.Label(
            self._main_binds_frame, text=config.TARGET_PTT_KEY or "(not set)",
            bg=_FIELD, fg=_TEXT, font=("Segoe UI", 9),
            padx=10, pady=3, relief="flat", anchor="center",
        )
        self._main_target_ptt_label.pack(side="left")

    def _build_text_area(self, parent: tk.Frame) -> None:
        frame = tk.LabelFrame(
            parent, text=" Transcript ", bg=_PANEL, fg=_TEXT_DIM,
            font=("Segoe UI", 9, "bold"), padx=12, pady=10, bd=1,
            relief="flat", highlightbackground=_BORDER, highlightthickness=1,
        )
        frame.pack(fill="both", expand=True, pady=(0, 8))

        text_font = tkfont.Font(family="Segoe UI", size=11)
        self.text = tk.Text(
            frame, height=4, font=text_font, wrap="word",
            bg=_FIELD, fg=_TEXT, insertbackground=_ACCENT,
            relief="flat", highlightthickness=0, bd=0, padx=10, pady=8,
        )
        self.text.pack(fill="both", expand=True)

        # Control bar inside transcript pane
        controls = tk.Frame(frame, bg=_PANEL)
        controls.pack(fill="x", side="bottom", pady=(6, 0))

        self._play_btn = tk.Button(
            controls, text="▶  Play", command=self._on_speak,
            bg=_ACCENT, fg="#ffffff", font=("Segoe UI", 8, "bold"),
            padx=12, pady=4, relief="flat",
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
            cursor="hand2", bd=0,
        )
        self._play_btn.pack(side="left")

        self._clear_btn = tk.Button(
            controls, text="🗑  Clear", command=self._on_clear,
            bg=_FIELD, fg=_TEXT_DIM, font=("Segoe UI", 8),
            padx=10, pady=4, relief="flat",
            activebackground=_ACCENT, activeforeground="#ffffff",
            cursor="hand2", bd=0,
        )
        self._clear_btn.pack(side="left", padx=(8, 0))

    def _build_status(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=_BG)
        bar.pack(fill="x", pady=(4, 0))

        # Small status dot + label.
        self._status_dot = tk.Canvas(
            bar, width=8, height=8, bg=_BG, highlightthickness=0,
        )
        self._status_dot.pack(side="left", padx=(2, 6))
        self._status_dot.create_oval(1, 1, 7, 7, fill=_OK, outline="",
                                     tags="dot")

        self.status_label = tk.Label(
            bar, text="Ready", bg=_BG, fg=_OK,
            font=("Segoe UI", 8), anchor="w", justify="left",
        )
        self.status_label.pack(side="left")

    # ------------------------------------------------------ settings window widgets

    def _build_config_header(self) -> None:
        header = tk.Frame(self.config_root, bg=_BG)
        header.pack(fill="x", side="top")

        accent_strip = tk.Frame(header, bg=_ACCENT, height=2)
        accent_strip.pack(fill="x", side="top")

        inner = tk.Frame(header, bg=_BG)
        inner.pack(fill="x", padx=24, pady=(14, 10))

        title = tk.Label(
            inner, text="Settings", bg=_BG, fg=_TEXT,
            font=("Segoe UI", 16, "bold"),
        )
        title.pack(side="left")

        subtitle = tk.Label(
            inner, text="Configuration", bg=_BG, fg=_MUTED,
            font=("Segoe UI", 9),
        )
        subtitle.pack(side="left", padx=(10, 0))

        # Premium back button on the top right
        back_btn = tk.Button(
            inner, text="✕", bg=_BG, fg=_MUTED, activebackground=_BG,
            activeforeground=_ERR, font=("Segoe UI", 14, "bold"),
            relief="flat", cursor="hand2", bd=0, command=self._hide_config,
        )
        back_btn.pack(side="right")
        back_btn.bind("<Enter>", lambda e: back_btn.config(fg=_ERR))
        back_btn.bind("<Leave>", lambda e: back_btn.config(fg=_MUTED))

        sep = tk.Frame(header, bg=_BORDER, height=1)
        sep.pack(fill="x")

    def _build_ptt_config(self, parent: tk.Frame) -> None:
        self._ptt_config_frame = tk.LabelFrame(
            parent, text=" Profiles & Hotkeys ", bg=_PANEL, fg=_TEXT_DIM,
            font=("Segoe UI", 9, "bold"), padx=14, pady=10, bd=1,
            relief="flat", highlightbackground=_BORDER, highlightthickness=1,
        )
        self._ptt_config_frame.pack(fill="x", pady=(10, 8))

        # Profile selection row
        profile_row = tk.Frame(self._ptt_config_frame, bg=_PANEL)
        profile_row.pack(fill="x", padx=12, pady=(8, 4))

        tk.Label(profile_row, text="Profile", bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 8), anchor="e", width=14).pack(side="left", padx=(0, 6))

        # Ensure config.PROFILES exists
        if not hasattr(config, "PROFILES") or not isinstance(config.PROFILES, dict):
            config.PROFILES = {"Default": {"SKIN_PTT_KEY": "", "TARGET_PTT_KEY": ""}}

        self.profile_var = tk.StringVar(value=getattr(config, "ACTIVE_PROFILE", "Default"))
        self._profile_combo = ttk.Combobox(
            profile_row, textvariable=self.profile_var,
            values=list(config.PROFILES.keys()), state="readonly",
            style="SpeechSkin.TCombobox", font=("Segoe UI", 9),
        )
        self._profile_combo.pack(side="left", fill="x", expand=True)
        self._profile_combo.bind("<<ComboboxSelected>>", self._on_profile_change)

        self._add_profile_btn = tk.Button(
            profile_row, text="+ New", command=self._on_add_profile,
            bg=_ACCENT, fg="#ffffff", font=("Segoe UI", 8, "bold"),
            padx=8, pady=2, relief="flat", cursor="hand2", bd=0,
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
        )
        self._add_profile_btn.pack(side="left", padx=(6, 0))

        self._remove_profile_btn = tk.Button(
            profile_row, text="- Delete", command=self._on_remove_profile,
            bg=_FIELD, fg=_TEXT_DIM, font=("Segoe UI", 8),
            padx=8, pady=2, relief="flat", cursor="hand2", bd=0,
            activebackground=_ERR, activeforeground="#ffffff",
        )
        self._remove_profile_btn.pack(side="left", padx=(4, 0))

        ptt_inner = tk.Frame(self._ptt_config_frame, bg=_PANEL)
        ptt_inner.pack(fill="x", padx=12, pady=4)

        # Skin PTT key
        tk.Label(ptt_inner, text="Skin PTT Key", bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 8), anchor="e", width=14).grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=2)
        self._skin_ptt_label = tk.Label(
            ptt_inner, text=config.SKIN_PTT_KEY or "(not set)",
            bg=_FIELD, fg=_TEXT, font=("Segoe UI", 9),
            padx=10, pady=4, relief="flat", width=18, anchor="w",
        )
        self._skin_ptt_label.grid(row=0, column=1, sticky="ew", pady=2)
        self._skin_ptt_bind_btn = tk.Button(
            ptt_inner, text="Bind", command=lambda: self._start_key_bind("skin"),
            bg=_ACCENT, fg="#ffffff", font=("Segoe UI", 8, "bold"),
            padx=8, pady=2, relief="flat", cursor="hand2", bd=0,
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
        )
        self._skin_ptt_bind_btn.grid(row=0, column=2, padx=(6, 0), pady=2)

        # Target PTT key
        tk.Label(ptt_inner, text="Target PTT Key", bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 8), anchor="e", width=14).grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=2)
        self._target_ptt_label = tk.Label(
            ptt_inner, text=config.TARGET_PTT_KEY or "(not set)",
            bg=_FIELD, fg=_TEXT, font=("Segoe UI", 9),
            padx=10, pady=4, relief="flat", width=18, anchor="w",
        )
        self._target_ptt_label.grid(row=1, column=1, sticky="ew", pady=2)
        self._target_ptt_bind_btn = tk.Button(
            ptt_inner, text="Bind", command=lambda: self._start_key_bind("target"),
            bg=_ACCENT, fg="#ffffff", font=("Segoe UI", 8, "bold"),
            padx=8, pady=2, relief="flat", cursor="hand2", bd=0,
            activebackground=_ACCENT_DARK, activeforeground="#ffffff",
        )
        self._target_ptt_bind_btn.grid(row=1, column=2, padx=(6, 0), pady=2)

        ptt_inner.grid_columnconfigure(1, weight=1)

        # Spacer inside profiles pane
        pad_row = tk.Frame(self._ptt_config_frame, bg=_PANEL)
        pad_row.pack(fill="x", padx=12, pady=(0, 4))

    def _build_options(self, parent: tk.Frame) -> None:
        # Voice Engine Frame
        engine_frame = tk.LabelFrame(
            parent, text=" Voice Engine ", bg=_PANEL, fg=_TEXT_DIM,
            font=("Segoe UI", 9, "bold"), padx=14, pady=10, bd=1,
            relief="flat", highlightbackground=_BORDER, highlightthickness=1,
        )
        engine_frame.pack(fill="x", pady=(8, 6))
        engine_frame.grid_columnconfigure(1, weight=1)

        # Engine
        self.engine_var = tk.StringVar()
        self._engine_combo = self._add_dropdown(
            engine_frame, 0, "Engine", self.engine_var, self._engine_values(),
            self._on_engine_change,
        )

        # Voice
        self.voice_var = tk.StringVar()
        self._voice_combo = self._add_dropdown(
            engine_frame, 1, "Voice", self.voice_var, self._voice_values(),
            self._on_voice_change, has_refresh=True, refresh_command=self._on_refresh_voices,
        )

        # API Key
        lbl_key = tk.Label(
            engine_frame, text="API Key", bg=_PANEL, fg=_MUTED,
            font=("Segoe UI", 8), anchor="e", width=12,
        )
        lbl_key.grid(row=2, column=0, sticky="e", pady=3, padx=(0, 6))

        key_container = tk.Frame(engine_frame, bg=_PANEL)
        key_container.grid(row=2, column=1, sticky="ew", pady=3)
        key_container.grid_columnconfigure(0, weight=1)

        self.api_key_var = tk.StringVar()
        self._api_key_entry = tk.Entry(
            key_container, textvariable=self.api_key_var, show="*",
            bg=_FIELD, fg=_TEXT, insertbackground=_ACCENT,
            relief="flat", highlightthickness=1,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            font=("Segoe UI", 9),
        )
        self._api_key_entry.grid(row=0, column=0, sticky="ew", ipady=3, padx=(0, 2))
        self._api_key_entry.bind("<FocusOut>", self._on_api_key_change)
        self._api_key_entry.bind("<Return>", self._on_api_key_change)

        self._api_key_toggle_btn = tk.Button(
            key_container, text="👁", command=self._toggle_api_key_visibility,
            bg=_FIELD, fg=_MUTED, font=("Segoe UI", 9, "bold"),
            padx=6, pady=2, relief="flat",
            activebackground=_ACCENT, activeforeground="#ffffff",
            cursor="hand2", bd=0,
        )
        self._api_key_toggle_btn.grid(row=0, column=1, padx=(4, 0), sticky="e")

        # Audio Devices Frame
        devices_frame = tk.LabelFrame(
            parent, text=" Audio Devices ", bg=_PANEL, fg=_TEXT_DIM,
            font=("Segoe UI", 9, "bold"), padx=14, pady=10, bd=1,
            relief="flat", highlightbackground=_BORDER, highlightthickness=1,
        )
        devices_frame.pack(fill="x", pady=(8, 6))
        devices_frame.grid_columnconfigure(1, weight=1)

        # Microphone (input)
        self.mic_var = tk.StringVar()
        self._mic_combo = self._add_dropdown(
            devices_frame, 0, "Microphone", self.mic_var, self._input_values(),
            self._on_mic_change,
        )

        # Virtual cable (TTS input)
        self.cable_var = tk.StringVar()
        self._cable_combo = self._add_dropdown(
            devices_frame, 1, "Virtual Cable", self.cable_var,
            self._output_values(include_default=False),
            self._on_cable_change,
        )

        # Speaker (output)
        self.speaker_var = tk.StringVar()
        self._speaker_combo = self._add_dropdown(
            devices_frame, 2, "Speaker", self.speaker_var,
            self._output_values(include_default=True),
            self._on_speaker_change,
        )

        # Toggles row
        toggles = tk.Frame(devices_frame, bg=_PANEL)
        toggles.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.allow_overlay_var = tk.BooleanVar(value=config.ALLOW_OVERLAY)
        chk_overlay = tk.Checkbutton(
            toggles, text="Overlay (Ctrl+Alt+T)",
            variable=self.allow_overlay_var,
            command=self._on_allow_overlay_toggle,
            bg=_PANEL, fg=_TEXT_DIM, selectcolor=_FIELD,
            activebackground=_PANEL, activeforeground=_TEXT,
            font=("Segoe UI", 8), anchor="w", bd=0, highlightthickness=0,
        )
        chk_overlay.pack(side="left")

        self.speakers_var = tk.BooleanVar(value=config.PLAY_ON_SPEAKERS)
        chk = tk.Checkbutton(
            toggles, text="Play on speakers",
            variable=self.speakers_var,
            command=self._on_speakers_toggle,
            bg=_PANEL, fg=_TEXT_DIM, selectcolor=_FIELD,
            activebackground=_PANEL, activeforeground=_TEXT,
            font=("Segoe UI", 8), anchor="w", bd=0, highlightthickness=0,
        )
        chk.pack(side="left", padx=(16, 0))

        self._sync_option_selections()

    def _build_config_status(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=_BG)
        bar.pack(fill="x", pady=(4, 0))

        # Small status dot + label.
        self._config_status_dot = tk.Canvas(
            bar, width=8, height=8, bg=_BG, highlightthickness=0,
        )
        self._config_status_dot.pack(side="left", padx=(2, 6))
        self._config_status_dot.create_oval(1, 1, 7, 7, fill=_OK, outline="",
                                            tags="dot")

        self._config_status_label = tk.Label(
            bar, text="Ready", bg=_BG, fg=_OK,
            font=("Segoe UI", 8), anchor="w", justify="left",
        )
        self._config_status_label.pack(side="left")

    def _add_dropdown(self, parent, row, label, var, values, on_change, has_refresh=False, refresh_command=None):
        lbl = tk.Label(
            parent, text=label, bg=_PANEL, fg=_MUTED,
            font=("Segoe UI", 8), anchor="e", width=12,
        )
        lbl.grid(row=row, column=0, sticky="e", pady=3, padx=(0, 6))

        if has_refresh:
            container = tk.Frame(parent, bg=_PANEL)
            container.grid(row=row, column=1, sticky="ew", pady=3)
            container.grid_columnconfigure(0, weight=1)

            combo = ttk.Combobox(
                container, textvariable=var, values=values, state="readonly",
                style="SpeechSkin.TCombobox", font=("Segoe UI", 9),
            )
            combo.grid(row=0, column=0, sticky="ew")
            combo.bind("<<ComboboxSelected>>", on_change)

            # Modern refresh button
            refresh_btn = tk.Button(
                container, text="⟳", command=refresh_command,
                bg=_FIELD, fg=_TEXT_DIM, font=("Segoe UI", 9, "bold"),
                padx=6, pady=2, relief="flat",
                activebackground=_ACCENT, activeforeground="#ffffff",
                cursor="hand2", bd=0,
            )
            refresh_btn.grid(row=0, column=1, padx=(6, 0), sticky="e")
        else:
            combo = ttk.Combobox(
                parent, textvariable=var, values=values, state="readonly",
                style="SpeechSkin.TCombobox", font=("Segoe UI", 9),
            )
            combo.grid(row=row, column=1, sticky="ew", pady=3)
            combo.bind("<<ComboboxSelected>>", on_change)

        return combo

    def _bind_keys(self) -> None:
        # Ctrl+Enter speaks (power-user shortcut).
        self.text.bind("<Control-Return>", self._on_speak)
        self.root.bind("<Control-c>", self._handle_quit)
        self.config_root.bind("<Control-c>", self._handle_quit)

    # ----------------------------------------------------- dropdown values

    def _engine_values(self) -> list[str]:
        return list(tts.available_engines())

    def _voice_values(self) -> list[str]:
        return [f"[{i}] {desc}" for i, desc in self._voices]

    def _input_values(self) -> list[str]:
        vals = [self._DEFAULT_OPTION]
        vals += [_device_label(i, name) for i, name in self._input_devices]
        return vals

    def _output_values(self, include_default: bool) -> list[str]:
        vals: list[str] = []
        if include_default:
            vals.append(self._DEFAULT_OPTION)
        vals += [_device_label(i, name) for i, name in self._output_devices]
        return vals

    def _sync_option_selections(self) -> None:
        """Set each dropdown's current value from the live config."""
        # Engine
        self.engine_var.set(tts.current_engine_key())

        # Voice — match by current voice index.
        voice_label = self._voice_label_for_current()
        if voice_label is not None:
            self.voice_var.set(voice_label)

        # Microphone
        self.mic_var.set(self._device_selection(
            config.MIC_DEVICE_INDEX, self._input_devices, default=True))

        # Cable
        self.cable_var.set(self._device_selection(
            config.CABLE_DEVICE_INDEX, self._output_devices, default=False))

        # Speaker
        self.speaker_var.set(self._device_selection(
            config.SPEAKER_DEVICE_INDEX, self._output_devices, default=True))

        # API Key
        engine_key = tts.current_engine_key()
        secret_name = f"{engine_key.upper()}_API_KEY"
        self.api_key_var.set(secrets.get(secret_name) or "")

    def _voice_label_for_current(self) -> str | None:
        idx = config.TTS_VOICE_INDEX
        for i, desc in self._voices:
            if i == idx:
                return f"[{i}] {desc}"
        if self._voices:
            first_i, first_desc = self._voices[0]
            return f"[{first_i}] {first_desc}"
        return None

    def _device_selection(self, index: int | None,
                          devices: list[tuple[int, str]],
                          default: bool) -> str:
        if index is None:
            return self._DEFAULT_OPTION if default else ""
        for i, name in devices:
            if i == index:
                return _device_label(i, name)
        return self._DEFAULT_OPTION if default else ""

    # -------------------------------------------------- dropdown callbacks

    @_safe
    def _on_engine_change(self, _event=None) -> None:
        name = self.engine_var.get()
        if not name or name == tts.current_engine_key():
            return
        try:
            engine = tts.set_engine(name)
        except Exception as exc:  # noqa: BLE001
            self._status(f"Engine switch failed: {exc}", _ERR)
            return
        # New engine → refresh voices and transcription availability.
        try:
            self._voices = tts.enumerate_voices()
        except Exception:  # noqa: BLE001
            self._voices = []
        self._voice_combo.config(values=self._voice_values())
        self._sync_option_selections()
        self._update_listen_availability()
        self._status(f"Engine -> {engine.name}", _OK)

    @_safe
    def _on_voice_change(self, _event=None) -> None:
        index = self._parse_index(self.voice_var.get())
        if index is None:
            return
        try:
            chosen = tts.select_voice(index)
        except Exception as exc:  # noqa: BLE001
            self._status(f"Voice select failed: {exc}", _ERR)
            return
        self._status(f"Voice -> {chosen}", _OK)

    def _toggle_api_key_visibility(self) -> None:
        if self._api_key_entry.cget("show") == "*":
            self._api_key_entry.config(show="")
            self._api_key_toggle_btn.config(text="👁", fg=_ACCENT)
        else:
            self._api_key_entry.config(show="*")
            self._api_key_toggle_btn.config(text="👁", fg=_MUTED)

    @_safe
    def _on_api_key_change(self, _event=None) -> None:
        engine_key = tts.current_engine_key()
        new_key = self.api_key_var.get().strip()

        secret_name = f"{engine_key.upper()}_API_KEY"
        current_saved_key = secrets.get(secret_name) or ""
        if new_key == current_saved_key:
            return

        secrets.set(secret_name, new_key)

        # Re-create the active engine so it uses the new key
        try:
            engine = tts.set_engine(engine_key)
            self._voices = tts.enumerate_voices()
            self._voice_combo.config(values=self._voice_values())
            self._sync_option_selections()
            self._update_listen_availability()
            self._status(f"API key updated for {engine.name}", _OK)
        except Exception as exc:
            self._status(f"Failed to update API key: {exc}", _ERR)

    @_safe
    def _on_mic_change(self, _event=None) -> None:
        index = self._device_value_index(self.mic_var.get())
        settings.set("MIC_DEVICE_INDEX", index)
        self._status(f"Microphone -> {self.mic_var.get()}", _OK)

    @_safe
    def _on_cable_change(self, _event=None) -> None:
        index = self._device_value_index(self.cable_var.get())
        if index is None:
            return
        settings.set("CABLE_DEVICE_INDEX", index)
        self._status(f"Virtual cable -> {self.cable_var.get()}", _OK)

    @_safe
    def _on_speaker_change(self, _event=None) -> None:
        index = self._device_value_index(self.speaker_var.get())
        settings.set("SPEAKER_DEVICE_INDEX", index)
        self._status(f"Speaker output -> {self.speaker_var.get()}", _OK)

    @_safe
    def _on_allow_overlay_toggle(self) -> None:
        settings.set("ALLOW_OVERLAY", bool(self.allow_overlay_var.get()))
        state = "enabled" if self.allow_overlay_var.get() else "disabled"
        self._status(f"Overlay -> {state}", _OK)

    @_safe
    def _on_speakers_toggle(self) -> None:
        settings.set("PLAY_ON_SPEAKERS", bool(self.speakers_var.get()))
        state = "on" if self.speakers_var.get() else "off"
        self._status(f"Play on speakers -> {state}", _OK)

    # ------------------------------------------------------- parse helpers

    @staticmethod
    def _parse_index(label: str) -> int | None:
        """Extract the leading ``[N]`` index from a dropdown label."""
        label = (label or "").strip()
        if label.startswith("[") and "]" in label:
            try:
                return int(label[1:label.index("]")])
            except ValueError:
                return None
        return None

    def _device_value_index(self, label: str) -> int | None:
        """Map a device dropdown label to its index (None for the default)."""
        if not label or label == self._DEFAULT_OPTION:
            return None
        return self._parse_index(label)

    # ----------------------------------------------------------- listen

    def _transcription_available(self) -> bool:
        try:
            return tts.supports_transcription()
        except Exception:  # noqa: BLE001
            return False

    def _update_listen_availability(self) -> None:
        if self._skin_active:
            self._skin_btn.config(state="normal")
        elif self._transcription_available():
            self._skin_btn.config(state="normal")
        else:
            self._skin_btn.config(state="disabled")

    def _transcribe_with_timeout(
        self,
        wav_bytes: bytes,
        timeout: int = 60,
        on_success: Callable[[str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Start transcription with a timeout mechanism."""
        result_queue: Queue[tuple[str, None] | tuple[None, Exception]] = Queue()
        on_success = on_success or self._on_transcribed
        on_error = on_error or self._on_transcribe_error

        def worker() -> None:
            try:
                text = tts.transcribe(wav_bytes)
                result_queue.put((text, None))
            except Exception as exc:  # noqa: BLE001
                result_queue.put((None, exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        # Schedule a check to see if the result is ready
        self._check_transcription_result(
            result_queue, timeout, on_success, on_error)

    def _check_transcription_result(
        self,
        result_queue: Queue,
        timeout: int,
        on_success: Callable[[str], None],
        on_error: Callable[[Exception], None],
        elapsed: int = 0,
    ) -> None:
        """Check if transcription result is ready; retry until timeout."""
        check_interval = 25  # milliseconds

        try:
            if not result_queue.empty():
                text, exc = result_queue.get_nowait()
                if exc is not None:
                    log.error("Transcription failed: %s", exc)
                    on_error(exc)
                else:
                    on_success(text)
                return

            if elapsed >= timeout * 1000:
                # Timeout reached
                log.error("Transcription timed out after %d seconds", timeout)
                on_error(
                    TimeoutError(f"Transcription did not complete within {timeout}s")
                )
                return

            # Schedule next check
            self.root.after(
                check_interval,
                lambda: self._check_transcription_result(
                    result_queue, timeout, on_success, on_error,
                    elapsed + check_interval,
                ),
            )
        except RuntimeError:
            log.debug("Main loop is not running, skipping transcription check")

    @_safe
    def _on_transcribed(self, text: str) -> None:
        if not text:
            self._status("Nothing was transcribed", _ERR)
            return
        self._append_text(text)
        self._status("Transcribed", _OK)

    def _append_text(self, text: str) -> None:
        existing = self.text.get("1.0", "end-1c")
        if existing and not existing.endswith((" ", "\n")):
            self.text.insert("end", " ")
        self.text.insert("end", text)

    @_safe
    def _on_transcribe_error(self, exc: Exception) -> None:
        self._status(f"Transcription error: {exc}", _ERR)

    # -------------------------------------------------------------- skin

    @_safe
    def _on_skin(self) -> None:
        if self._skin_active:
            self._stop_skin()
        else:
            self._start_skin()

    def _start_skin(self) -> None:
        if not self._transcription_available():
            self._status("Skin requires transcription support", _ERR)
            return
        if self._speaking:
            self._status("Wait for speaking to finish", _ERR)
            return
        if self._recorder is not None and self._recorder.is_recording:
            self._status("Stop recording first", _ERR)
            return
        self._skin_active = True
        self._skin_btn.config(text="■  STOP", bg=_SKIN_ACTIVE, fg="#ffffff")
        self._skin_ring.config(bg=_SKIN_ACTIVE)

        self._status("Skin PTT active", _OK)
        self._skin_ptt = _skin_ptt_mod.SkinPTT(
            on_status=self._on_ptt_status,
            on_transcribed=self._on_ptt_transcribed
        )
        if not self._skin_ptt.install():
            self._skin_active = False
            self._skin_ptt = None
            self._skin_btn.config(text="▶  SKIN", bg=_SKIN_IDLE, fg="#ffffff")
            self._skin_ring.config(bg=_SKIN_IDLE)
            self._status("Skin PTT: no key bound or failed to register", _ERR)

    def _stop_skin(self, status: str = "Skin stopped",
                    colour: str = _OK) -> None:
        self._skin_active = False
        if self._skin_ptt is not None:
            self._skin_ptt.teardown()
            self._skin_ptt = None
        self._skin_btn.config(text="▶  SKIN", bg=_SKIN_IDLE, fg="#ffffff")
        self._skin_ring.config(bg=_SKIN_IDLE)
        if self._transcription_available():
            self._skin_btn.config(state="normal")
        else:
            self._skin_btn.config(state="disabled")
        self._status(status, colour)

    def _on_ptt_transcribed(self, text: str) -> None:
        self._ui_callbacks.put(lambda: self._append_text(text))

    def _update_key_binds_info(self) -> None:
        if hasattr(self, "_main_skin_ptt_label"):
            self._main_skin_ptt_label.config(text=config.SKIN_PTT_KEY or "(not set)")
        if hasattr(self, "_main_target_ptt_label"):
            self._main_target_ptt_label.config(text=config.TARGET_PTT_KEY or "(not set)")

    # ------------------------------------------------------------- pynput key bindings

    def _start_key_bind(self, key_type: str) -> None:
        """Begin capturing a key/mouse press for binding. key_type is 'skin' or 'target'."""
        if self._binding_key is not None:
            return
        
        self._binding_key = key_type
        self._skin_ptt_bind_btn.config(state="disabled")
        self._target_ptt_bind_btn.config(state="disabled")
        if hasattr(self, "_main_skin_ptt_bind_btn"):
            self._main_skin_ptt_bind_btn.config(state="disabled")
        if hasattr(self, "_main_target_ptt_bind_btn"):
            self._main_target_ptt_bind_btn.config(state="disabled")
        
        if key_type == "skin":
            self._skin_ptt_label.config(text="Press key/mouse...", fg=_ACCENT_GLOW)
            if hasattr(self, "_main_skin_ptt_label"):
                self._main_skin_ptt_label.config(text="Press key/mouse...", fg=_ACCENT_GLOW)
        else:
            self._target_ptt_label.config(text="Press key/mouse...", fg=_ACCENT_GLOW)
            if hasattr(self, "_main_target_ptt_label"):
                self._main_target_ptt_label.config(text="Press key/mouse...", fg=_ACCENT_GLOW)
            
        self._status("Press any key or mouse button (ESC to cancel, backspace to clear)...", _BUSY)
        threading.Thread(target=self._capture_key_thread, daemon=True).start()

    def _capture_key_thread(self) -> None:
        """Thread worker that captures keyboard inputs or mouse clicks globally via pynput."""
        import time
        # Small delay to let the click that triggered the bind button release
        time.sleep(0.15)
        
        captured_key = None
        event_done = threading.Event()
        
        try:
            from pynput import keyboard, mouse
            
            def on_key_press(key):
                nonlocal captured_key
                # Check for ESC (cancel key)
                if key == keyboard.Key.esc:
                    captured_key = "esc"
                else:
                    if isinstance(key, keyboard.Key):
                        captured_key = key.name
                    elif isinstance(key, keyboard.KeyCode):
                        captured_key = key.char or f"vk_{key.vk}"
                event_done.set()
                return False  # Stops keyboard listener
                
            def on_mouse_click(x, y, button, pressed):
                nonlocal captured_key
                if pressed:
                    if button == mouse.Button.left:
                        captured_key = "left_mouse"
                    elif button == mouse.Button.right:
                        captured_key = "right_mouse"
                    elif button == mouse.Button.middle:
                        captured_key = "middle_mouse"
                    elif button == mouse.Button.x1:
                        captured_key = "mouse4"
                    elif button == mouse.Button.x2:
                        captured_key = "mouse5"
                    else:
                        captured_key = f"mouse_{button.name}"
                    event_done.set()
                    return False  # Stops mouse listener
            
            kb_listener = keyboard.Listener(on_press=on_key_press)
            mouse_listener = mouse.Listener(on_click=on_mouse_click)
            
            kb_listener.start()
            mouse_listener.start()
            
            event_done.wait()
            
            try:
                kb_listener.stop()
            except Exception:
                pass
            try:
                mouse_listener.stop()
            except Exception:
                pass
                
        except Exception as exc:
            log.error("Error in pynput capture thread: %s", exc)
            captured_key = None
            
        self._ui_callbacks.put(lambda: self._finish_key_bind(captured_key))

    def _finish_key_bind(self, key_name: str | None) -> None:
        key_type = self._binding_key
        self._binding_key = None
        
        self._skin_ptt_bind_btn.config(state="normal")
        self._target_ptt_bind_btn.config(state="normal")
        if hasattr(self, "_main_skin_ptt_bind_btn"):
            self._main_skin_ptt_bind_btn.config(state="normal")
        if hasattr(self, "_main_target_ptt_bind_btn"):
            self._main_target_ptt_bind_btn.config(state="normal")
        
        if not key_name or key_name.lower() == "esc":
            self._skin_ptt_label.config(text=config.SKIN_PTT_KEY or "(not set)", fg=_TEXT)
            self._target_ptt_label.config(text=config.TARGET_PTT_KEY or "(not set)", fg=_TEXT)
            self._status("Binding cancelled", _OK)
            self._update_key_binds_info()
            return
            
        if key_name.lower() == "backspace":
            if key_type == "skin":
                settings.set("SKIN_PTT_KEY", "")
                self._skin_ptt_label.config(text="(not set)", fg=_TEXT)
                self._status("Skin PTT Key cleared", _OK)
                active_prof = getattr(config, "ACTIVE_PROFILE", "Default")
                if hasattr(config, "PROFILES") and active_prof in config.PROFILES:
                    config.PROFILES[active_prof]["SKIN_PTT_KEY"] = ""
                    settings.set("PROFILES", config.PROFILES)
                if self._skin_active:
                    if self._skin_ptt is not None:
                        self._skin_ptt.teardown()
                        self._skin_ptt = _skin_ptt_mod.SkinPTT(
                            on_status=self._on_ptt_status,
                            on_transcribed=self._on_ptt_transcribed
                        )
                        self._skin_ptt.install()
            else:
                settings.set("TARGET_PTT_KEY", "")
                self._target_ptt_label.config(text="(not set)", fg=_TEXT)
                self._status("Target PTT Key cleared", _OK)
                active_prof = getattr(config, "ACTIVE_PROFILE", "Default")
                if hasattr(config, "PROFILES") and active_prof in config.PROFILES:
                    config.PROFILES[active_prof]["TARGET_PTT_KEY"] = ""
                    settings.set("PROFILES", config.PROFILES)
            self._update_key_binds_info()
            return
            
        if key_type == "skin":
            settings.set("SKIN_PTT_KEY", key_name)
            self._skin_ptt_label.config(text=key_name, fg=_TEXT)
            self._status(f"Skin PTT Key -> {key_name}", _OK)
            
            active_prof = getattr(config, "ACTIVE_PROFILE", "Default")
            if hasattr(config, "PROFILES") and active_prof in config.PROFILES:
                config.PROFILES[active_prof]["SKIN_PTT_KEY"] = key_name
                settings.set("PROFILES", config.PROFILES)
                
            if self._skin_active:
                if self._skin_ptt is not None:
                    self._skin_ptt.teardown()
                self._skin_ptt = _skin_ptt_mod.SkinPTT(
                    on_status=self._on_ptt_status,
                    on_transcribed=self._on_ptt_transcribed
                )
                self._skin_ptt.install()
        else:
            settings.set("TARGET_PTT_KEY", key_name)
            self._target_ptt_label.config(text=key_name, fg=_TEXT)
            self._status(f"Target PTT Key -> {key_name}", _OK)
            
            active_prof = getattr(config, "ACTIVE_PROFILE", "Default")
            if hasattr(config, "PROFILES") and active_prof in config.PROFILES:
                config.PROFILES[active_prof]["TARGET_PTT_KEY"] = key_name
                settings.set("PROFILES", config.PROFILES)
                
        self._update_key_binds_info()

    @_safe
    def _on_profile_change(self, _event=None) -> None:
        profile_name = self.profile_var.get()
        if not profile_name or profile_name not in config.PROFILES:
            return
            
        settings.set("ACTIVE_PROFILE", profile_name)
        profile_settings = config.PROFILES[profile_name]
        skin_key = profile_settings.get("SKIN_PTT_KEY", "")
        target_key = profile_settings.get("TARGET_PTT_KEY", "")
        
        settings.set("SKIN_PTT_KEY", skin_key)
        settings.set("TARGET_PTT_KEY", target_key)
        
        self._skin_ptt_label.config(text=skin_key or "(not set)")
        self._target_ptt_label.config(text=target_key or "(not set)")
        self._status(f"Switched to profile: {profile_name}", _OK)
        
        self._update_key_binds_info()
        
        if self._skin_active:
            if self._skin_ptt is not None:
                self._skin_ptt.teardown()
            self._skin_ptt = _skin_ptt_mod.SkinPTT(
                on_status=self._on_ptt_status,
                on_transcribed=self._on_ptt_transcribed
            )
            self._skin_ptt.install()

    @_safe
    def _on_add_profile(self) -> None:
        from tkinter import simpledialog
        name = simpledialog.askstring(
            "New Profile", "Enter profile name (e.g. game title):",
            parent=self.config_root
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
            
        if name in config.PROFILES:
            self._status("Profile already exists", _ERR)
            return
            
        config.PROFILES[name] = {
            "SKIN_PTT_KEY": "",
            "TARGET_PTT_KEY": "",
        }
        settings.set("PROFILES", config.PROFILES)
        
        self._profile_combo.config(values=list(config.PROFILES.keys()))
        self.profile_var.set(name)
        self._on_profile_change()

    @_safe
    def _on_remove_profile(self) -> None:
        name = self.profile_var.get()
        if name == "Default":
            self._status("Cannot delete the Default profile", _ERR)
            return
            
        if name not in config.PROFILES:
            return
            
        del config.PROFILES[name]
        settings.set("PROFILES", config.PROFILES)
        
        self._profile_combo.config(values=list(config.PROFILES.keys()))
        self.profile_var.set("Default")
        self._on_profile_change()



    def _on_ptt_status(self, status: str, message: str) -> None:
        self._ui_callbacks.put(lambda: self._apply_ptt_status(status, message))

    def _apply_ptt_status(self, status: str, message: str) -> None:
        if status == _skin_ptt_mod.STATUS_RECORDING:
            self._status(message, _REC)
        elif status == _skin_ptt_mod.STATUS_PROCESSING:
            self._status(message, _BUSY)
        elif status == _skin_ptt_mod.STATUS_PLAYING:
            self._status(message, _BUSY)
            self._ptt_playing = True
            self._update_play_btn_state()
        elif status == _skin_ptt_mod.STATUS_IDLE:
            self._status(message, _OK)
            if getattr(self, "_ptt_playing", False):
                self._ptt_playing = False
                self.text.delete("1.0", "end")
            self._update_play_btn_state()

    def _update_play_btn_state(self) -> None:
        if not hasattr(self, "_play_btn"):
            return
        is_playing = self._speaking or getattr(self, "_ptt_playing", False)
        if is_playing:
            self._play_btn.config(state="disabled")
        else:
            self._play_btn.config(state="normal")

    # ------------------------------------------------------------- speak

    @_safe
    def _on_speak(self, _event=None) -> str | None:
        """Power-user shortcut: Ctrl+Enter synthesises the text box."""
        if self._speaking or getattr(self, "_ptt_playing", False):
            return "break"
        text = self.text.get("1.0", "end-1c").strip()
        if not text:
            self._status("Nothing to speak", _ERR)
            return "break"
        self._speaking = True
        self._update_play_btn_state()
        self._status("Speaking...", _BUSY)
        threading.Thread(
            target=self._do_speak, args=(text,), daemon=True
        ).start()
        return "break"

    def _do_speak(self, text: str) -> None:
        """Worker thread: synthesise + play, then re-enable the UI."""
        try:
            speech.speak_to_devices(text)
        finally:
            self._ui_callbacks.put(self._on_speak_done)

    @_safe
    def _on_speak_done(self) -> None:
        self._speaking = False
        self.text.delete("1.0", "end")
        self._update_play_btn_state()
        self._status("Done", _OK)

    @_safe
    def _on_clear(self) -> None:
        self.text.delete("1.0", "end")
        self._status("Cleared", _OK)

    @_safe
    def _on_refresh_voices(self) -> None:
        if self._skin_active:
            self._status("Stop Skin first", _ERR)
            return
        self._status("Refreshing voices...", _BUSY)
        threading.Thread(target=self._do_refresh_voices, daemon=True).start()

    def _do_refresh_voices(self) -> None:
        try:
            tts.invalidate_cache()
            self._voices = tts.enumerate_voices()
            self._ui_callbacks.put(self._on_refresh_voices_done)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not refresh voices: %s", exc)
            self._ui_callbacks.put(lambda: self._status(f"Refresh failed: {exc}", _ERR))

    @_safe
    def _on_refresh_voices_done(self) -> None:
        self._voice_combo.config(values=self._voice_values())
        self._sync_option_selections()
        self._status("Voices refreshed", _OK)

    # ------------------------------------------------------------ status

    @_safe
    def _status(self, text: str, colour: str = _OK) -> None:
        # Update Main Window status
        if hasattr(self, "status_label"):
            self.status_label.config(text=text, fg=colour)
        if hasattr(self, "_status_dot"):
            try:
                self._status_dot.itemconfig("dot", fill=colour)
            except (tk.TclError, AttributeError):
                pass
                
        # Update Settings Window status
        if hasattr(self, "_config_status_label"):
            self._config_status_label.config(text=text, fg=colour)
        if hasattr(self, "_config_status_dot"):
            try:
                self._config_status_dot.itemconfig("dot", fill=colour)
            except (tk.TclError, AttributeError):
                pass
                
        self.root.update_idletasks()
        if hasattr(self, "config_root"):
            try:
                self.config_root.update_idletasks()
            except tk.TclError:
                pass

    # ------------------------------------------------------ window control

    def _center(self) -> None:
        try:
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            x = (sw - w) // 2
            y = (sh - h) // 3
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass

    @_safe
    def _show_config(self) -> None:
        """Transitions from MainWindow to SettingsWindow covering it seamlessly."""
        if self._skin_active:
            self._status("Stop Skin first", _ERR)
            return
            
        self.root.update_idletasks()
        w = 580
        h = 640
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        
        self.config_root.geometry(f"{w}x{h}+{x}+{y}")
        self.config_root.deiconify()
        self.config_root.lift()
        self.config_root.focus_force()
        self.root.withdraw()

    @_safe
    def _hide_config(self) -> None:
        """Transitions from SettingsWindow back to MainWindow covering it seamlessly."""
        self.config_root.update_idletasks()
        w = 580
        h = 500
        x = self.config_root.winfo_x()
        y = self.config_root.winfo_y()
        
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.config_root.withdraw()

    @_safe
    def _on_close(self) -> None:
        sd.request_shutdown()



    def pump_worker_events(self) -> None:
        """Run UI callbacks requested by worker threads."""
        while True:
            try:
                callback = self._ui_callbacks.get_nowait()
            except Empty:
                return
            try:
                callback()
            except Exception as exc:  # noqa: BLE001
                log.warning("Settings worker callback failed: %s", exc)

    @_safe
    def destroy(self) -> None:
        self._stop_skin("Skin stopped", _OK)
        try:
            if self._recorder is not None and self._recorder.is_recording:
                self._recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.config_root.destroy()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    @_safe
    def _handle_quit(self, _event=None) -> None:
        sd.request_shutdown()
