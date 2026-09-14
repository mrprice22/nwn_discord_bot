"""Rehosting Discord attachments, so a screenshot outlives its signed link.

Why this module exists, stated plainly because it is easy to get wrong: a
``cdn.discordapp.com`` attachment URL is *signed* and expires. Measured on this
guild the window is under 24 hours, and every screenshot pasted into
``roadmap.yaml`` before the bot existed is already a 404 — seven of them, across
three items, all dead. Storing the Discord link is therefore not a cheap
alternative to rehosting; it is a link that reviews fine today and rots by
tomorrow, which is worse than having none.

So the bytes are copied somewhere permanent, and only that URL is ever written
into an idea. Three steps, each separately testable:

1. :func:`transcode` — bytes in, smaller bytes out. Pure, no network, no disk.
2. :class:`ImageStore` — where the bytes land. :class:`LocalStore` is the
   filesystem one used by tests; the live one is object storage.
3. :func:`key_for` — the name they land under, derived from the *content*, so
   the same image uploaded twice occupies one object and a retry after a failed
   run is free rather than duplicating.

Nothing here decides *whether* to rehost. That is the planner's business.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

#: Longest edge, in pixels, after downscaling. A bug report is read at a glance
#: on a roadmap card; a 4K screenshot costs ten times the bytes to say the same
#: thing. Measured on a real report: 440KB PNG -> 45KB WebP, and that one was
#: already under this limit — the saving is the re-encode, not the resize.
MAX_EDGE_PX = 1600

#: WebP quality. 80 is the usual "no visible loss on a screenshot" figure; 72
#: buys another ~20% and starts to soften small text, which bug reports are
#: mostly made of.
WEBP_QUALITY = 80

#: Refuse anything larger than this *before* decoding it. A decompression bomb
#: is a 10KB file that becomes 40GB of pixels, and this runs against whatever
#: a stranger chose to upload to a public forum.
MAX_SOURCE_BYTES = 25 * 1024 * 1024

#: Same hazard, the other axis: total pixels after decode.
MAX_SOURCE_PIXELS = 80_000_000

#: What a rehosted image becomes, whatever it arrived as.
TARGET_CONTENT_TYPE = "image/webp"
TARGET_EXTENSION = ".webp"

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


class TranscodeError(Exception):
    """The bytes could not be turned into a storable image."""


class ImageStore(Protocol):
    """Somewhere bytes can be put and read back over HTTP afterwards."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store ``data`` under ``key``; return the URL it is readable at."""

    def exists(self, key: str) -> bool:
        """Whether ``key`` is already stored. Lets a retry skip the upload."""

    def url_for(self, key: str) -> str:
        """The public URL for ``key``, without storing anything."""


def key_for(data: bytes, *, prefix: str = "discord") -> str:
    """A content-addressed object name.

    The hash is of the *stored* bytes, so the same screenshot posted in two
    threads is one object, and re-running a partially-failed batch re-uploads
    nothing. It also means the URL cannot leak a filename someone chose, which
    on a public forum is not always something to republish.
    """
    digest = hashlib.sha256(data).hexdigest()[:32]
    return f"{prefix}/{digest[:2]}/{digest}{TARGET_EXTENSION}"


def safe_filename(name: str) -> str:
    """A filename reduced to something harmless. Only used for logging."""
    cleaned = _SAFE.sub("-", (name or "").strip()).strip("-.")
    return cleaned[:80] or "file"


def looks_like_image(content_type: str, filename: str = "") -> bool:
    """Whether this is worth trying to transcode at all.

    Trusts the declared content type first and the extension only as a
    fallback, because Discord supplies the former and it is the more reliable.
    """
    if content_type:
        return content_type.split(";")[0].strip().lower().startswith("image/")
    guessed, _ = mimetypes.guess_type(filename or "")
    return bool(guessed and guessed.startswith("image/"))


def transcode(data: bytes, *, max_edge: int = MAX_EDGE_PX,
              quality: int = WEBP_QUALITY) -> bytes:
    """Downscale and re-encode to WebP. Pure: no network, no disk, no clock.

    Raises :class:`TranscodeError` rather than returning the original bytes on
    failure. Silently falling back would mean quietly storing a 2MB PNG under a
    ``.webp`` name, and the caller could not tell the difference.
    """
    if not data:
        raise TranscodeError("no bytes")
    if len(data) > MAX_SOURCE_BYTES:
        raise TranscodeError(
            f"source is {len(data)} bytes, over the {MAX_SOURCE_BYTES} limit")
    try:
        from PIL import Image, ImageFile
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise TranscodeError("Pillow is not installed") from exc

    import io

    # A truncated upload should raise here, not produce a half-grey image that
    # looks like a successful rehost.
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_SOURCE_PIXELS:
                raise TranscodeError(
                    f"{width}x{height} is over the {MAX_SOURCE_PIXELS} pixel limit")
            image.load()
            # Flatten first: WebP keeps alpha, but a screenshot pasted from a
            # transparent-background tool otherwise renders on whatever colour
            # the roadmap card happens to be, which has been white and black in
            # the same week.
            if image.mode in ("RGBA", "LA", "P"):
                converted = image.convert("RGBA")
                background = Image.new("RGB", converted.size, (255, 255, 255))
                background.paste(converted, mask=converted.split()[-1])
                image = background
            else:
                image = image.convert("RGB")
            if max(image.size) > max_edge:
                image.thumbnail((max_edge, max_edge), Image.LANCZOS)
            out = io.BytesIO()
            image.save(out, "WEBP", quality=quality, method=6)
            return out.getvalue()
    except TranscodeError:
        raise
    except Exception as exc:
        raise TranscodeError(f"could not read image: {exc}") from exc


@dataclass
class LocalStore:
    """An :class:`ImageStore` backed by a directory.

    What the tests run against, and a usable fallback when object storage is
    not configured: the bytes are at least kept, and the URL is wrong rather
    than the image being lost.
    """

    root: Path
    base_url: str = ""

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.base_url = self.base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        # Refuse to climb out of root even if a key is ever built from input.
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError(f"key escapes the store root: {key!r}")
        return target

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/{key}" if self.base_url else key

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.url_for(key)


def store_image(data: bytes, store: ImageStore, *, prefix: str = "discord",
                max_edge: int = MAX_EDGE_PX,
                quality: int = WEBP_QUALITY) -> tuple[str, int]:
    """Transcode then store, skipping the upload when the object already exists.

    Returns ``(url, stored_bytes)``. The skip is what makes a resumed run cheap
    and an accidental double-run harmless.
    """
    encoded = transcode(data, max_edge=max_edge, quality=quality)
    key = key_for(encoded, prefix=prefix)
    if store.exists(key):
        return store.url_for(key), len(encoded)
    return store.put(key, encoded, TARGET_CONTENT_TYPE), len(encoded)


__all__ = [
    "ImageStore",
    "LocalStore",
    "MAX_EDGE_PX",
    "MAX_SOURCE_BYTES",
    "MAX_SOURCE_PIXELS",
    "TARGET_CONTENT_TYPE",
    "TARGET_EXTENSION",
    "TranscodeError",
    "WEBP_QUALITY",
    "key_for",
    "looks_like_image",
    "safe_filename",
    "store_image",
    "transcode",
]
