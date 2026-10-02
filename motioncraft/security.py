"""Security primitives: auth, rate limiting, upload validation, input sanitising."""
from __future__ import annotations

import hmac
import io
import re
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # we enforce our own limit before decoding

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def clean_text(value: object, max_len: int) -> str:
    """Strip control / bidi-override chars, collapse whitespace, bound length."""
    if value is None:
        return ""
    s = _CTRL.sub("", str(value)).replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:max_len]


def key_ok(candidate: str, valid: tuple[str, ...]) -> bool:
    """Constant-time comparison against every configured key."""
    if not candidate:
        return False
    ok = False
    for k in valid:
        ok |= hmac.compare_digest(candidate.encode(), k.encode())
    return ok


def bearer(headers) -> str:
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("X-API-Key", "").strip()


class RateLimiter:
    """Token bucket per (bucket, client)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_gc = time.monotonic()

    def allow(self, bucket: str, client: str, per_min: int) -> bool:
        now = time.monotonic()
        rate = per_min / 60.0
        with self._lock:
            tokens, ts = self._state.get((bucket, client), (float(per_min), now))
            tokens = min(float(per_min), tokens + (now - ts) * rate)
            ok = tokens >= 1.0
            self._state[(bucket, client)] = (tokens - 1.0 if ok else tokens, now)
            if now - self._last_gc > 300:
                self._state = {k: v for k, v in self._state.items() if now - v[1] < 600}
                self._last_gc = now
            return ok


def client_ip(handler, hops: int) -> str:
    peer = handler.client_address[0]
    if hops <= 0:
        return peer
    xff = [p.strip() for p in handler.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    return xff[-hops] if len(xff) >= hops else peer


class UploadError(ValueError):
    pass


@dataclass
class CleanImage:
    png: bytes
    width: int
    height: int


def validate_image(raw: bytes, max_bytes: int, max_pixels: int) -> CleanImage:
    """Verify magic bytes, bound pixels, then fully re-encode.

    Re-encoding strips EXIF/ICC payloads and any polyglot/trailing data.
    """
    if len(raw) > max_bytes:
        raise UploadError("Image is too large.")
    head = raw[:12]
    ok = (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )
    if not ok:
        raise UploadError("Only PNG, JPEG or WebP images are accepted.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ("PNG", "JPEG", "WEBP"):
                raise UploadError("Unsupported image format.")
            w, h = im.size
            if w < 32 or h < 32:
                raise UploadError("Image is too small (min 32x32).")
            if w * h > max_pixels:
                raise UploadError("Image resolution is too high.")
            im.load()
            im = ImageOps.exif_transpose(im)
            has_alpha = "A" in im.getbands() or im.mode == "P"
            im = im.convert("RGBA" if has_alpha else "RGB")
            longest = max(im.size)
            if longest > 2048:
                f = 2048 / longest
                im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "PNG")
            return CleanImage(out.getvalue(), im.width, im.height)
    except UploadError:
        raise
    except Exception as exc:  # corrupt / truncated
        raise UploadError("The image file could not be read.") from exc


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
