"""Tiny secrets loader.

Reads secrets (like the ElevenLabs API key) from a local, git-ignored file
``secrets.env`` next to the project, falling back to real environment
variables. The file uses a simple ``KEY=value`` format, one per line:

    ELEVENLABS_API_KEY=sk_xxxxxxxxxxxxxxxxxxxxxxxx

Lines that are blank or start with ``#`` are ignored. Values may optionally be
wrapped in single or double quotes.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import logutil

log = logutil.get(__name__)

# secrets.env lives at the repo root (parent of the tts_overlay package).
SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.env"

_cache: dict[str, str] | None = None


def _load_file() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    data: dict[str, str] = {}
    try:
        if SECRETS_PATH.is_file():
            for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    data[key] = value
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read secrets file %s: %s", SECRETS_PATH, exc)
    _cache = data
    return data


def get(name: str, default: str | None = None) -> str | None:
    """Return secret *name* from the environment, then secrets.env, else default."""
    env = os.environ.get(name)
    if env:
        return env
    return _load_file().get(name, default)


def set(name: str, value: str) -> None:
    """Write or update a secret in secrets.env and update the cache."""
    global _cache
    cache = _load_file()
    cache[name] = value

    lines: list[str] = []
    updated = False
    try:
        if SECRETS_PATH.is_file():
            for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, _ = stripped.partition("=")
                    key = key.strip()
                    if key == name:
                        lines.append(f"{name}={value}")
                        updated = True
                        continue
                lines.append(line)
        if not updated:
            lines.append(f"{name}={value}")

        SECRETS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # secrets.env holds API keys: keep it owner-only whatever the umask was
        os.chmod(SECRETS_PATH, 0o600)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not write secrets file %s: %s", SECRETS_PATH, exc)
