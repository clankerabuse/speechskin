# SpeechSkin

SpeechSkin is a lightweight, low-latency Windows utility that lets you speak in voice chats using high-quality AI text-to-speech (TTS) and speech-to-text (STT).


<img width="568" height="521" alt="image" src="https://github.com/user-attachments/assets/b22ee685-9b94-4981-b99f-2227d804ed70" />


## Core Concept

1. **Type to Speak**: Press `Ctrl+Alt+T` to open a floating overlay. Type a message, press Enter, and SpeechSkin speaks it through your virtual audio cable.
2. **Skin Mode**: Press the **SKIN** button to start a continuous loop: **Microphone Input ➔ ElevenLabs Speech-to-Text ➔ ElevenLabs Text-to-Speech ➔ Virtual Audio Cable**. You talk, and it speaks in your chosen AI voice!

---

## Quick Start

1. **Setup Environment**:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure Secrets**:
   Copy `secrets.env.example` to `secrets.env` and add your ElevenLabs API key:
   ```env
   ELEVENLABS_API_KEY=your_key_here
   ```

3. **Run**:
   Double-click `start.bat` or run:
   ```bash
   python speechskin.py
   ```

---

## Live Commands

Type these directly into the floating `Ctrl+Alt+T` overlay for quick controls:

- `/engine [name]` — Switch TTS engine
- `/voice <index>` — Change active voice
- `/audio input <idx>` — Set input virtual cable
- `/audio output <idx>` — Set speaker device
- `/audio speakers <on|off>` — Toggle speaker playback
- `/exit` — Quit the application

---

## Running Tests

Verify your installation with `pytest`:
```bash
python -m pytest
```

---

## Linux (PipeWire / Wayland)

SpeechSkin runs natively on Linux: no third-party driver is needed, because the
virtual cable is a PipeWire null sink the app creates at startup.

System packages (Arch):
```bash
sudo pacman -S tk portaudio pipewire pipewire-pulse
```
`tk` is what Python's `tkinter` links against; PortAudio backs `sounddevice`.

Run it:
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp secrets.env.example secrets.env   # add ELEVENLABS_API_KEY
.venv/bin/python speechskin.py       # or ./start.sh
```

The first start creates the sink `SpeechSkin` (check with
`pactl list short sinks`). In your voice-chat app pick **Monitor of SpeechSkin**
as the microphone — TTS goes to the sink, the app reads its monitor. The sink
is a runtime module, so start the app again after a PipeWire restart or make it
permanent with `~/.config/pipewire/pipewire.conf.d/10-speechskin.conf`:

```conf
context.objects = [
  { factory = adapter
    args = {
      factory.name     = support.null-audio-sink
      node.name        = speechskin
      node.description = "SpeechSkin"
      media.class      = "Audio/Sink"
      object.linger    = true
      audio.position   = [ FL FR ]
    }
  }
]
```

### Global hotkey

On Linux the app cannot grab global keys itself (the `keyboard` library needs
root and reads `/dev/input`), so the binding lives in the compositor and calls
`scripts/speechskin-toggle`, which sends `SIGUSR1` to the PID published in
`$XDG_RUNTIME_DIR/speechskin.pid`:

```lua
-- ~/.config/hypr/bindings.lua (Hyprland / Omarchy)
o.bind("CTRL ALT + T", "SpeechSkin overlay",
       "/path/to/speechskin/scripts/speechskin-toggle")
```

Tick **Overlay (Ctrl+Alt+T)** in the settings window first — `ALLOW_OVERLAY`
defaults to off and the signal is ignored while it is off. `SIGTERM` shuts the
app down cleanly and removes the pidfile.

### Window placement

The settings window is a normal window, so a tiling compositor will tile it.
To float it:

```conf
windowrulev2 = float, class:^(Toplevel)$, title:^(SpeechSkin)$
```

### Notes

* Fixed-rate output nodes (PipeWire nodes exposed through the JACK host API, and
  most virtual cables) only accept their own sample rate. Playback resamples to
  the device automatically, and streaming playback degrades to buffered playback
  on such devices rather than failing.
* Not ported yet: Skin mode's push-to-talk hooks (`skin_ptt.py`,
  `target_ptt.py`) use `pynput`, which only sees XWayland clients under Wayland.
  Typing TTS, the overlay, the virtual cable and mic mute all work.
