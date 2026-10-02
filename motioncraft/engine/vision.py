"""Vision layer — local image understanding (no models downloaded, no network).

* subject isolation: alpha channel -> uniform-background keying -> GrabCut -> rounded "photo card" fallback
* palette extraction via k-means
* blurred cover plate used as an ambient background
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image


@dataclass
class Subject:
    rgba: np.ndarray          # uint8 HxWx4 (straight alpha), cropped to bbox
    plate: np.ndarray         # uint8 HxWx3 original image (for ambient background)
    accent: tuple[int, int, int]
    accent2: tuple[int, int, int]
    luminance: float
    method: str               # alpha | keyed | grabcut | card


def load_png(png: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(png))
    im.load()
    return np.array(im.convert("RGBA"))


def _largest_components(mask: np.ndarray, keep_ratio: float = 0.08) -> np.ndarray:
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return mask
    areas = stats[1:, cv2.CC_STAT_AREA]
    big = areas.max()
    out = np.zeros_like(mask)
    for i, a in enumerate(areas, start=1):
        if a >= big * keep_ratio:
            out[lab == i] = 255
    return out


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    flood = np.pad(mask, 1)
    ff = flood.copy()
    cv2.floodFill(ff, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 255)
    holes = cv2.bitwise_not(ff)[1:-1, 1:-1]
    return cv2.bitwise_or(mask, holes)


def _key_background(rgb: np.ndarray) -> np.ndarray | None:
    h, w = rgb.shape[:2]
    b = max(3, int(min(h, w) * 0.04))
    border = np.concatenate([rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                             rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3)])
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    blab = cv2.cvtColor(border.reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)
    med = np.median(blab, axis=0)
    bd = np.linalg.norm(blab - med, axis=1)
    if np.percentile(bd, 92) > 14:       # background is not uniform
        return None
    d = np.linalg.norm(lab - med, axis=2)
    thr = max(16.0, float(np.percentile(bd, 98)) * 1.7 + 6)
    return ((d > thr).astype(np.uint8)) * 255


def _grabcut(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    f = 320 / max(h, w)
    small = cv2.resize(rgb, (max(32, int(w * f)), max(32, int(h * f))), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    mask = np.zeros((sh, sw), np.uint8)
    rect = (int(sw * 0.05), int(sh * 0.05), int(sw * 0.90), int(sh * 0.90))
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), mask, rect, bgd, fgd, 4, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return np.zeros((h, w), np.uint8)
    m = ((mask == 1) | (mask == 3)).astype(np.uint8) * 255
    return cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)


def _refine(mask: np.ndarray) -> np.ndarray:
    k = max(3, int(min(mask.shape) * 0.012)) | 1
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ker)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, ker)
    m = _largest_components(m)
    m = _fill_holes(m)
    soft = cv2.GaussianBlur(m, (0, 0), max(1.0, k / 3.0)).astype(np.float32) / 255.0
    soft = np.clip((soft - 0.35) / 0.4, 0, 1)         # tighten halo
    return (soft * 255).astype(np.uint8)


def _card_alpha(h: int, w: int) -> np.ndarray:
    r = int(min(h, w) * 0.06)
    a = np.zeros((h, w), np.uint8)
    cv2.rectangle(a, (r, 0), (w - r - 1, h - 1), 255, -1)
    cv2.rectangle(a, (0, r), (w - 1, h - r - 1), 255, -1)
    for cx, cy in ((r, r), (w - r - 1, r), (r, h - r - 1), (w - r - 1, h - r - 1)):
        cv2.circle(a, (cx, cy), r, 255, -1, cv2.LINE_AA)
    return a


def _palette(rgba: np.ndarray, fallback: tuple[int, int, int]):
    px = rgba[rgba[..., 3] > 200][:, :3]
    if len(px) < 50:
        return fallback, fallback
    if len(px) > 4000:
        px = px[np.random.default_rng(1).choice(len(px), 4000, replace=False)]
    data = px.astype(np.float32)
    k = 4
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 1.0)
    _, labels, centers = cv2.kmeans(data, k, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    hsv = cv2.cvtColor(centers.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2HSV).reshape(-1, 3).astype(np.float32)
    sat, val = hsv[:, 1] / 255, hsv[:, 2] / 255
    score = counts / counts.sum() * (0.25 + sat) * (0.4 + val)
    order = np.argsort(-score)
    pick = lambda i: tuple(int(c) for c in centers[i])
    acc, acc2 = pick(order[0]), pick(order[1] if k > 1 else order[0])
    if hsv[order[0], 1] < 60:       # near-grey subject: keep the style accent
        acc = fallback
    return acc, acc2


def analyze(png: bytes, fallback_accent: tuple[int, int, int]) -> Subject:
    arr = load_png(png)
    rgb = np.ascontiguousarray(arr[..., :3])
    alpha_in = arr[..., 3]
    h, w = rgb.shape[:2]
    method, mask = "card", None

    transparent = float((alpha_in < 250).mean())
    if 0.02 < transparent < 0.9:
        mask, method = alpha_in, "alpha"
    else:
        keyed = _key_background(rgb)
        if keyed is not None:
            cov = float((keyed > 0).mean())
            if 0.04 < cov < 0.9:
                mask, method = keyed, "keyed"
        if mask is None:
            gc = _grabcut(rgb)
            cov = float((gc > 0).mean())
            if 0.06 < cov < 0.88:
                mask, method = gc, "grabcut"

    if mask is not None and method != "alpha":
        mask = _refine(mask)
        cov = float((mask > 40).mean())
        if cov < 0.04 or cov > 0.92:
            mask, method = None, "card"
    if mask is None:
        mask, method = _card_alpha(h, w), "card"

    ys, xs = np.where(mask > 16)
    if len(xs) == 0:
        mask, method = _card_alpha(h, w), "card"
        ys, xs = np.where(mask > 16)
    pad = int(0.015 * max(h, w))
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + 1 + pad)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + 1 + pad)
    rgba = np.dstack([rgb, mask])[y0:y1, x0:x1].copy()
    acc, acc2 = _palette(rgba, fallback_accent)
    lum = float(np.mean(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))) / 255
    return Subject(rgba=rgba, plate=rgb, accent=acc, accent2=acc2, luminance=lum, method=method)


def cover_plate(img: np.ndarray, w: int, h: int, blur: float) -> np.ndarray:
    """Cover-fit then heavily blur: an ambient backdrop derived from the image."""
    ih, iw = img.shape[:2]
    s = max(w / iw, h / ih)
    small = cv2.resize(img, (max(2, int(iw * s * 0.25)), max(2, int(ih * s * 0.25))), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    x0, y0 = (sw - w // 4) // 2, (sh - h // 4) // 2
    crop = small[max(0, y0): max(0, y0) + h // 4, max(0, x0): max(0, x0) + w // 4]
    crop = cv2.resize(crop, (w // 4, h // 4), interpolation=cv2.INTER_AREA)
    crop = cv2.GaussianBlur(crop, (0, 0), max(1.0, blur / 4))
    return cv2.resize(crop, (w, h), interpolation=cv2.INTER_CUBIC)
