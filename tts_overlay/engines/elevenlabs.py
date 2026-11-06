"""ElevenLabs cloud text-to-speech backend.

Calls the ElevenLabs Text-to-Speech REST API, receives audio bytes, and plays
them on a chosen PortAudio output device via :mod:`tts_overlay.audioplayer`.

Networking uses the standard library (``urllib``) so no extra HTTP dependency
is required; audio playback uses ``sounddevice`` + ``soundfile``.

Device indices for this engine are PortAudio indices (see
``tts_overlay.audioplayer``).

API reference: https://elevenlabs.io/docs/api-reference/text-to-speech
"""

from __future__ import annotations

import http.client
import io
import json
import re
import ssl
import threading
import urllib.error
import urllib.request
import uuid
import wave
from collections.abc import Iterator

from .. import audioplayer, config, logutil, secrets
from .. import shutdown as sd
from .base import TTSEngine

log = logutil.get(__name__)

_API_ROOT = "https://api.elevenlabs.io/v1"
_API_HOST = "api.elevenlabs.io"
# Remove parentheses (), brackets [], braces {}, and their inner content
_BRACKETS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|{[^}]*}")


class ElevenLabsEngine(TTSEngine):
    name = "ElevenLabs"

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._voices_cache: list[tuple[int, str]] | None = None
        self._voice_ids: list[str] = []
        # Persistent HTTPS connection for keep-alive (saves ~100-150 ms TLS
        # handshake on every PTT press after the first).
        self._conn: http.client.HTTPSConnection | None = None
        self._conn_lock = threading.Lock()

    def invalidate_cache(self) -> None:
        self._voices_cache = None

    # -- credentials / startup --

    def _key(self) -> str | None:
        if self._api_key is None:
            self._api_key = secrets.get("ELEVENLABS_API_KEY")
        return self._api_key or None

    def startup_check(self) -> None:
        if not self._key():
            raise RuntimeError(
                "ElevenLabs API key not found. Add it to your secrets file "
                f"({secrets.SECRETS_PATH.name}) as:\n"
                "    ELEVENLABS_API_KEY=your_key_here\n"
                "  or set the ELEVENLABS_API_KEY environment variable."
            )

    # -- HTTP helpers --

    # -- persistent connection helpers --

    def _get_conn(self) -> http.client.HTTPSConnection:
        """Return a persistent HTTPS connection, creating one if needed."""
        if self._conn is not None:
            return self._conn
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            _API_HOST, timeout=config.ELEVENLABS_TIMEOUT, context=ctx,
        )
        self._conn = conn
        return conn

    def _persistent_request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
        *,
        stream: bool = False,
    ) -> http.client.HTTPResponse:
        """Send a request over the keep-alive connection.

        Returns the HTTPResponse (caller must read the body).  On any
        connection-level error the connection is recycled and retried once.
        """
        hdrs = dict(headers) if headers else {}
        hdrs.setdefault("xi-api-key", self._key() or "")
        hdrs.setdefault("Connection", "keep-alive")

        for attempt in range(2):
            try:
                conn = self._get_conn()
                conn.request(method, f"/v1{path}", body=body, headers=hdrs)
                resp = conn.getresponse()
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                log.debug("Persistent conn failed (attempt %d): %s", attempt + 1, exc)
                # Recycle connection and retry once.
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None
                if attempt == 1:
                    raise
                continue

            # An HTTP error is a completed exchange, not a broken connection.
            # Retrying it would resend POSTs (synthesis, uploads) and hammer
            # rate limits. urllib's HTTPError subclasses OSError, so this
            # check must live *outside* the except clause above.
            if resp.status >= 400:
                resp_body = resp.read()
                raise urllib.error.HTTPError(
                    f"{_API_ROOT}{path}", resp.status,
                    resp_body.decode("utf-8", "replace"),
                    dict(resp.getheaders()), io.BytesIO(resp_body),
                )
            return resp
        raise RuntimeError("unreachable")

    def _request(self, method: str, path: str, body: dict | None = None,
                 accept: str = "application/json") -> bytes:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self._conn_lock:
            resp = self._persistent_request(method, path, body=data, headers=headers)
            return resp.read()

    def _request_multipart(self, path: str, fields: dict[str, str],
                           file_field: str, filename: str,
                           file_bytes: bytes,
                           file_content_type: str = "audio/wav") -> bytes:
        """POST a multipart/form-data request and return the response body."""
        boundary = f"----speechskin{uuid.uuid4().hex}"
        body = _encode_multipart(
            boundary, fields, file_field, filename, file_bytes,
            file_content_type,
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        with self._conn_lock:
            resp = self._persistent_request("POST", path, body=body, headers=headers)
            return resp.read()

    # -- voices --

    def enumerate_voices(self) -> list[tuple[int, str]]:
        if self._voices_cache is not None:
            return self._voices_cache
        if not self._key():
            return []
        try:
            raw = self._request("GET", "/voices")
            payload = json.loads(raw)
            all_voices = payload.get("voices", [])
            # Keep only voices the user has personally chosen:
            #   - is_owner=True      → voices they created/cloned themselves
            #   - is_bookmarked=True → library voices they added to My Voices
            # This excludes ElevenLabs' built-in premade/default voices and
            # any library copies that others may have added to the account.
            voices = [
                v for v in all_voices
                if v.get("is_owner") or v.get("is_bookmarked")
            ]
            result: list[tuple[int, str]] = []
            self._voice_ids = []
            for i, v in enumerate(voices):
                vid = v.get("voice_id", "")
                name = v.get("name", vid)
                self._voice_ids.append(vid)
                result.append((i, f"{name}  ({vid})"))
            self._voices_cache = result
            return result
        except urllib.error.HTTPError as exc:
            log.error("ElevenLabs voice list failed (HTTP %s): %s",
                      exc.code, exc.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            log.error("ElevenLabs voice list failed: %s", exc)
        return []

    def select_voice(self, index: int) -> str:
        """Pin the ElevenLabs voice at *index* by its voice id and persist it.

        ElevenLabs resolves voices by id (``ELEVENLABS_VOICE_ID``), which takes
        priority over ``TTS_VOICE_INDEX``, so we store both: the id for actual
        selection and the index for banner highlighting.
        """
        from .. import settings
        if not self._voice_ids:
            self.enumerate_voices()
        if not (0 <= index < len(self._voice_ids)):
            raise ValueError(f"voice #{index} is out of range")
        voice_id = self._voice_ids[index]
        settings.set("ELEVENLABS_VOICE_ID", voice_id)
        settings.set("TTS_VOICE_INDEX", int(index))
        voices = dict(self.enumerate_voices())
        return voices.get(index, voice_id)

    def current_voice_description(self) -> str:
        if config.ELEVENLABS_VOICE_ID:
            for _idx, desc in self.enumerate_voices():
                if config.ELEVENLABS_VOICE_ID in desc:
                    return desc
            return config.ELEVENLABS_VOICE_ID
        voices = dict(self.enumerate_voices())
        return voices.get(config.TTS_VOICE_INDEX,
                          f"Voice #{config.TTS_VOICE_INDEX}")

    def _resolve_voice_id(self) -> str:
        """Pick the voice id to use.

        Priority: an explicit ELEVENLABS_VOICE_ID in config, otherwise the
        voice at TTS_VOICE_INDEX from the fetched voice list.
        """
        if config.ELEVENLABS_VOICE_ID:
            return config.ELEVENLABS_VOICE_ID
        if not self._voice_ids:
            self.enumerate_voices()  # populate _voice_ids
        idx = config.TTS_VOICE_INDEX
        if 0 <= idx < len(self._voice_ids):
            return self._voice_ids[idx]
        if self._voice_ids:
            return self._voice_ids[0]
        return ""

    @property
    def supports_streaming(self) -> bool:
        return True

    # -- audio devices (delegated to PortAudio) --

    def enumerate_audio_devices(self) -> list[tuple[int, str]]:
        return audioplayer.enumerate_output_devices()

    def find_cable_device(self, keyword: str) -> int | None:
        return audioplayer.find_output_device(keyword)

    # -- synthesis --

    def speak(self, text: str, device_index: int | None | list[int | None]) -> None:
        try:
            voice_id = self._resolve_voice_id()
            if not voice_id:
                log.error("No ElevenLabs voice available. Set "
                          "ELEVENLABS_VOICE_ID in config or check your key.")
                return

            body = {
                "text": text,
                "model_id": config.ELEVENLABS_MODEL_ID,
                "voice_settings": {
                    "stability": config.ELEVENLABS_STABILITY,
                    "similarity_boost": config.ELEVENLABS_SIMILARITY_BOOST,
                },
            }
            path = (
                f"/text-to-speech/{voice_id}"
                f"?output_format={config.ELEVENLABS_OUTPUT_FORMAT}"
            )
            audio = self._request("POST", path, body=body, accept="audio/*")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 402 and "paid_plan_required" in detail:
                log.error(
                    "ElevenLabs rejected voice %s: this is a community "
                    "*Library* voice, which the API blocks on the free plan. "
                    "Set ELEVENLABS_VOICE_ID in config.py to a Default voice "
                    "(e.g. Rachel = 21m00Tcm4TlvDq8ikWAM), or upgrade to the "
                    "Starter plan to use Library voices.", voice_id)
            else:
                log.error("ElevenLabs synthesis failed (HTTP %s): %s",
                          exc.code, detail)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("ElevenLabs synthesis failed: %s", exc)
            return

        # ElevenLabs "pcm_<rate>" formats are raw headerless 16-bit mono PCM,
        # which soundfile can't sniff. Tell the player the sample rate so it
        # can decode the raw stream. Container formats (mp3/wav/...) decode
        # normally with pcm_rate=None.
        pcm_rate = _parse_pcm_rate(config.ELEVENLABS_OUTPUT_FORMAT)
        audioplayer.play_bytes(
            audio, device_index,
            should_stop=sd.shutdown_event.is_set,
            pcm_rate=pcm_rate,
        )

    def _iter_stream_chunks(self, path: str, body: dict) -> Iterator[bytes]:
        """Open the ElevenLabs streaming endpoint and yield raw bytes chunks."""
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "audio/*",
            "Content-Type": "application/json",
        }
        with self._conn_lock:
            resp = self._persistent_request("POST", path, body=data, headers=headers)
        # Read outside the lock so playback isn't blocked by other calls.
        while True:
            # 1024-byte reads so the prebuffer fills on the very first
            # chunk, minimising time-to-first-sound.
            chunk = resp.read(1024)
            if not chunk:
                break
            yield chunk

    def speak_streaming(self, text: str, device_index: int | None | list[int | None]) -> None:
        """Speak *text* with minimal latency by streaming PCM from the API.

        Uses the ``/text-to-speech/{voice_id}/stream`` endpoint so playback
        starts as soon as the first audio bytes arrive (~100 ms) rather than
        waiting for the entire synthesis to complete.

        Falls back to the standard :meth:`speak` if the output format is not
        raw PCM (streaming requires a known sample rate to open the output
        stream before the full response arrives).
        """
        pcm_rate = _parse_pcm_rate(config.ELEVENLABS_OUTPUT_FORMAT)
        if pcm_rate is None:
            # Non-PCM format: can't stream without a container header, fall back.
            log.debug("speak_streaming: non-PCM format, falling back to speak()")
            self.speak(text, device_index)
            return

        try:
            voice_id = self._resolve_voice_id()
            if not voice_id:
                log.error("No ElevenLabs voice available.")
                return

            body = {
                "text": text,
                "model_id": config.ELEVENLABS_MODEL_ID,
                "voice_settings": {
                    "stability": config.ELEVENLABS_STABILITY,
                    "similarity_boost": config.ELEVENLABS_SIMILARITY_BOOST,
                },
            }
            path = (
                f"/text-to-speech/{voice_id}/stream"
                f"?output_format={config.ELEVENLABS_OUTPUT_FORMAT}"
                f"&optimize_streaming_latency=3"
            )
            chunks = self._iter_stream_chunks(path, body)
            audioplayer.play_stream(
                chunks,
                device_index,
                pcm_rate=pcm_rate,
                should_stop=sd.shutdown_event.is_set,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 402 and "paid_plan_required" in detail:
                log.error(
                    "ElevenLabs rejected voice %s: paid plan required.",
                    self._resolve_voice_id())
            else:
                log.error("ElevenLabs streaming failed (HTTP %s): %s",
                          exc.code, detail)
        except Exception as exc:  # noqa: BLE001
            log.error("ElevenLabs streaming failed: %s", exc)

    # -- speech-to-text --

    @property
    def supports_transcription(self) -> bool:
        return True

    def transcribe(self, wav_bytes: bytes) -> str:
        """Transcribe WAV *wav_bytes* via the ElevenLabs speech-to-text API.

        Configured to:
        - Force English language transcription (languageCode=eng, ISO 639-3)
        - Suppress background event tags (e.g., laughter, music)
        - Remove all brackets and their inner content from the result

        When the input is 16 kHz / 16-bit / mono PCM (which the recorder
        produces) we strip the WAV header and send raw PCM with
        ``file_format=pcm_s16le_16``.  ElevenLabs documents this as lower
        latency than passing an encoded waveform, shaving time off the gap
        between speaking and hearing the reply.
        """
        if not wav_bytes:
            return ""
        if not self._key():
            raise RuntimeError("ElevenLabs API key not found.")

        fields = {
            "model_id": config.ELEVENLABS_STT_MODEL_ID,
            "tagAudioEvents": "false",
            "languageCode": "eng",
        }
        file_bytes = wav_bytes
        filename = "audio.wav"
        file_content_type = "audio/wav"

        pcm = _extract_pcm_s16le_16(wav_bytes)
        if pcm is not None:
            # Low-latency raw PCM path.
            fields["file_format"] = "pcm_s16le_16"
            file_bytes = pcm
            filename = "audio.pcm"
            file_content_type = "application/octet-stream"

        try:
            raw = self._request_multipart(
                "/speech-to-text",
                fields=fields,
                file_field="file",
                filename=filename,
                file_bytes=file_bytes,
                file_content_type=file_content_type,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log.error("ElevenLabs transcription failed (HTTP %s): %s",
                      exc.code, detail)
            raise RuntimeError(f"Transcription failed (HTTP {exc.code})") from exc
        except Exception as exc:  # noqa: BLE001
            log.error("ElevenLabs transcription failed: %s", exc)
            raise

        try:
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not parse transcription response: %s", exc)
            raise RuntimeError("Invalid transcription response") from exc

        # Single-channel response carries the text directly; multichannel nests
        # it under a 'transcripts' list. Handle both.
        if isinstance(payload, dict):
            if "text" in payload:
                return _clean_transcription(payload.get("text") or "")
            transcripts = payload.get("transcripts")
            if isinstance(transcripts, list) and transcripts:
                parts = [t.get("text", "") for t in transcripts
                         if isinstance(t, dict)]
                return _clean_transcription(" ".join(p for p in parts if p))
        return ""





def _encode_multipart(boundary: str, fields: dict[str, str],
                      file_field: str, filename: str, file_bytes: bytes,
                      file_content_type: str) -> bytes:
    """Build a multipart/form-data request body."""
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(b"--" + boundary.encode("ascii") + crlf)
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            + crlf + crlf
        )
        parts.append(str(value).encode("utf-8") + crlf)
    parts.append(b"--" + boundary.encode("ascii") + crlf)
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"'.encode("utf-8") + crlf
    )
    parts.append(f"Content-Type: {file_content_type}".encode("utf-8")
                 + crlf + crlf)
    parts.append(file_bytes + crlf)
    parts.append(b"--" + boundary.encode("ascii") + b"--" + crlf)
    return b"".join(parts)


def _parse_pcm_rate(output_format: str) -> int | None:
    """Return the sample rate for a ``pcm_<rate>`` format, else None."""
    fmt = (output_format or "").lower()
    if fmt.startswith("pcm_"):
        try:
            return int(fmt.split("_", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def _extract_pcm_s16le_16(wav_bytes: bytes) -> bytes | None:
    """Return raw PCM frames if *wav_bytes* is 16 kHz / 16-bit / mono WAV.

    ElevenLabs' ``pcm_s16le_16`` STT input format is lower latency than an
    encoded waveform, but it requires exactly 16 kHz, 16-bit, single-channel,
    little-endian PCM.  Returns ``None`` for any other format so the caller
    falls back to sending the original container untouched.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            if (wav.getframerate() == 16000
                    and wav.getsampwidth() == 2
                    and wav.getnchannels() == 1):
                return wav.readframes(wav.getnframes())
    except Exception:  # noqa: BLE001
        return None
    return None


def _clean_transcription(text: str) -> str:
    """Remove brackets and their inner content, then collapse whitespace.
    
    Removes: (), [], {}, and all text between matching bracket pairs.
    Preserves quotes as requested.
    """
    cleaned = _BRACKETS_RE.sub("", text or "")
    return " ".join(cleaned.split()).strip()
