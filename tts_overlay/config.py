"""User-configurable settings for the TTS overlay.

Edit the values here to change behaviour. Everything else is implementation.
"""

import sys

# Global hotkey that toggles the overlay.
HOTKEY = "ctrl+alt+t"

# Which TTS backend to use. Currently only "elevenlabs" is supported.
# You can change the engine at runtime from the overlay by typing "/engine"
# (cycles) or "/engine <name>"; that choice is saved to settings.json and
# overrides this value on the next launch.
TTS_ENGINE = "elevenlabs"

# Virtual cable detection — audio device names are searched for this substring.
# Case-insensitive. On Windows the cable is a driver you install ("cable" =
# VB-Cable, "virtual" = Virtual Audio Cable, "voicemeeter" = Voicemeeter). On
# Linux tts_overlay.cable creates a PipeWire null sink whose *description* is
# "SpeechSkin", and PortAudio shows that description, not the node name.
CABLE_DEVICE_KEYWORD = "cable" if sys.platform == "win32" else "speechskin"

# Index of the audio device to route TTS into (the virtual cable / "input").
# None = auto-detect by CABLE_DEVICE_KEYWORD at startup.
# Use /audio input <index> at runtime to change and persist this.
CABLE_DEVICE_INDEX: int | None = None

# Enable the TTS overlay triggered by Ctrl+Alt+T hotkey.
ALLOW_OVERLAY = False

# Also play through your speakers so you can hear the TTS yourself?
PLAY_ON_SPEAKERS = True

# Index of the speaker output device. None = system default.
# Use /audio output <index> at runtime to change and persist this.
SPEAKER_DEVICE_INDEX: int | None = None

# Shared voice settings.
TTS_VOICE_INDEX = 0     # Index of the voice in the startup list to use.
                        # For ElevenLabs this indexes your fetched voices,
                        # unless ELEVENLABS_VOICE_ID below is set.

# --- ElevenLabs settings (only used when TTS_ENGINE = "elevenlabs") ---
# Put your API key in secrets.env (ELEVENLABS_API_KEY=...), not here.
#
# TIP: instead of editing the id below by hand, open the overlay and type
# "/voice" to browse your account's voices in a dropdown and pick one with the
# arrow keys + Enter. The choice is saved to settings.json and overrides this
# value on the next launch ("/voices" just lists them).
#
# Pin a specific voice by id. IMPORTANT: on the FREE plan the API can only use
# "premade" voices, NOT "professional"/community Library voices (those return
# HTTP 402 paid_plan_required). The id below is "Sarah", a premade voice that
# works on the free plan. Other premade voice ids:
#   Sarah   EXAVITQu4vr4xnSDxMaL      George  JBFqnCBsd6RMkjVDRZzb
#   Brian   nPczCjzI2devNBz1zQrb      Charlie IKne3meq5aSn9XLyUdCD
#   Lily    pFZP5JQG7iQjIQuC4Bku      Adam    pNInz6obpgDQGcFmaJgB
#   Roger   CwhRBWXzGAHq8TQ4Fs17      Eric    cjVigY5qzO86Huf0OWal
# The startup banner lists every voice your account can use (with its index).
# (Library voices like 8WqHCYyrnUqoK70Px5EJ need the Starter plan or higher.)
# Leave "" to instead use TTS_VOICE_INDEX against your fetched voice list.
ELEVENLABS_VOICE_ID = "Cvv0EXhC1Zv7b4a2QfWl"
# Model. "eleven_multilingual_v2" (quality) or "eleven_turbo_v2_5"/"eleven_flash_v2_5"
# (lower latency, good for live chat). "eleven_flash_v2_5" has the lowest
# time-to-first-byte (~75 ms) which makes streaming feel near-instant.
ELEVENLABS_MODEL_ID = "eleven_flash_v2_5"
# Voice tuning (0.0 - 1.0).
ELEVENLABS_STABILITY = 0.5
ELEVENLABS_SIMILARITY_BOOST = 0.75
# Audio format requested from the API. We decode this with soundfile, so a WAV
# format is safest (no MP3 codec needed). Examples: "pcm_24000", "pcm_44100".
ELEVENLABS_OUTPUT_FORMAT = "pcm_24000"
# Network timeout (seconds) for API calls.
ELEVENLABS_TIMEOUT = 30
# Speech-to-text model used by the "Listen" button and Skin mode.
# "scribe_v2" is the current state-of-the-art (more accurate than v1) and is
# used here for the best transcription quality.  "scribe_v1" remains available
# as a fallback if v2 is ever unavailable on your plan.
ELEVENLABS_STT_MODEL_ID = "scribe_v2"

# --- Microphone / speech-to-text capture ---
# Index of the microphone input device. None = system default input device.
# Use the "Listen" dropdown in the settings window to change this.
MIC_DEVICE_INDEX: int | None = None

# --- Push-to-Talk settings ---
# Hotkey the user holds to record their voice for skin processing.
# Supports any key/combo the `keyboard` library accepts.
# Blank by default — configured per-profile in the settings window.
SKIN_PTT_KEY = ""

# Key to hold down in the target app while processed audio plays.
# Pressed/released programmatically. Blank by default.
TARGET_PTT_KEY = ""

# Active profile name for PTT key bindings.
ACTIVE_PROFILE = "Default"

# Profile key bindings. Stored as {profile_name: {"SKIN_PTT_KEY": ..., "TARGET_PTT_KEY": ...}}
PROFILES = {
    "Default": {
        "SKIN_PTT_KEY": "",
        "TARGET_PTT_KEY": "",
    }
}

# Overlay appearance.
OVERLAY_ALPHA = 0.85
OVERLAY_WIDTH_PX = 620
OVERLAY_HEIGHT_PX = 66
OVERLAY_Y_OFFSET = 180  # distance from top of screen

# How often (seconds) the watchdog checks the keyboard listener is alive.
WATCHDOG_INTERVAL = 2.0

# How often (seconds) the main loop pumps Tk events.
MAIN_LOOP_INTERVAL = 0.01
