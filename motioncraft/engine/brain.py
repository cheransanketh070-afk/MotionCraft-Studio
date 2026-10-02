"""Prompt Director — the platform's own, fully local planning brain.

Turns a free-text prompt into a deterministic storyboard: style, camera moves,
palette bias, copy, shot timeline and music brief. No network, no tokens, no quota.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import asdict, dataclass, field

STYLE_LEX: dict[str, list[str]] = {
    "luxury": ["luxury", "premium", "gold", "golden", "elegant", "exclusive", "watch", "jewel", "perfume", "classy", "high-end", "cinematic", "royal"],
    "minimal": ["minimal", "clean", "simple", "white", "studio", "calm", "soft", "pure", "light"],
    "neon": ["neon", "cyber", "energy", "drink", "gaming", "night", "party", "glow", "electric", "bold", "vibrant", "club"],
    "social": ["tiktok", "reel", "social", "viral", "ugc", "fast", "trendy", "youth", "sale", "hype", "shorts", "streetwear"],
    "beauty": ["beauty", "skincare", "cosmetic", "makeup", "serum", "lipstick", "spa", "cream", "fragrance", "hair", "pastel"],
    "tech": ["tech", "gadget", "phone", "laptop", "ai", "future", "smart", "device", "headphone", "speaker", "robot", "software", "app"],
    "fresh": ["fresh", "organic", "natural", "eco", "water", "bottle", "green", "nature", "fitness", "health", "hydration", "summer", "outdoor"],
    "warm": ["food", "coffee", "cafe", "burger", "pizza", "bakery", "restaurant", "tea", "chocolate", "delicious", "cozy", "warm", "sunset"],
}

MOTION_LEX: dict[str, list[str]] = {
    "orbit": ["orbit", "rotate", "360", "spin", "turntable", "around"],
    "macro": ["macro", "zoom", "close-up", "closeup", "detail", "texture", "reveal"],
    "float": ["float", "levitate", "hover", "fly", "weightless", "drift"],
    "sweep": ["sweep", "pan", "slide", "glide", "parallax"],
    "push": ["push", "dolly", "approach", "dramatic", "epic"],
}

# palette: (bg_top, bg_bottom, accent) in RGB 0-255; dark styles use white text.
STYLE_PRESETS: dict[str, dict] = {
    "luxury":  dict(dark=True,  top=(26, 20, 12),  bot=(5, 4, 3),     accent=(222, 176, 82),  particles="dust",   bloom=0.55, contrast=1.12, sat=1.02, motions=["orbit", "push", "macro"], bpm=84,  root=50, scale="minor",  wave="soft", weight="serif"),
    "minimal": dict(dark=False, top=(250, 250, 252), bot=(222, 226, 234), accent=(79, 70, 229), particles="none",   bloom=0.15, contrast=1.04, sat=1.0,  motions=["float", "push", "sweep"], bpm=96,  root=57, scale="major",  wave="soft", weight="sans"),
    "neon":    dict(dark=True,  top=(18, 8, 44),   bot=(4, 2, 14),    accent=(0, 229, 255),   particles="sparks", bloom=0.9,  contrast=1.18, sat=1.2,  motions=["orbit", "sweep", "push"], bpm=124, root=45, scale="minor",  wave="synth", weight="sans"),
    "social":  dict(dark=True,  top=(36, 10, 44),  bot=(8, 4, 22),    accent=(255, 64, 129),  particles="sparks", bloom=0.6,  contrast=1.15, sat=1.25, motions=["push", "orbit", "sweep"], bpm=128, root=48, scale="major",  wave="synth", weight="sans"),
    "beauty":  dict(dark=False, top=(255, 240, 244), bot=(246, 214, 224), accent=(214, 76, 128), particles="bokeh", bloom=0.5,  contrast=1.02, sat=1.05, motions=["float", "macro", "orbit"], bpm=90,  root=55, scale="major",  wave="soft", weight="serif"),
    "tech":    dict(dark=True,  top=(8, 18, 38),   bot=(2, 5, 14),    accent=(96, 165, 250),  particles="dust",   bloom=0.7,  contrast=1.14, sat=1.05, motions=["orbit", "macro", "push"], bpm=110, root=43, scale="minor",  wave="synth", weight="sans"),
    "fresh":   dict(dark=False, top=(234, 250, 244), bot=(190, 232, 220), accent=(16, 150, 120), particles="bokeh", bloom=0.35, contrast=1.05, sat=1.12, motions=["float", "sweep", "push"], bpm=104, root=52, scale="major",  wave="soft", weight="sans"),
    "warm":    dict(dark=True,  top=(44, 22, 10),  bot=(12, 6, 3),    accent=(255, 152, 56),  particles="bokeh",  bloom=0.6,  contrast=1.1,  sat=1.12, motions=["macro", "push", "float"], bpm=92,  root=48, scale="major",  wave="soft", weight="sans"),
}

HEADLINES = {
    "luxury": ["Timeless. Refined. Yours.", "Crafted beyond ordinary", "Elegance, perfected"],
    "minimal": ["Simply better", "Less noise. More you.", "Designed to disappear"],
    "neon": ["Turn the energy up", "Ignite your night", "Feel the charge"],
    "social": ["Everyone's talking about it", "Your new obsession", "Stop scrolling. Start owning."],
    "beauty": ["Glow like never before", "Your ritual, elevated", "Radiance, redefined"],
    "tech": ["The future, in your hands", "Engineered to impress", "Smarter by design"],
    "fresh": ["Fresh. Natural. Alive.", "Better for you, every day", "Naturally made"],
    "warm": ["Made to be savoured", "Taste the difference", "Warm, rich, unforgettable"],
}
TAGLINES = {
    "luxury": "Every detail, considered.", "minimal": "Clean lines. Clear purpose.",
    "neon": "Built to be seen.", "social": "Limited drop — don't miss out.",
    "beauty": "Gentle on you. Stunning results.", "tech": "Precision you can feel.",
    "fresh": "Good things, naturally.", "warm": "Fresh, handcrafted, delicious.",
}
CTAS = [(r"\blearn more\b", "LEARN MORE"), (r"\border now\b", "ORDER NOW"), (r"\bbuy now\b", "BUY NOW"),
        (r"\bget yours\b", "GET YOURS"), (r"\btry (it )?(now|today|free)\b", "TRY IT NOW"),
        (r"\bsign ?up\b", "SIGN UP"), (r"\bbook (now|today)\b", "BOOK NOW"), (r"\bshop now\b", "SHOP NOW")]
OFFER_PATTERNS = [
    (r"(\d{1,2})\s*%\s*(?:off|discount)", lambda m: f"{m.group(1)}% OFF"),
    (r"buy\s*(\d)\s*get\s*(\d)", lambda m: f"BUY {m.group(1)} GET {m.group(2)}"),
    (r"free\s+shipping", lambda m: "FREE SHIPPING"),
    (r"(?:new\s+(?:arrival|launch|collection)|just\s+launched|launch(?:ing)?)", lambda m: "NEW ARRIVAL"),
    (r"limited\s+(?:time|edition|offer)", lambda m: "LIMITED EDITION"),
]
_PRODUCT_RE = re.compile(
    r"(?:\bfor|\bof|advertis(?:e|ing)|promot(?:e|ing)|showcas(?:e|ing)|introducing)\s+(?:an?\s+|the\s+|our\s+|my\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9'&\- ]{2,38}?)(?=\s*(?:[,.;:!]|\bwith\b|\bin\b|\bon\b|\bthat\b|\bat\b|\bfeaturing\b|\bunder\b)|$)",
    re.I,
)
_QUOTE_RE = re.compile(r"[\"“”']([^\"“”']{3,60})[\"“”']")


@dataclass
class Shot:
    start: float
    end: float
    motion: str
    role: str                     # hook | detail | cta
    hue_shift: float
    texts: list[dict] = field(default_factory=list)   # {key, t_in, t_out} (shot-local seconds)


@dataclass
class Plan:
    style: str
    seed: int
    duration: int
    aspect: str
    product_name: str
    headline: str
    tagline: str
    offer: str
    cta: str
    motions: list[str]
    bpm: int
    root: int
    scale: str
    wave: str
    particles: str
    shots: list[Shot]
    notes: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _tokens(prompt: str) -> list[str]:
    return re.findall(r"[a-z0-9\-%]+", prompt.lower())


def _score(prompt: str, lex: dict[str, list[str]]) -> dict[str, int]:
    low = prompt.lower()
    toks = set(_tokens(prompt))
    out = {}
    for name, words in lex.items():
        out[name] = sum(1 for w in words if (w in toks if " " not in w and "-" not in w else w in low))
    return out


def _title(s: str) -> str:
    s = s.strip(" .,-")
    return s[:1].upper() + s[1:] if s else s


def build_plan(prompt: str, duration: int, aspect: str, seed: int = 0, overrides: dict | None = None) -> Plan:
    ov = {k: v for k, v in (overrides or {}).items() if v}
    digest = hashlib.sha256(f"{prompt}|{seed}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    notes: list[str] = []

    scores = _score(prompt, STYLE_LEX)
    best = max(scores.values())
    if best == 0:
        style = rng.choice(["luxury", "tech", "neon", "minimal"]) if not prompt.strip() else "luxury"
        notes.append("No style keywords found — using the cinematic luxury look.")
    else:
        style = rng.choice([k for k, v in scores.items() if v == best])
        notes.append(f"Detected style: {style}.")
    P = STYLE_PRESETS[style]

    mscore = _score(prompt, MOTION_LEX)
    ranked = [k for k, v in sorted(mscore.items(), key=lambda kv: -kv[1]) if v > 0]
    pool = ranked + [m for m in P["motions"] if m not in ranked]
    n_shots = {5: 1, 10: 2, 15: 3}[duration]
    motions = pool[:n_shots]
    if len(motions) < n_shots:
        motions += [m for m in ("push", "orbit", "float") if m not in motions][: n_shots - len(motions)]
    if not ranked:
        rng.shuffle(motions)
    notes.append("Camera: " + ", ".join(motions) + ".")

    low = prompt.lower()
    quote = _QUOTE_RE.search(prompt)
    headline = ov.get("headline") or (_title(quote.group(1)) if quote else rng.choice(HEADLINES[style]))
    product = ov.get("product_name")
    if not product:
        m = _PRODUCT_RE.search(prompt)
        product = _title(m.group(1)) if m else ""
        product = re.sub(r"\b(ad|advert|advertisement|video|commercial|short)\b$", "", product, flags=re.I).strip()
    offer = ov.get("offer", "")
    if not offer:
        for pat, fn in OFFER_PATTERNS:
            m = re.search(pat, low)
            if m:
                offer = fn(m)
                break
    cta = ov.get("cta", "")
    if not cta:
        cta = "SHOP NOW"
        for pat, label in CTAS:
            if re.search(pat, low):
                cta = label
                break

    # shot timeline -------------------------------------------------------
    per = duration / n_shots
    shots: list[Shot] = []
    for i in range(n_shots):
        role = "hook" if i == 0 else ("cta" if i == n_shots - 1 else "detail")
        s = Shot(start=round(i * per, 3), end=round((i + 1) * per, 3), motion=motions[i], role=role,
                 hue_shift=round((i * 0.05) * (1 if rng.random() > .5 else -1), 3))
        d = per
        if n_shots == 1:
            s.texts = [dict(key="offer", t_in=0.35, t_out=d), dict(key="headline", t_in=0.55, t_out=d),
                       dict(key="product", t_in=1.5, t_out=d), dict(key="cta", t_in=d * 0.52, t_out=d)]
        elif role == "hook":
            s.texts = [dict(key="offer", t_in=0.3, t_out=d - 0.2), dict(key="headline", t_in=0.5, t_out=d - 0.2)]
        elif role == "detail":
            s.texts = [dict(key="product", t_in=0.4, t_out=d - 0.2), dict(key="tagline", t_in=0.9, t_out=d - 0.2)]
        else:
            s.texts = [dict(key="product", t_in=0.3, t_out=d), dict(key="cta", t_in=0.9, t_out=d)]
        shots.append(s)

    return Plan(style=style, seed=int.from_bytes(digest[8:12], "big"), duration=duration, aspect=aspect,
                product_name=product[:60], headline=headline[:80], tagline=ov.get("tagline", TAGLINES[style]),
                offer=offer[:30], cta=cta[:24], motions=motions, bpm=P["bpm"], root=P["root"], scale=P["scale"],
                wave=P["wave"], particles=P["particles"], shots=shots, notes=notes)
