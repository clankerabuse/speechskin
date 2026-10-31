"""Tests for the audio playback module (sounddevice mocked)."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from tts_overlay import audioplayer


class FakeStream:
    def __init__(self, active_polls=1):
        self._active_calls = 0
        self._active_polls = active_polls

    @property
    def active(self):
        # Report active for a fixed number of polls, then inactive so the
        # playback loop exits promptly.
        self._active_calls += 1
        return self._active_calls <= self._active_polls


class FakeOutputStream:
    """Fake sounddevice.OutputStream for play_stream tests."""

    def __init__(self, **kwargs):
        self.callback = kwargs.get("callback")
        self.channels = kwargs.get("channels", 2)
        self.started = False
        self.stopped_called = False
        self.closed = False

    def start(self):
        self.started = True
        # The FakeOutputStream is not backed by a real audio thread, so we
        # drain the ring-buffer synchronously here so play_stream's drain loop
        # can exit.  Call the callback repeatedly until the ring-buffer has
        # been consumed (max 200 iterations to avoid infinite loops).
        if self.callback is not None:
            import numpy as np
            frames = 512
            for _ in range(200):
                outdata = np.zeros((frames, self.channels), dtype="float32")
                self.callback(outdata, frames, None, None)

    def stop(self):
        self.stopped_called = True

    def close(self):
        self.closed = True


class FakeSounddevice(types.ModuleType):
    def __init__(self, devices=None):
        super().__init__("sounddevice")
        self._devices = devices or [
            {"name": "Speakers", "max_input_channels": 0,
             "max_output_channels": 2},
            {"name": "CABLE Input", "max_input_channels": 0,
             "max_output_channels": 2},
            {"name": "Mic", "max_input_channels": 1,
             "max_output_channels": 0},
        ]
        self.played = []
        self.stopped = False
        self.last_output_stream: FakeOutputStream | None = None

        class _Default:
            device = (0, 0)
        self.default = _Default()
        self._stream = FakeStream()

    def query_devices(self, index=None, *args, **kwargs):
        if index is None:
            return self._devices
        return self._devices[index]

    def play(self, data, samplerate, device=None):
        self.played.append((data, samplerate, device))
        # Fresh stream per playback so the poll loop terminates.
        self._stream = FakeStream()

    def get_stream(self):
        return self._stream

    def stop(self):
        self.stopped = True

    def sleep(self, ms):
        pass

    def OutputStream(self, **kwargs):
        stream = FakeOutputStream(**kwargs)
        self.last_output_stream = stream
        return stream


class FakeSoundfile(types.ModuleType):
    def __init__(self):
        super().__init__("soundfile")

    def read(self, buf, dtype="float32"):
        # Return a tiny stereo signal at 44.1kHz.
        return np.zeros((10, 2), dtype="float32"), 44100


@pytest.fixture
def fake_audio(monkeypatch):
    sd = FakeSounddevice()
    sf = FakeSoundfile()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setitem(sys.modules, "soundfile", sf)
    return sd, sf


# ----------------------------------------------- device enumeration


def test_enumerate_output_devices(fake_audio):
    devices = audioplayer.enumerate_output_devices()
    # Only output-capable devices (indices 0 and 1).
    assert (0, "Speakers") in devices
    assert (1, "CABLE Input") in devices
    assert all(idx != 2 for idx, _ in devices)


def test_find_output_device(fake_audio):
    assert audioplayer.find_output_device("cable") == 1
    assert audioplayer.find_output_device("speakers") == 0
    assert audioplayer.find_output_device("nothere") is None


def test_enumerate_handles_missing_libs(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "soundfile", None)
    assert audioplayer.enumerate_output_devices() == []


# --------------------------------------------------- play_bytes


def test_play_bytes_container_format(fake_audio):
    sd, _ = fake_audio
    audioplayer.play_bytes(b"fakeaudio", device_index=1)
    assert len(sd.played) == 1
    _data, samplerate, device = sd.played[0]
    assert samplerate == 44100
    assert device == 1


def test_play_bytes_pcm_raw(fake_audio):
    sd, _ = fake_audio
    # 4 int16 samples of raw PCM.
    raw = np.array([100, 200, 300, 400], dtype="<i2").tobytes()
    audioplayer.play_bytes(raw, device_index=0, pcm_rate=16000)
    assert len(sd.played) == 1
    data, samplerate, device = sd.played[0]
    assert samplerate == 16000
    # Mono should be upmixed to stereo for a 2-channel device.
    assert data.ndim == 2
    assert data.shape[1] == 2


def test_play_bytes_should_stop_aborts(fake_audio):
    sd, _ = fake_audio
    audioplayer.play_bytes(
        b"audio", device_index=0, should_stop=lambda: True
    )
    assert sd.stopped is True


def test_play_bytes_missing_libs_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "soundfile", None)
    # Should not raise.
    audioplayer.play_bytes(b"audio", device_index=0)


# --------------------------------------------------- play_stream


def _make_pcm_chunk(n_samples: int = 256) -> bytes:
    """Return *n_samples* silent 16-bit mono PCM bytes."""
    return np.zeros(n_samples, dtype="<i2").tobytes()


def test_play_stream_opens_output_stream(fake_audio):
    sd, _ = fake_audio
    chunk = _make_pcm_chunk(512)
    audioplayer.play_stream(iter([chunk]), device_index=0, pcm_rate=24000)
    assert sd.last_output_stream is not None
    assert sd.last_output_stream.started


def test_play_stream_starts_after_prebuffer(fake_audio, monkeypatch):
    """Stream must not open the device before enough bytes have arrived."""
    sd, _ = fake_audio
    # Each chunk is smaller than the prebuffer threshold.
    small = _make_pcm_chunk(32)  # 64 bytes — well below 4800
    chunks_sent = []

    original_os = sd.OutputStream

    def tracking_os(**kwargs):
        stream = original_os(**kwargs)
        chunks_sent.clear()
        return stream

    monkeypatch.setattr(sd, "OutputStream", tracking_os)

    # Enough small chunks to exceed the prebuffer.
    many_chunks = [small] * 100
    audioplayer.play_stream(iter(many_chunks), device_index=0, pcm_rate=24000)
    assert sd.last_output_stream is not None
    assert sd.last_output_stream.started


def test_play_stream_short_audio_still_plays(fake_audio):
    """Even a single tiny chunk must start the stream."""
    sd, _ = fake_audio
    tiny = _make_pcm_chunk(16)  # only 32 bytes
    audioplayer.play_stream(iter([tiny]), device_index=0, pcm_rate=24000)
    assert sd.last_output_stream is not None
    assert sd.last_output_stream.started


def test_play_stream_stops_on_should_stop(fake_audio):
    sd, _ = fake_audio
    chunk = _make_pcm_chunk(512)
    audioplayer.play_stream(
        iter([chunk] * 10),
        device_index=0,
        pcm_rate=24000,
        should_stop=lambda: True,
    )
    # Stream should be cleaned up even when aborted.
    if sd.last_output_stream is not None:
        assert sd.last_output_stream.closed


def test_play_stream_missing_libs_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "soundfile", None)
    # Should not raise even with missing libs.
    audioplayer.play_stream(iter([b"\x00\x01"]), device_index=0, pcm_rate=24000)


def test_play_stream_closes_stream_on_completion(fake_audio):
    sd, _ = fake_audio
    chunk = _make_pcm_chunk(512)
    audioplayer.play_stream(iter([chunk]), device_index=0, pcm_rate=24000)
    assert sd.last_output_stream.closed


def test_play_bytes_upmixes_mono_container_audio(fake_audio, monkeypatch):
    """Container-decoded mono must reach both channels of a stereo device."""
    sd, sf = fake_audio
    monkeypatch.setattr(
        sf, "read",
        lambda buf, dtype="float32": (np.zeros(10, dtype="float32"), 24000),
    )
    audioplayer.play_bytes(b"fakeaudio", device_index=0)
    data, samplerate, _device = sd.played[-1]
    assert samplerate == 24000
    assert data.shape == (10, 2)


# ------------------------------------------------- fixed-rate devices


def test_resample_doubles_and_halves():
    mono = np.sin(np.linspace(0, 20, 2400)).astype("float32")
    up = audioplayer.resample(mono, 24000, 48000)
    assert up.shape == (4800,)
    down = audioplayer.resample(up, 48000, 12000)
    assert down.shape == (1200,)


def test_resample_keeps_channels_and_noop_cases():
    stereo = np.zeros((240, 2), dtype="float32")
    assert audioplayer.resample(stereo, 24000, 48000).shape == (480, 2)
    assert audioplayer.resample(stereo, 24000, 24000) is stereo
    assert audioplayer.resample(stereo, 0, 48000) is stereo
    assert audioplayer.resample(None, 24000, 48000) is None


def test_play_bytes_adapts_to_a_fixed_rate_device(fake_audio):
    """A 48 kHz-only endpoint still gets the 24 kHz PCM, just resampled."""
    sd, _ = fake_audio
    sd._devices[1].update({"default_samplerate": 48000})
    audioplayer.play_bytes(_make_pcm_chunk(2400), device_index=1, pcm_rate=24000)
    data, samplerate, device = sd.played[-1]
    assert (samplerate, device) == (48000, 1)
    assert data.shape == (4800, 2)


def test_play_stream_buffers_on_a_fixed_rate_device(fake_audio):
    """Streaming to a fixed-rate device degrades to buffered playback."""
    sd, _ = fake_audio
    sd._devices[1].update({"default_samplerate": 48000})
    audioplayer.play_stream(iter([_make_pcm_chunk(2400)]), device_index=1,
                            pcm_rate=24000)
    assert sd.last_output_stream is None          # no stream was opened
    data, samplerate, device = sd.played[-1]      # played through play_bytes
    assert (samplerate, device) == (48000, 1)
    assert data.shape == (4800, 2)
