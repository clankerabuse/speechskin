"""Tests for the engine registry, base class, and ElevenLabs backend."""

from __future__ import annotations

import json
import urllib.error

import pytest

from tts_overlay import engines
from tts_overlay.engines.base import TTSEngine
from tts_overlay.engines.elevenlabs import (
    ElevenLabsEngine,
    _encode_multipart,
    _extract_pcm_s16le_16,
    _parse_pcm_rate,
)
from tts_overlay.recorder import encode_wav


# --------------------------------------------------------------- registry


def test_available_lists_elevenlabs():
    assert "elevenlabs" in engines.available()


def test_create_default_returns_engine():
    engine = engines.create()
    assert isinstance(engine, TTSEngine)


def test_create_unknown_falls_back(caplog):
    engine = engines.create("does-not-exist")
    # Falls back to ElevenLabs rather than raising.
    assert isinstance(engine, ElevenLabsEngine)


def test_create_is_case_insensitive():
    engine = engines.create("ELEVENLABS")
    assert isinstance(engine, ElevenLabsEngine)


# ------------------------------------------------------------- base class


class MinimalEngine(TTSEngine):
    name = "Minimal"

    def enumerate_voices(self):
        return [(0, "v0")]

    def enumerate_audio_devices(self):
        return [(0, "d0")]

    def find_cable_device(self, keyword):
        return None

    def speak(self, text, device_index):
        return None


def test_base_supports_transcription_defaults_false():
    assert MinimalEngine().supports_transcription is False


def test_base_transcribe_raises_by_default():
    with pytest.raises(NotImplementedError):
        MinimalEngine().transcribe(b"data")


def test_base_supports_dual_output_default_true():
    assert MinimalEngine().supports_dual_output is True


def test_base_supports_streaming_defaults_false():
    assert MinimalEngine().supports_streaming is False


def test_base_speak_streaming_falls_back_to_speak():
    """Default speak_streaming must call speak() not raise."""
    spoken = []

    class TrackEngine(MinimalEngine):
        def speak(self, text, device_index):
            spoken.append((text, device_index))

    TrackEngine().speak_streaming("hello", 0)
    assert spoken == [("hello", 0)]


def test_elevenlabs_supports_streaming(el_engine):
    assert el_engine.supports_streaming is True


def test_elevenlabs_speak_streaming_calls_play_stream(el_engine, monkeypatch):
    """speak_streaming should call audioplayer.play_stream with PCM chunks."""
    import numpy as np
    from tts_overlay import audioplayer, config

    monkeypatch.setattr(config, "ELEVENLABS_OUTPUT_FORMAT", "pcm_24000")
    monkeypatch.setattr(config, "ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5")

    # Fake voice list so _resolve_voice_id returns something.
    monkeypatch.setattr(el_engine, "_voice_ids", ["fake-voice-id"])
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "fake-voice-id")

    pcm_chunk = np.zeros(256, dtype="<i2").tobytes()
    chunks_yielded = [pcm_chunk, pcm_chunk]

    def fake_iter_stream(*args, **kwargs):
        yield from chunks_yielded

    monkeypatch.setattr(el_engine, "_iter_stream_chunks", fake_iter_stream)

    play_calls = []

    def fake_play_stream(chunks, device_index, pcm_rate, should_stop=None):
        play_calls.append((list(chunks), device_index, pcm_rate))

    monkeypatch.setattr(audioplayer, "play_stream", fake_play_stream)

    el_engine.speak_streaming("hello", device_index=1)

    assert len(play_calls) == 1
    _chunks, dev, rate = play_calls[0]
    assert dev == 1
    assert rate == 24000
    assert _chunks == chunks_yielded


def test_elevenlabs_speak_streaming_fallback_for_non_pcm(
    el_engine, monkeypatch
):
    """speak_streaming must fall back to speak() for non-PCM formats."""
    from tts_overlay import config

    monkeypatch.setattr(config, "ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    spoken = []
    monkeypatch.setattr(el_engine, "speak",
                        lambda t, d: spoken.append((t, d)))

    el_engine.speak_streaming("test", device_index=0)
    assert spoken == [("test", 0)]


def test_base_current_voice_description(monkeypatch):
    from tts_overlay import config
    monkeypatch.setattr(config, "TTS_VOICE_INDEX", 0)
    assert MinimalEngine().current_voice_description() == "v0"


# -------------------------------------------------- pcm rate parsing


@pytest.mark.parametrize("fmt,expected", [
    ("pcm_24000", 24000),
    ("pcm_44100", 44100),
    ("pcm_16000", 16000),
    ("mp3_44100_128", None),
    ("", None),
    (None, None),
    ("pcm_notanumber", None),
])
def test_parse_pcm_rate(fmt, expected):
    assert _parse_pcm_rate(fmt) == expected


# -------------------------------------------------- multipart encoding


def test_encode_multipart_contains_field_and_file():
    body = _encode_multipart(
        "BOUNDARY",
        {"model_id": "scribe_v1"},
        "file", "audio.wav", b"\x00\x01\x02", "audio/wav",
    )
    assert b"--BOUNDARY" in body
    assert b'name="model_id"' in body
    assert b"scribe_v1" in body
    assert b'name="file"; filename="audio.wav"' in body
    assert b"audio/wav" in body
    assert b"\x00\x01\x02" in body
    # Properly terminated.
    assert body.rstrip().endswith(b"--BOUNDARY--")


def test_encode_multipart_multiple_fields():
    body = _encode_multipart(
        "B", {"a": "1", "b": "2"}, "file", "f.wav", b"x", "audio/wav",
    )
    assert b'name="a"' in body
    assert b'name="b"' in body


# ----------------------------------------------- ElevenLabs STT (mocked)


@pytest.fixture
def el_engine(monkeypatch):
    engine = ElevenLabsEngine()
    monkeypatch.setattr(engine, "_api_key", "fake-key")
    return engine


def test_elevenlabs_supports_transcription(el_engine):
    assert el_engine.supports_transcription is True


def test_transcribe_empty_returns_empty(el_engine):
    assert el_engine.transcribe(b"") == ""


def test_transcribe_parses_single_channel(el_engine, monkeypatch):
    def fake_multipart(*args, **kwargs):
        return json.dumps({"text": "  hello there  "}).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == "hello there"


# --------------------------------------------- pcm_s16le_16 extraction


def test_extract_pcm_from_16k_mono_wav():
    """A 16 kHz/16-bit/mono WAV yields raw PCM frames."""
    raw_pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"  # 4 int16 samples
    wav = encode_wav(raw_pcm, sample_rate=16000, channels=1)
    assert _extract_pcm_s16le_16(wav) == raw_pcm


def test_extract_pcm_rejects_wrong_rate():
    raw_pcm = b"\x01\x00\x02\x00"
    wav = encode_wav(raw_pcm, sample_rate=44100, channels=1)
    assert _extract_pcm_s16le_16(wav) is None


def test_extract_pcm_rejects_non_wav():
    assert _extract_pcm_s16le_16(b"not a wav at all") is None


def test_transcribe_uses_pcm_format_for_16k_mono(el_engine, monkeypatch):
    """When given 16k mono WAV, transcribe sends file_format=pcm_s16le_16."""
    raw_pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    wav = encode_wav(raw_pcm, sample_rate=16000, channels=1)

    captured = {}

    def fake_multipart(path, fields, file_field, filename, file_bytes,
                       file_content_type="audio/wav"):
        captured["fields"] = fields
        captured["file_bytes"] = file_bytes
        captured["filename"] = filename
        return json.dumps({"text": "ok"}).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    el_engine.transcribe(wav)

    assert captured["fields"].get("file_format") == "pcm_s16le_16"
    assert captured["file_bytes"] == raw_pcm
    assert captured["filename"] == "audio.pcm"


def test_transcribe_keeps_wav_for_non_16k(el_engine, monkeypatch):
    """A non-16k WAV is sent unchanged (no pcm_s16le_16 hint)."""
    raw_pcm = b"\x01\x00\x02\x00"
    wav = encode_wav(raw_pcm, sample_rate=44100, channels=1)

    captured = {}

    def fake_multipart(path, fields, file_field, filename, file_bytes,
                       file_content_type="audio/wav"):
        captured["fields"] = fields
        captured["file_bytes"] = file_bytes
        return json.dumps({"text": "ok"}).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    el_engine.transcribe(wav)

    assert "file_format" not in captured["fields"]
    assert captured["file_bytes"] == wav


def test_transcribe_parses_multichannel(el_engine, monkeypatch):
    def fake_multipart(*args, **kwargs):
        return json.dumps({
            "transcripts": [{"text": "left"}, {"text": "right"}]
        }).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == "left right"


def test_transcribe_without_key_raises(monkeypatch):
    engine = ElevenLabsEngine()
    monkeypatch.setattr(engine, "_api_key", "")
    monkeypatch.setattr("tts_overlay.secrets.get", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        engine.transcribe(b"wavbytes")


def test_transcribe_http_error_raises(el_engine, monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.HTTPError(
            "url", 422, "Unprocessable", {}, _FakeFp(b"bad audio")
        )

    monkeypatch.setattr(el_engine, "_request_multipart", boom)
    with pytest.raises(RuntimeError):
        el_engine.transcribe(b"wavbytes")


def test_transcribe_invalid_json_raises(el_engine, monkeypatch):
    monkeypatch.setattr(
        el_engine, "_request_multipart", lambda *a, **k: b"not json"
    )
    with pytest.raises(RuntimeError):
        el_engine.transcribe(b"wavbytes")


def test_transcribe_removes_parentheses(el_engine, monkeypatch):
    """Transcription removes parentheses and their content."""
    def fake_multipart(*args, **kwargs):
        return json.dumps({"text": "hello (world)"}).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == "hello"


def test_transcribe_removes_brackets(el_engine, monkeypatch):
    """Transcription removes square brackets and their content."""
    def fake_multipart(*args, **kwargs):
        return json.dumps({"text": "hello [world]"}).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == "hello"


def test_transcribe_removes_braces(el_engine, monkeypatch):
    """Transcription removes curly braces and their content."""
    def fake_multipart(*args, **kwargs):
        return json.dumps({"text": "hello {world}"}).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == "hello"


def test_transcribe_removes_mixed_brackets(el_engine, monkeypatch):
    """Transcription removes all bracket types from the same text."""
    def fake_multipart(*args, **kwargs):
        return json.dumps({
            "text": "This is (a) test [with] brackets {and} braces"
        }).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == "This is test brackets braces"


def test_transcribe_preserves_quotes(el_engine, monkeypatch):
    """Transcription preserves quoted text."""
    def fake_multipart(*args, **kwargs):
        return json.dumps({
            "text": 'He said "hello" (muttered)'
        }).encode("utf-8")

    monkeypatch.setattr(el_engine, "_request_multipart", fake_multipart)
    assert el_engine.transcribe(b"wavbytes") == 'He said "hello"'


def test_startup_check_without_key_raises(monkeypatch):
    engine = ElevenLabsEngine()
    monkeypatch.setattr("tts_overlay.secrets.get", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        engine.startup_check()


def test_startup_check_with_key_passes(monkeypatch):
    engine = ElevenLabsEngine()
    monkeypatch.setattr("tts_overlay.secrets.get", lambda *a, **k: "sk_x")
    engine.startup_check()  # should not raise


# ------------------------------------ persistent connection retry policy


class _FakeResponse:
    """Minimal ``http.client.HTTPResponse`` stand-in."""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self, *args):
        return self._body

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def close(self):
        pass


class _FakeConn:
    """Minimal ``HTTPSConnection`` stand-in that records every request."""

    def __init__(self, response=None, error=None):
        self.requests = []
        self._response = response
        self._error = error
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path))
        if self._error is not None:
            raise self._error

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


def _conns(*conns):
    """Return a ``_get_conn`` replacement yielding *conns* in order."""
    pending = list(conns)
    return lambda: pending.pop(0)


def test_http_error_is_not_retried(el_engine, monkeypatch):
    """A 4xx/5xx must surface immediately instead of being resent.

    ``urllib.error.HTTPError`` subclasses ``OSError``, so it used to be caught
    by the connection-error retry: every failed API call was issued twice.
    """
    conn = _FakeConn(_FakeResponse(429, b'{"detail":"rate limited"}'))
    monkeypatch.setattr(el_engine, "_get_conn", lambda: conn)

    with pytest.raises(urllib.error.HTTPError) as info:
        el_engine._request("POST", "/text-to-speech/voice", body={"text": "hi"})

    assert info.value.code == 429
    assert info.value.read() == b'{"detail":"rate limited"}'
    assert len(conn.requests) == 1


def test_connection_error_is_still_retried(el_engine, monkeypatch):
    """A genuine connection failure is still retried once on a fresh conn."""
    dead = _FakeConn(error=ConnectionResetError("peer reset"))
    live = _FakeConn(_FakeResponse(200, b'{"voices": []}'))
    monkeypatch.setattr(el_engine, "_get_conn", _conns(dead, live))

    assert el_engine._request("GET", "/voices") == b'{"voices": []}'
    assert len(dead.requests) == 1
    assert len(live.requests) == 1


class _FakeFp:
    """Minimal file-like object for HTTPError.read()."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self, *args):
        return self._data

    def close(self):
        pass





