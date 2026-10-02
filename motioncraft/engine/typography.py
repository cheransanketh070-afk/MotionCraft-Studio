"""Kinetic typography: word-level sprites with soft shadows, chips and CTA buttons."""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

_ASSETS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_CANDIDATES = {
    "sans": ["display.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
             "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"],
    "serif": ["display-serif.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
              "/Library/Fonts/Georgia Bold.ttf", "C:/Windows/Fonts/georgiab.ttf"],
}


@functools.lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    for cand in _CANDIDATES.get(kind, []) + _CANDIDATES["sans"]:
        p = _ASSETS / cand if not os.path.isabs(cand) else Path(cand)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _premult(img: Image.Image) -> np.ndarray:
    a = np.asarray(img, dtype=np.float32) / 255.0
    a[..., :3] *= a[..., 3:4]
    return a


@dataclass
class Word:
    spr: np.ndarray     # premultiplied float32 RGBA
    x: int
    y: int


@dataclass
class Block:
    words: list[Word]
    w: int
    h: int


def _text_sprite(text: str, fnt, color, shadow: bool = True, tracking: float = 0.0) -> Image.Image:
    size = fnt.size
    pad = int(size * 0.35)
    if tracking:
        width = sum(fnt.getlength(c) + tracking * size for c in text)
    else:
        width = fnt.getlength(text)
    asc, desc = fnt.getmetrics()
    w, h = int(width) + pad * 2, asc + desc + pad * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if shadow:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        _draw(sd, (pad, pad + size * 0.05), text, fnt, (0, 0, 0, 120), tracking)
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(size * 0.06)))
    _draw(ImageDraw.Draw(img), (pad, pad), text, fnt, tuple(color) + (255,), tracking)
    return img


def _draw(d, xy, text, fnt, fill, tracking):
    if not tracking:
        d.text(xy, text, font=fnt, fill=fill)
        return
    x, y = xy
    for c in text:
        d.text((x, y), c, font=fnt, fill=fill)
        x += fnt.getlength(c) + tracking * fnt.size


def layout_words(text: str, kind: str, size: int, color, max_w: int, align: str = "center",
                 tracking: float = 0.0, line_gap: float = 1.08) -> Block:
    fnt = font(kind, size)
    words = text.split()
    if not words:
        return Block([], 0, 0)
    space = fnt.getlength(" ") + tracking * size
    wlen = [sum(fnt.getlength(c) + tracking * size for c in w) if tracking else fnt.getlength(w) for w in words]
    lines: list[list[int]] = [[]]
    cur = 0.0
    for i, wl in enumerate(wlen):
        add = wl + (space if lines[-1] else 0)
        if lines[-1] and cur + add > max_w:
            lines.append([])
            cur = 0.0
            add = wl
        lines[-1].append(i)
        cur += add
    asc, desc = fnt.getmetrics()
    lh = int((asc + desc) * line_gap)
    pad = int(size * 0.35)
    out: list[Word] = []
    total_w = 0
    for li, idxs in enumerate(lines):
        lw = sum(wlen[i] for i in idxs) + space * (len(idxs) - 1)
        total_w = max(total_w, lw)
    for li, idxs in enumerate(lines):
        lw = sum(wlen[i] for i in idxs) + space * (len(idxs) - 1)
        x = {"left": 0, "center": (total_w - lw) / 2, "right": total_w - lw}[align]
        for i in idxs:
            spr = _premult(_text_sprite(words[i], fnt, color, True, tracking))
            out.append(Word(spr, int(x) - pad, li * lh - pad))
            x += wlen[i] + space
    return Block(out, int(total_w), lh * len(lines))


def chip(text: str, size: int, fg, bg, kind: str = "sans") -> Block:
    fnt = font(kind, size)
    tr = 0.06
    tw = sum(fnt.getlength(c) + tr * size for c in text)
    px, py = int(size * 0.7), int(size * 0.38)
    asc, desc = fnt.getmetrics()
    w, h = int(tw) + px * 2, asc + desc + py * 2
    pad = int(size * 0.5)
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((pad, pad, pad + w, pad + h), h // 2, fill=tuple(bg) + (140,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size * 0.35)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((pad, pad, pad + w, pad + h), h // 2, fill=tuple(bg) + (255,))
    _draw(d, (pad + px, pad + py - 1), text, fnt, tuple(fg) + (255,), tr)
    return Block([Word(_premult(img), -pad, -pad)], w, h)


def button(text: str, size: int, fg, c1, c2, kind: str = "sans") -> Block:
    fnt = font(kind, size)
    tr = 0.08
    tw = sum(fnt.getlength(c) + tr * size for c in text)
    px, py = int(size * 1.35), int(size * 0.62)
    asc, desc = fnt.getmetrics()
    w, h = int(tw) + px * 2 + int(size * 0.9), asc + desc + py * 2
    pad = int(size * 0.7)
    W, H = w + pad * 2, h + pad * 2
    grad = np.zeros((H, W, 4), np.uint8)
    t = np.linspace(0, 1, W, dtype=np.float32)[None, :, None]
    grad[..., :3] = (np.array(c1, np.float32) * (1 - t) + np.array(c2, np.float32) * t).astype(np.uint8)
    grad[..., 3] = 255
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, pad + w, pad + h), h // 2, fill=255)
    img = Image.fromarray(grad, "RGBA")
    img.putalpha(mask)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((pad, pad + size * 0.15, pad + w, pad + h + size * 0.15), h // 2,
                                           fill=tuple(c1) + (150,))
    base = glow.filter(ImageFilter.GaussianBlur(size * 0.45))
    base.alpha_composite(img)
    d = ImageDraw.Draw(base)
    _draw(d, (pad + px, pad + py - 1), text, fnt, tuple(fg) + (255,), tr)
    # chevron arrow
    ax, ay, s = pad + px + tw + size * 0.55, pad + h / 2, size * 0.26
    d.line([(ax - s, ay - s), (ax, ay), (ax - s, ay + s)], fill=tuple(fg) + (255,), width=max(2, int(size * 0.13)),
           joint="curve")
    return Block([Word(_premult(base), -pad, -pad)], w, h)


def blit(dst: np.ndarray, spr: np.ndarray, x: int, y: int, a: float = 1.0) -> None:
    """Composite premultiplied sprite onto float32 RGB frame in place."""
    if a <= 0.003:
        return
    H, W = dst.shape[:2]
    h, w = spr.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    s = spr[y0 - y: y1 - y, x0 - x: x1 - x]
    d = dst[y0:y1, x0:x1]
    d *= 1.0 - s[..., 3:4] * a
    d += s[..., :3] * a


def smooth(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * x * (x * (6 * x - 15) + 10)


def resize_sprite(spr: np.ndarray, s: float) -> np.ndarray:
    if abs(s - 1) < 0.002:
        return spr
    h, w = spr.shape[:2]
    return cv2.resize(spr, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_LINEAR)
