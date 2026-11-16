# SpeechSkin

Windows utility that lets you speak in voice chats with ElevenLabs TTS and STT,
routed into a virtual audio cable.

<img width="568" height="521" alt="SpeechSkin overlay" src="https://github.com/user-attachments/assets/b22ee685-9b94-4981-b99f-2227d804ed70" />

- **Type to speak** — `Ctrl+Alt+T` opens a floating overlay. Type, press Enter,
  and it speaks through the cable.
- **Skin mode** — the SKIN button loops **mic → ElevenLabs STT → ElevenLabs TTS
  → virtual audio cable**, so you talk and it answers in your chosen voice.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `secrets.env.example` to `secrets.env`, set `ELEVENLABS_API_KEY`, then
double-click `start.bat` (or run `python speechskin.py`).

## Overlay commands

`/engine [name]` · `/voice <index>` · `/audio input <idx>` ·
`/audio output <idx>` · `/audio speakers <on|off>` · `/exit`

Tests: `python -m pytest`
