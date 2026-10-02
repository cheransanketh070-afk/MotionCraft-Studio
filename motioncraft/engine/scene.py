"""Scene compositor: procedural backgrounds, 2.5D product camera, light, fx, kinetic text."""
from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass

import cv2
import numpy as np

from . import typography as ty
from .brain import STYLE_PRESETS, Plan
from .typography import blit, smooth
from .vision import Subject, cover_plate


def _ease(x: float) -> float:
    return smooth(x)


def _lift(rgb, min_v=0.62, max_v=1.0):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    v = min(max(v, min_v), max_v)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def _shift_hue(rgb, d):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb((h + d) % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def camera(motion: str, p: float, tl: float) -> dict:
    """Camera state for a shot. p = shot progress 0..1, tl = local seconds."""
    e = _ease(p)
    c = dict(scale=1.0, dx=0.0, dy=0.0, rot=0.0, yaw=0.0, pitch=0.0, bg_scale=1.1, bg_dx=0.0, bg_dy=0.0)
    if motion == "push":
        c.update(scale=1.0 + 0.2 * e, dy=-0.01 * e, yaw=-6 + 5 * e, bg_scale=1.08 + 0.06 * e, bg_dy=-0.01 * e)
    elif motion == "orbit":
        c.update(scale=1.02 + 0.05 * math.sin(math.pi * p), yaw=-26 + 52 * e, rot=2.2 * (1 - 2 * e),
                 bg_dx=0.035 * (1 - 2 * e), bg_scale=1.12)
    elif motion == "macro":
        c.update(scale=1.6 - 0.58 * e, dy=0.1 * (1 - e), yaw=4 * math.sin(2 * math.pi * p), bg_scale=1.22 - 0.1 * e)
    elif motion == "float":
        c.update(scale=1.04 + 0.03 * e, dy=0.016 * math.sin(2 * math.pi * tl / 3.0),
                 rot=1.8 * math.sin(2 * math.pi * tl / 4.0), yaw=9 * math.sin(2 * math.pi * tl / 5.0),
                 bg_dy=-0.012 * math.sin(2 * math.pi * tl / 3.0), bg_scale=1.1)
    else:  # sweep
        c.update(scale=1.05, dx=0.06 - 0.12 * e, yaw=-11 + 22 * e, bg_dx=-0.045 + 0.09 * e, bg_scale=1.12)
    return c


def _quad(cx, cy, w, h, s, rot, yaw, pitch, f):
    pts = np.array([[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]], np.float32) * s
    ya, pa, ra = map(math.radians, (yaw, pitch, rot))
    x, y = pts[:, 0], pts[:, 1]
    x1, z1 = x * math.cos(ya), x * math.sin(ya)
    y2, z2 = y * math.cos(pa) - z1 * math.sin(pa), y * math.sin(pa) + z1 * math.cos(pa)
    x3, y3 = x1 * math.cos(ra) - y2 * math.sin(ra), x1 * math.sin(ra) + y2 * math.cos(ra)
    k = f / (f + z2)
    return np.stack([cx + x3 * k, cy + y3 * k], 1).astype(np.float32)


@dataclass
class _Layout:
    pos: dict          # key -> (x, y)
    cx: float
    cy: float
    box_w: float
    box_h: float


class Scene:
    def __init__(self, plan: Plan, subject: Subject | None, W: int, H: int, fps: int,
                 brand: str = "", watermark: str = ""):
        self.plan, self.subject, self.W, self.H, self.fps = plan, subject, W, H, fps
        self.portrait = H >= W
        P = STYLE_PRESETS[plan.style]
        self.P = P
        self.dark = P["dark"]
        acc = subject.accent if subject else P["accent"]
        acc2 = subject.accent2 if subject else _shift_hue(P["accent"], 0.08)
        if self.dark:
            acc, acc2 = _lift(acc, 0.72), _lift(acc2, 0.6)
        else:
            acc, acc2 = _lift(acc, 0.35, 0.82), _lift(acc2, 0.35, 0.9)
        self.accent, self.accent2 = acc, acc2
        self.fg = (246, 246, 250) if self.dark else (16, 18, 28)
        lum = 0.299 * acc[0] + 0.587 * acc[1] + 0.114 * acc[2]
        self.on_accent = (16, 16, 24) if lum > 160 else (255, 255, 255)
        self.pname_color = tuple(int(a * 0.55 + f * 0.45) for a, f in zip(acc, self.fg))
        self.u = min(W, H) / 100.0
        self.rng = np.random.default_rng(plan.seed)
        self._build_vignette()
        self._build_grain()
        self._build_particles()
        self._build_product()
        self._build_text(watermark)
        self._layout()
        self._bg_cache: dict[int, np.ndarray] = {}
        self._sweep_ramp = self._make_ramp()

    # ---------------------------------------------------------------- setup
    def _build_vignette(self):
        h, w = self.H // 4, self.W // 4
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / 1.414
        v = 1.0 - (0.5 if self.dark else 0.22) * np.clip(r, 0, 1) ** 2.2
        self.vig = cv2.resize(v, (self.W, self.H), interpolation=cv2.INTER_CUBIC)[..., None].astype(np.float32)

    def _build_grain(self):
        self.grain = [(self.rng.standard_normal((self.H // 2, self.W // 2)).astype(np.float32) * 0.009)
                      for _ in range(4)]

    def _build_particles(self):
        kind = self.plan.particles
        n = {"none": 0, "dust": 42, "bokeh": 26, "sparks": 52}[kind]
        r = self.rng
        self.pt = dict(kind=kind, x=r.random(n), y=r.random(n), z=0.4 + r.random(n) * 0.9,
                       rad=(1.2 + r.random(n) * (5 if kind == "bokeh" else 1.8)) * self.u / 7.2,
                       vx=(r.random(n) - 0.5) * 0.04, vy=-(0.012 + r.random(n) * 0.05),
                       ph=r.random(n) * 6.28, fr=0.5 + r.random(n) * 1.8)

    def _build_product(self):
        s = self.subject
        self.spr = self.shadow = self.refl = None
        if not s:
            return
        W, H = self.W, self.H
        if self.portrait:
            bw, bh = W * 0.78, H * 0.40
        else:
            bw, bh = W * 0.42, H * 0.66
        h0, w0 = s.rgba.shape[:2]
        k = min(bw / w0, bh / h0)
        sw, sh = max(8, int(w0 * k)), max(8, int(h0 * k))
        rgba = cv2.resize(s.rgba, (sw, sh), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
        pad = int(max(sw, sh) * 0.12)
        canvas = np.zeros((sh + 2 * pad, sw + 2 * pad, 4), np.float32)
        canvas[pad:pad + sh, pad:pad + sw] = rgba.astype(np.float32) / 255
        canvas[..., :3] *= canvas[..., 3:4]                 # premultiply
        self.spr = canvas
        a = canvas[..., 3]
        sa = cv2.GaussianBlur(a, (0, 0), max(2.0, sw * 0.035)) * (0.5 if self.dark else 0.3)
        self.shadow = np.dstack([np.zeros_like(canvas[..., :3]), sa]).astype(np.float32)
        self.pw, self.ph = sw, sh
        self.pad = pad
        # mirrored floor reflection (dark looks only)
        if self.dark:
            fl = canvas[::-1].copy()
            ramp = np.linspace(0.22, 0.0, fl.shape[0], dtype=np.float32)[:, None, None] ** 1.0
            ramp[: pad] = 0.22 * np.linspace(0, 1, pad, dtype=np.float32)[:, None, None]
            self.refl = fl * ramp * np.clip(np.linspace(1.6, 0, fl.shape[0]), 0, 1)[:, None, None].astype(np.float32)

    def _build_text(self, watermark: str):
        p, u, W, H = self.plan, self.u, self.W, self.H
        kind = "serif" if self.P["weight"] == "serif" else "sans"
        maxw = int(W * 0.84) if self.portrait else int(W * 0.46)
        align = "center" if self.portrait else "left"
        hs = int(u * (8.4 if self.portrait else 9.4))
        T = {}
        if p.offer:
            T["offer"] = ty.chip(p.offer, int(u * 3.3), self.on_accent, self.accent)
        T["headline"] = ty.layout_words(p.headline, kind, hs, self.fg, maxw, align)
        T["tagline"] = ty.layout_words(p.tagline, kind, int(u * 5.6), self.fg, maxw, align)
        if p.product_name:
            T["product"] = ty.layout_words(p.product_name.upper(), "sans", int(u * 3.4), self.pname_color, maxw, align,
                                           tracking=0.14)
        T["cta"] = ty.button(p.cta, int(u * 4.0), self.on_accent, self.accent, self.accent2)
        self.T = T
        self.wm = None
        if watermark:
            self.wm = ty.layout_words(watermark, "sans", int(u * 2.2), self.fg, int(W * 0.5), "right")

    def _layout(self):
        W, H, u = self.W, self.H, self.u
        self.L = []
        for shot in self.plan.shots:
            keys = [t["key"] for t in shot.texts if t["key"] in self.T]
            pos = {}
            gap = int(u * 2.2)
            def bx(b):
                return int((W - b.w) / 2) if self.portrait else int(W * 0.07)
            if self.portrait:
                y = int(H * 0.065)
                for k in [k for k in ("offer", "headline", "tagline") if k in keys]:
                    pos[k] = (bx(self.T[k]), y)
                    y += self.T[k].h + gap
                y = int(H * 0.945)
                for k in [k for k in ("cta", "product") if k in keys]:
                    y -= self.T[k].h
                    pos[k] = (bx(self.T[k]), y)
                    y -= gap
                cx, cy, bw, bh = W * 0.5, H * 0.56, W * 0.78, H * 0.40
            else:
                order = [k for k in ("offer", "headline", "tagline", "product", "cta") if k in keys]
                total = sum(self.T[k].h for k in order) + gap * max(0, len(order) - 1)
                y = int((H - total) / 2)
                for k in order:
                    pos[k] = (bx(self.T[k]), y)
                    y += self.T[k].h + gap
                cx, cy, bw, bh = W * 0.73, H * 0.5, W * 0.42, H * 0.66
            self.L.append(_Layout(pos, cx, cy, bw, bh))

    def _make_ramp(self):
        h, w = self.H // 4, self.W // 4
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        return (xx / w) * 0.8 + (yy / h) * 0.6

    # ------------------------------------------------------------ background
    def _background(self, si: int) -> np.ndarray:
        if si in self._bg_cache:
            return self._bg_cache[si]
        W, H, P = self.W, self.H, self.P
        h, w = H // 4, W // 4
        top, bot = np.array(P["top"], np.float32), np.array(P["bot"], np.float32)
        acc = np.array(self.accent, np.float32)
        hs = self.plan.shots[si].hue_shift
        if hs:
            acc = np.array(_shift_hue(tuple(int(c) for c in acc), hs), np.float32)
        t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
        base = (top * (1 - t) + bot * t) * np.ones((1, w, 1), np.float32)
        base = base * 0.88 + acc * 0.12 * (0.55 if self.dark else 0.35)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        L = self.L[si]
        for (gx, gy, s, k) in ((L.cx / W, L.cy / H, 0.45, 0.55), (0.15 + 0.7 * ((si + 1) % 2), 0.15 + 0.7 * (si % 2), 0.38, 0.28)):
            d2 = ((xx / w - gx) ** 2 + (yy / h - gy) ** 2)
            g = np.exp(-d2 / (2 * s * s * 0.5))[..., None]
            base = base + g * acc * k * (0.55 if self.dark else 0.3)
        bg = cv2.resize(np.clip(base, 0, 255), (W, H), interpolation=cv2.INTER_CUBIC) / 255.0
        if self.subject is not None:
            plate = cover_plate(self.subject.plate, W, H, W * 0.06).astype(np.float32) / 255.0
            mix = 0.38 if self.dark else 0.14
            plate = plate * (0.55 if self.dark else 1.0)
            bg = bg * (1 - mix) + plate * mix
        self._bg_cache[si] = bg.astype(np.float32)
        return self._bg_cache[si]

    # ----------------------------------------------------------------- frame
    def _shot_frame(self, si: int, tl: float) -> np.ndarray:
        W, H = self.W, self.H
        shot = self.plan.shots[si]
        dur = shot.end - shot.start
        p = min(1.0, max(0.0, tl / dur))
        cam = camera(shot.motion, p, tl)
        L = self.L[si]

        # background with parallax drift
        s = cam["bg_scale"]
        M = np.float32([[s, 0, W / 2 - s * W / 2 + cam["bg_dx"] * W], [0, s, H / 2 - s * H / 2 + cam["bg_dy"] * H]])
        frame = cv2.warpAffine(self._background(si), M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

        self._halo(frame, L, tl)
        self._particles(frame, tl, back=True)
        wp = None
        if self.spr is not None:
            wp = self._product(frame, cam, L, si, tl, p)
        self._sweep(frame, wp, p, tl)
        self._particles(frame, tl, back=False)
        self._text(frame, si, tl, L)
        return frame

    def _halo(self, frame, L, tl):
        h, w = self.H // 4, self.W // 4
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt((xx - L.cx / 4) ** 2 + (yy - L.cy / 4) ** 2)
        R = min(L.box_w, L.box_h) / 4 * 0.62 * (1 + 0.03 * math.sin(tl * 1.7))
        ring = np.exp(-((r - R) / (R * 0.16)) ** 2) * 0.55 + np.exp(-(r / (R * 0.9)) ** 2) * 0.35
        acc = np.array(self.accent, np.float32) / 255.0
        k = 0.30 if self.dark else 0.16
        frame += cv2.resize(ring, (self.W, self.H), interpolation=cv2.INTER_LINEAR)[..., None] * acc * k

    def _particles(self, frame, tl, back: bool):
        pt = self.pt
        n = len(pt["x"])
        if not n:
            return
        sel = (pt["z"] < 0.9) if back else (pt["z"] >= 0.9)
        if not sel.any():
            return
        h, w = self.H // 4, self.W // 4
        canvas = np.zeros((h, w, 3), np.float32)
        acc = np.array(self.accent, np.float32) / 255.0
        col = acc * 0.5 + 0.5 if pt["kind"] != "bokeh" else acc
        for i in np.where(sel)[0]:
            x = ((pt["x"][i] + pt["vx"][i] * tl * pt["z"][i]) % 1.0) * w
            y = ((pt["y"][i] + pt["vy"][i] * tl * pt["z"][i]) % 1.0) * h
            tw = 0.55 + 0.45 * math.sin(tl * pt["fr"][i] * 2 + pt["ph"][i])
            rad = max(1.0, float(pt["rad"][i]) * pt["z"][i] / 4 * 2)
            c = col * tw * (0.9 if pt["kind"] == "sparks" else 0.55)
            if pt["kind"] == "sparks":
                cv2.line(canvas, (int(x), int(y)), (int(x), int(y + rad * 3)), tuple(float(v) for v in c), 1, cv2.LINE_AA)
            else:
                cv2.circle(canvas, (int(x), int(y)), int(round(rad)), tuple(float(v) for v in c), -1, cv2.LINE_AA)
        sig = 2.2 if pt["kind"] == "bokeh" else 0.9
        canvas = cv2.GaussianBlur(canvas, (0, 0), sig)
        gain = 1.0 if self.dark else 0.55
        frame += cv2.resize(canvas, (self.W, self.H), interpolation=cv2.INTER_LINEAR) * gain

    def _product(self, frame, cam, L, si, tl, p):
        W, H = self.W, self.H
        s = cam["scale"]
        alpha = 1.0
        if si == 0:                                    # hero reveal on first shot
            r = smooth(tl / 0.9)
            s *= 0.86 + 0.14 * r
            alpha = smooth(tl / 0.5)
        cx, cy = L.cx + cam["dx"] * W, L.cy + cam["dy"] * H
        f = 1.8 * max(W, H)
        sh, sw = self.spr.shape[:2]
        src = np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]])
        flags = dict(flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))

        # ground contact shadow + soft drop shadow
        bottom = cy + self.ph * s / 2
        gh, gw = H // 4, W // 4
        g = np.zeros((gh, gw), np.float32)
        cv2.ellipse(g, (int(cx / 4), int((bottom + self.ph * 0.03) / 4)),
                    (max(2, int(self.pw * s * 0.38 / 4)), max(1, int(self.ph * 0.035 * s / 4))), 0, 0, 360, 1.0, -1, cv2.LINE_AA)
        g = cv2.GaussianBlur(g, (0, 0), 3.0)
        frame *= 1.0 - cv2.resize(g, (W, H), interpolation=cv2.INTER_LINEAR)[..., None] * (0.55 if self.dark else 0.3) * alpha

        dst = _quad(cx, cy + H * 0.02, sw, sh, s * 0.99, cam["rot"], cam["yaw"], cam["pitch"], f)
        wsh = cv2.warpPerspective(self.shadow, cv2.getPerspectiveTransform(src, dst), (W, H), **flags)
        frame *= 1.0 - wsh[..., 3:4] * alpha
        if self.refl is not None:
            rc = cy + self.ph * s + self.pad * 0.2 * s
            dst_r = _quad(cx, rc, sw, sh, s, -cam["rot"], cam["yaw"], cam["pitch"], f)
            wr = cv2.warpPerspective(self.refl, cv2.getPerspectiveTransform(src, dst_r), (W, H), **flags)
            frame += wr[..., :3] * alpha
        dst = _quad(cx, cy, sw, sh, s, cam["rot"], cam["yaw"], cam["pitch"], f)
        wp = cv2.warpPerspective(self.spr, cv2.getPerspectiveTransform(src, dst), (W, H), **flags)
        # rim light: bright edge in accent colour, strongest mid-shot
        frame *= 1.0 - wp[..., 3:4] * alpha
        frame += wp[..., :3] * alpha
        return wp[..., 3] * alpha

    def _sweep(self, frame, wp, p, tl):
        if wp is None:
            return
        pos = -0.3 + 1.9 * smooth((p - 0.22) / 0.5)
        if pos <= -0.25 or pos >= 1.55:
            return
        band = np.exp(-((self._sweep_ramp - pos) / 0.07) ** 2).astype(np.float32)
        band = cv2.resize(band, (self.W, self.H), interpolation=cv2.INTER_LINEAR)
        k = 0.5 if self.dark else 0.3
        frame += (band * wp)[..., None] * k

    def _text(self, frame, si, tl, L):
        shot = self.plan.shots[si]
        for t in shot.texts:
            k = t["key"]
            blk = self.T.get(k)
            if blk is None or k not in L.pos:
                continue
            lt = tl - t["t_in"]
            if lt < 0:
                continue
            out = 1.0 - smooth((tl - t["t_out"] + 0.2) / 0.25)
            x0, y0 = L.pos[k]
            if k == "cta":
                a = smooth(lt / 0.45) * out
                spr = blk.words[0].spr.copy()
                shine = np.exp(-(((np.arange(spr.shape[1]) / spr.shape[1]) - ((lt * 0.55) % 1.6 - 0.3)) / 0.07) ** 2)
                spr[..., :3] += (shine[None, :, None] * spr[..., 3:4] * 0.35).astype(np.float32)
                pul = 1 + 0.035 * math.sin(lt * 7.0) * smooth(lt / 0.8)
                spr = ty.resize_sprite(spr, pul * (0.9 + 0.1 * smooth(lt / 0.45)))
                dw, dh = spr.shape[1] - blk.words[0].spr.shape[1], spr.shape[0] - blk.words[0].spr.shape[0]
                blit(frame, spr, x0 + blk.words[0].x - dw // 2, y0 + blk.words[0].y - dh // 2, a)
                continue
            for i, w in enumerate(blk.words):
                step = 0.07 if len(blk.words) > 1 else 0.0
                pw = smooth((lt - i * step) / 0.5)
                if pw <= 0:
                    continue
                rise = int((1 - pw) * self.u * 3.2)
                blit(frame, w.spr, x0 + w.x, y0 + w.y + rise, pw * out)
        if self.wm is not None:
            w = self.wm.words[0]
            for w in self.wm.words:
                blit(frame, w.spr, self.W - self.wm.w - int(self.u * 4) + w.x, self.H - int(self.u * 6) + w.y, 0.55)

    # --------------------------------------------------------------- compose
    def render(self, t: float) -> np.ndarray:
        shots = self.plan.shots
        TR = 0.5
        si = len(shots) - 1
        for i, s in enumerate(shots):
            if t < s.end:
                si = i
                break
        frame = None
        tl = t - shots[si].start
        # transition window centred on a boundary
        for i in range(len(shots) - 1):
            b = shots[i].end
            if abs(t - b) < TR / 2:
                w = smooth((t - (b - TR / 2)) / TR)
                a = self._shot_frame(i, t - shots[i].start)
                bfr = self._shot_frame(i + 1, max(0.0, t - shots[i + 1].start))
                frame = a * (1 - w) + bfr * w
                pulse = math.sin(math.pi * w)
                frame += pulse ** 2 * 0.12
                z = 1.0 + 0.05 * pulse
                if z > 1.001:
                    M = np.float32([[z, 0, self.W / 2 * (1 - z)], [0, z, self.H / 2 * (1 - z)]])
                    frame = cv2.warpAffine(frame, M, (self.W, self.H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
                break
        if frame is None:
            frame = self._shot_frame(si, tl)
        return self._finish(frame, t)

    def _finish(self, f: np.ndarray, t: float) -> np.ndarray:
        P = self.P
        if P["bloom"] > 0:
            small = cv2.resize(f, (self.W // 4, self.H // 4), interpolation=cv2.INTER_AREA)
            bright = np.clip(small - 0.62, 0, None) * 1.6
            bright = cv2.GaussianBlur(bright, (0, 0), 5)
            f += cv2.resize(bright, (self.W, self.H), interpolation=cv2.INTER_LINEAR) * P["bloom"] * 0.6
        c = P["contrast"]
        f = (f - 0.5) * c + 0.5
        if P["sat"] != 1.0:
            gray = f.mean(axis=2, keepdims=True)
            f = gray + (f - gray) * P["sat"]
        f *= self.vig
        g = self.grain[int(t * self.fps) % len(self.grain)]
        f += cv2.resize(g, (self.W, self.H), interpolation=cv2.INTER_NEAREST)[..., None]
        # fade in/out
        d = self.plan.duration
        fade = min(1.0, t / 0.25) * min(1.0, (d - t) / 0.35)
        f *= max(0.0, fade)
        return (np.clip(f, 0, 1) * 255 + 0.5).astype(np.uint8)
