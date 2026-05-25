import pytest

import claude_dingtalk_bridge.images as images


class _FakeResponse:
    def __init__(self, content, content_type):
        self._content = content
        self.headers = {"Content-Type": content_type} if content_type else {}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def close(self):
        pass


def _fake_get(content, content_type):
    def get(url, timeout=None, stream=None):
        return _FakeResponse(content, content_type)

    return get


def test_download_image_writes_file_with_jpeg_extension(monkeypatch):
    monkeypatch.setattr(
        images.requests, "get", _fake_get(b"\xff\xd8jpegbytes", "image/jpeg")
    )
    path = images.download_image("http://example.com/x")
    assert path.suffix == ".jpg"
    assert path.read_bytes() == b"\xff\xd8jpegbytes"


def test_download_image_infers_extension_ignoring_charset(monkeypatch):
    monkeypatch.setattr(
        images.requests, "get", _fake_get(b"gif", "image/gif; charset=binary")
    )
    path = images.download_image("http://example.com/x")
    assert path.suffix == ".gif"


def test_download_image_defaults_to_png_without_content_type(monkeypatch):
    monkeypatch.setattr(images.requests, "get", _fake_get(b"data", None))
    path = images.download_image("http://example.com/x")
    assert path.suffix == ".png"
    assert path.read_bytes() == b"data"


def test_download_image_rejects_oversized_response(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setattr(images, "_IMAGE_DIR", cache)
    monkeypatch.setattr(images, "_MAX_IMAGE_BYTES", 8)
    monkeypatch.setattr(
        images.requests, "get", _fake_get(b"x" * 100, "image/png")
    )
    with pytest.raises(ValueError):
        images.download_image("http://example.com/x")
    # The partial file is unlinked, so the overshoot leaves nothing behind.
    assert list(cache.iterdir()) == []


def test_download_image_refuses_symlinked_cache_dir(monkeypatch, tmp_path):
    link = tmp_path / "linked-cache"
    link.symlink_to(tmp_path)
    monkeypatch.setattr(images, "_IMAGE_DIR", link)
    with pytest.raises(RuntimeError):
        images.download_image("http://example.com/x")


def test_download_image_refuses_symlinked_parent_dir(monkeypatch, tmp_path):
    # A symlinked parent is the bigger attack surface — the leaf is recreated
    # on every install, but a parent like ~/Library/Caches lives across runs.
    other = tmp_path / "elsewhere"
    other.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(other)
    cache = linked_parent / "cache"
    monkeypatch.setattr(images, "_IMAGE_DIR", cache)
    with pytest.raises(RuntimeError):
        images.download_image("http://example.com/x")


def test_download_image_prunes_stale_files_tolerating_errors(monkeypatch, tmp_path):
    import os

    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / "stale.png"
    stale.write_bytes(b"old")
    os.utime(stale, (0, 0))  # epoch mtime — far older than the retention window
    monkeypatch.setattr(images, "_IMAGE_DIR", cache)

    # Make the prune unlink raise so the OSError tolerance path is exercised.
    def boom(self, *args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(images.Path, "unlink", boom)
    monkeypatch.setattr(
        images.requests, "get", _fake_get(b"fresh", "image/png")
    )
    path = images.download_image("http://example.com/x")
    assert path.read_bytes() == b"fresh"
