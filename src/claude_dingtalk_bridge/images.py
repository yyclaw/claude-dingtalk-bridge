"""Download DingTalk image attachments to local cache files."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import requests

from claude_dingtalk_bridge.config import CACHE_DIR

# Extension by Content-Type — DingTalk serves images over a generic URL, so the
# response header is the only reliable hint. Anything unrecognized falls to PNG.
_EXT_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

# Cache attachments under the user's home, not the shared system temp dir: a
# stable path in /tmp invites a symlink pre-creation attack and leaves the
# files readable by every local user. The directory is created owner-only.
_IMAGE_DIR = CACHE_DIR

# Cap a single download so a malicious or broken sender cannot exhaust memory
# or disk — ample for a phone photo, far below anything alarming.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Cached files are pruned once older than this — nothing else reclaims them
# now that they live outside the system temp dir.
_MAX_IMAGE_AGE_SECONDS = 72 * 60 * 60

# Throttle the prune scan: a burst of images would otherwise re-walk the cache
# dir and stat every entry on each download for no useful benefit.
_PRUNE_INTERVAL_SECONDS = 60 * 60
_last_prune = 0.0
# Image downloads run in worker threads (asyncio.to_thread), so the throttle
# timestamp and the prune scan need a lock — otherwise two threads can both
# pass the throttle check and concurrently iterate/unlink the cache dir.
_prune_lock = threading.Lock()


def _ensure_image_dir() -> Path:
    """Create the cache dir owner-only, refusing a symlinked path.

    ``mkdir(mode=0o700)`` only applies on creation; refusing a symlink on the
    leaf *and* its immediate parent closes the pre-creation attack a fixed
    path would otherwise allow. Walking further up would trip over macOS
    firmlinks (``/Users``, ``/var``), which are intentional system symlinks.
    """
    for ancestor in (_IMAGE_DIR, _IMAGE_DIR.parent):
        if ancestor.is_symlink():
            raise RuntimeError(
                f"Refusing to use {_IMAGE_DIR}: {ancestor} is a symlink"
            )
    _IMAGE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _IMAGE_DIR


def _prune_old_images() -> None:
    """Best-effort delete of cached images older than the retention window."""
    global _last_prune
    with _prune_lock:
        now = time.time()
        if now - _last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        _last_prune = now
        cutoff = now - _MAX_IMAGE_AGE_SECONDS
        for entry in _IMAGE_DIR.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                pass  # a file vanishing mid-prune is fine — nothing to do


def download_image(download_url: str) -> Path:
    """Fetch an image into the bridge's cache directory and return its path.

    The extension is inferred from the response Content-Type, defaulting to
    `.png`. The body is streamed and capped at `_MAX_IMAGE_BYTES`; a download
    that overshoots is deleted and raises. Files older than the retention
    window are pruned on each call, since the cache dir is no longer the
    OS-reclaimed temp dir.
    """
    _ensure_image_dir()
    _prune_old_images()
    response = requests.get(download_url, timeout=30, stream=True)
    try:
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").split(";")[0]
        ext = _EXT_BY_CONTENT_TYPE.get(content_type.strip().lower(), ".png")
        path = _IMAGE_DIR / f"{time.time_ns()}{ext}"
        size = 0
        try:
            with path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > _MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"Image exceeds the {_MAX_IMAGE_BYTES}-byte limit"
                        )
                    f.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)  # never leave a partial file behind
            raise
    finally:
        response.close()
    return path
