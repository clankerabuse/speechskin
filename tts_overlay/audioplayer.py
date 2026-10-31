"""Per-device audio playback via PortAudio (``sounddevice``).

Used by network/file based TTS engines (e.g. ElevenLabs) that hand us raw
audio *bytes* instead of speaking to a device themselves. We decode the audio
and play it on a chosen PortAudio output device.

Device indices here are PortAudio indices (``sounddevice.query_devices``).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator

from . import logutil

log = logutil.get(__name__)

# Minimum bytes to buffer before opening the output stream and starting
# playback.  Incoming chunks are raw 16-bit *mono* PCM at 24 000 Hz
# (48 000 bytes/sec), so 2400 bytes ≈ 50 ms of audio.
# Smaller = lower latency to first sound; larger = safer against underruns.
_STREAM_PREBUFFER_BYTES = 1200  # ~25 ms @ 24 000 Hz × 1 ch × 2 bytes


def _import_audio_libs():
    """Import sounddevice + soundfile lazily with a helpful error message."""
    try:
        import sounddevice as sd
        import soundfile as sf
        return sd, sf
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Audio playback requires 'sounddevice' and 'soundfile'. "
            "Install them with:  pip install sounddevice soundfile"
        ) from exc


def enumerate_output_devices() -> list[tuple[int, str]]:
    """Return ``(index, name)`` for each PortAudio output-capable device."""
    try:
        sd, _ = _import_audio_libs()
    except RuntimeError as exc:
        log.error("%s", exc)
        return []
    try:
        devices = sd.query_devices()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to query audio devices: %s", exc)
        return []
    out: list[tuple[int, str]] = []
    for idx, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) > 0:
            out.append((idx, dev.get("name", f"device {idx}")))
    return out


def find_output_device(keyword: str) -> int | None:
    """Return the first output device index whose name contains *keyword*."""
    kw = keyword.lower()
    for idx, name in enumerate_output_devices():
        if kw in name.lower():
            return idx
    return None


def _device_max_output_channels(sd, device_index: int | None) -> int | None:
    """Return max_output_channels for *device_index*, or None on any error."""
    try:
        info = sd.query_devices(device_index if device_index is not None
                                else sd.default.device[1])
        return int(info.get("max_output_channels", 0)) or None
    except Exception:  # noqa: BLE001
        return None


def _device_rate(sd, device_index: int | None) -> int | None:
    """Return the sample rate *device_index* accepts, or None when unknown.

    Some endpoints are fixed-rate: PipeWire nodes showing up through the JACK
    host API, and several virtual-cable drivers, reject any stream whose rate
    differs from the node's own (PortAudio: ``Invalid sample rate``). Knowing
    the device's rate lets us adapt instead of failing.
    """
    try:
        info = sd.query_devices(device_index if device_index is not None
                                else sd.default.device[1])
        return int(info.get("default_samplerate") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def resample(data, src_rate: int, dst_rate: int):
    """Linearly resample *data* from *src_rate* to *dst_rate*.

    Works on a mono 1-D array or a ``(frames, channels)`` array and preserves
    the shape. Linear interpolation is plenty for speech and keeps this
    dependency-free (numpy only). Returns *data* unchanged when no work is
    needed.
    """
    if (data is None or not src_rate or not dst_rate
            or int(src_rate) == int(dst_rate) or len(data) == 0):
        return data
    import numpy as np

    n_src = len(data)
    n_dst = max(1, int(round(n_src * float(dst_rate) / float(src_rate))))
    x_src = np.arange(n_src, dtype="float64")
    x_dst = np.linspace(0.0, float(n_src - 1), n_dst, dtype="float64")

    mono = getattr(data, "ndim", 1) == 1
    arr = np.asarray(data, dtype="float32").reshape(n_src, -1)
    out = np.empty((n_dst, arr.shape[1]), dtype="float32")
    for ch in range(arr.shape[1]):
        out[:, ch] = np.interp(x_dst, x_src, arr[:, ch].astype("float64"))
    return out[:, 0] if mono else out


def play_bytes(audio: bytes, device_index: int | None | list[int | None],
               should_stop=None, pcm_rate: int | None = None) -> None:
    """Decode *audio* and play it on *device_index* (can be a single device or a list).

    If *pcm_rate* is given, *audio* is treated as raw headerless 16-bit signed
    little-endian mono PCM at that sample rate (this is what ElevenLabs returns
    for ``pcm_*`` output formats, which has no container header for soundfile
    to sniff). Otherwise *audio* is decoded as a self-describing container
    (WAV/FLAC/OGG/MP3/...).

    ``device_index`` of ``None`` uses the system default output device.
    ``should_stop`` is an optional zero-arg callable; if it returns truthy
    during playback, playback is aborted early. Never raises.
    """
    try:
        sd, sf = _import_audio_libs()
    except RuntimeError as exc:
        log.error("%s", exc)
        return

    # Decode container once if needed
    decoded_data = None
    samplerate = pcm_rate
    if pcm_rate is None:
        try:
            decoded_data, samplerate = sf.read(io.BytesIO(audio), dtype="float32")
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to decode audio for playback: %s", exc)
            return

    devices = device_index if isinstance(device_index, list) else [device_index]
    unique_devices = []
    seen = set()
    for d in devices:
        if d not in seen:
            seen.add(d)
            unique_devices.append(d)

    if not unique_devices:
        return

    # Helper to get play data for a specific device
    def _get_data_for_device(dev):
        if pcm_rate is not None:
            import numpy as np
            ints = np.frombuffer(audio, dtype="<i2")
            d_arr = ints.astype("float32") / 32768.0
        else:
            d_arr = decoded_data
        if d_arr is None:
            return None
        # A mono buffer played on a stereo device reaches only the left channel,
        # so duplicate it — for container-decoded audio as well as raw PCM.
        if getattr(d_arr, "ndim", 1) == 1:
            import numpy as np
            max_ch = _device_max_output_channels(sd, dev)
            if max_ch is None or max_ch >= 2:
                d_arr = np.column_stack([d_arr, d_arr])     # duplicate to stereo
        return d_arr

    # Fixed-rate endpoints (JACK nodes, some virtual cables) only accept their
    # own rate, so adapt each device's copy instead of letting playback fail.
    def _adapt_for_device(dev, data):
        out_rate = _device_rate(sd, dev)
        if data is None or not samplerate or not out_rate or out_rate == samplerate:
            return data, samplerate or 0
        return resample(data, samplerate, out_rate), out_rate

    # Play on all devices concurrently
    threads = []
    def _play_worker(dev, dev_data, dev_rate):
        try:
            sd.play(dev_data, dev_rate, device=dev)
            
            # Sleep for the exact duration of the audio to poll safely without blocking.
            # dev_data is a numpy array; shape[0] is the number of frames.
            if dev_data is not None and dev_rate:
                duration = len(dev_data) / dev_rate
                import time
                start_time = time.time()
                while time.time() - start_time < duration:
                    if should_stop is not None and should_stop():
                        sd.stop()
                        break
                    time.sleep(0.02)
        except Exception as exc:  # noqa: BLE001
            log.error("Audio playback error on device %s: %s", dev, exc)

    for dev in unique_devices:
        dev_data, dev_rate = _adapt_for_device(dev, _get_data_for_device(dev))
        t = threading.Thread(target=_play_worker, args=(dev, dev_data, dev_rate),
                             daemon=True)
        t.start()
        threads.append(t)

    # Poll and wait for all playbacks to finish
    try:
        while any(t.is_alive() for t in threads):
            if should_stop is not None and should_stop():
                sd.stop()
                break
            sd.sleep(50)
    except Exception as exc:  # noqa: BLE001
        log.error("Error during concurrent playback wait: %s", exc)


def play_stream(
    chunks: Iterator[bytes],
    device_index: int | None | list[int | None],
    pcm_rate: int,
    should_stop=None,
) -> None:
    """Stream raw PCM chunks to *device_index* (or list of devices) with minimal latency.

    Collects incoming *chunks* (raw 16-bit signed little-endian mono PCM at
    *pcm_rate* Hz, as returned by the ElevenLabs streaming endpoint) into a
    ring-buffer that feeds one or more sounddevice OutputStreams concurrently.
    Playback begins as soon as *_STREAM_PREBUFFER_BYTES* have arrived so the
    user hears audio before the full response has even been downloaded.

    This is the low-latency alternative to :func:`play_bytes`: instead of
    waiting for the entire audio response, we start playing within ~100 ms of
    the first bytes arriving from the API.

    ``should_stop`` is an optional zero-arg callable; returning truthy aborts
    both the download loop and playback immediately.  Never raises.
    """
    try:
        import numpy as np
        sd, _ = _import_audio_libs()
    except Exception as exc:  # noqa: BLE001
        log.error("play_stream: audio libs unavailable: %s", exc)
        return

    devices = device_index if isinstance(device_index, list) else [device_index]
    unique_devices = []
    seen = set()
    for d in devices:
        if d not in seen:
            seen.add(d)
            unique_devices.append(d)

    if not unique_devices:
        return

    # Determine channel count and setup info for each device
    streams_info = []
    for dev in unique_devices:
        max_ch = _device_max_output_channels(sd, dev)
        n_channels = 1 if (max_ch is not None and max_ch < 2) else 2
        streams_info.append({
            "device": dev, "channels": n_channels, "rate": _device_rate(sd, dev),
        })

    # A fixed-rate endpoint (JACK node, some virtual cables) cannot accept the
    # engine's PCM rate at all. Buffering the response and letting play_bytes()
    # adapt it keeps the audio correct at the cost of some latency.
    if any((info["rate"] or pcm_rate) != pcm_rate for info in streams_info):
        log.debug(
            "play_stream: fixed-rate output device(s) %s cannot stream %d Hz; "
            "buffering instead", [i["device"] for i in streams_info
                                  if (i["rate"] or pcm_rate) != pcm_rate], pcm_rate,
        )
        buffered = bytearray()
        for chunk in chunks:
            if should_stop is not None and should_stop():
                return
            if chunk:
                buffered.extend(chunk)
        if buffered:
            play_bytes(bytes(buffered), device_index, should_stop=should_stop,
                       pcm_rate=pcm_rate)
        return

    # Shared ring-buffer and read cursors for each stream
    _buf_lock = threading.Lock()
    _buf = bytearray()
    _buf_pos = {i: 0 for i in range(len(unique_devices))}

    # Make callback for each stream
    def make_callback(stream_idx: int, n_channels: int):
        def _callback(outdata, frames, _time, status):
            needed = frames * 2  # 16-bit mono PCM is 2 bytes per frame
            with _buf_lock:
                pos = _buf_pos[stream_idx]
                chunk = bytes(_buf[pos:pos + needed])
                _buf_pos[stream_idx] = pos + len(chunk)
                
                # Compact the buffer occasionally so it doesn't grow unbounded.
                # Only delete the part that ALL streams have consumed.
                min_consumed = min(_buf_pos.values())
                if min_consumed > 1_048_576:  # 1 MB consumed
                    del _buf[:min_consumed]
                    for k in _buf_pos:
                        _buf_pos[k] -= min_consumed
            if len(chunk) < needed:
                chunk = chunk + b"\x00" * (needed - len(chunk))
            ints = np.frombuffer(chunk, dtype="<i2").astype("float32") / 32768.0
            if n_channels == 2:
                outdata[:, 0] = ints
                outdata[:, 1] = ints
            else:
                outdata[:, 0] = ints
        return _callback

    # Open output streams for all targeted devices
    streams = []
    try:
        for idx, info in enumerate(streams_info):
            stream = sd.OutputStream(
                samplerate=pcm_rate,
                channels=info["channels"],
                dtype="float32",
                device=info["device"],
                callback=make_callback(idx, info["channels"]),
                latency="low",
            )
            streams.append(stream)
    except Exception as exc:  # noqa: BLE001
        log.error("play_stream: could not open output stream: %s", exc)
        for s in streams:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        return

    stream_started = False

    try:
        prebuf_bytes = 0
        for chunk in chunks:
            if should_stop is not None and should_stop():
                break
            if not chunk:
                continue

            with _buf_lock:
                _buf.extend(chunk)

            if not stream_started:
                prebuf_bytes += len(chunk)
                if prebuf_bytes >= _STREAM_PREBUFFER_BYTES:
                    for s in streams:
                        s.start()
                    stream_started = True

        # Start streams even if prebuffer threshold was never reached (short utterances).
        if not stream_started:
            for s in streams:
                s.start()
            stream_started = True

        # Drain: keep the thread alive while PortAudio plays back buffered
        # audio.  We poll until the ring-buffer has been fully consumed by all callbacks.
        _DRAIN_POLLS = 500
        for _ in range(_DRAIN_POLLS):
            if should_stop is not None and should_stop():
                break
            with _buf_lock:
                remaining = max(len(_buf) - pos for pos in _buf_pos.values())
            if remaining <= 0:
                break
            sd.sleep(20)

        # Small tail to let the callbacks flush their last frame.
        sd.sleep(40)

    except Exception as exc:  # noqa: BLE001
        log.error("play_stream: playback error: %s", exc)
    finally:
        for s in streams:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass

