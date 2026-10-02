"""Central configuration. Every private value comes from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int, lo: int = 0, hi: int = 10**9) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


def _list(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    access_keys: tuple[str, ...]     # empty => open mode (public, rate-limited)
    admin_key: str                   # empty => admin endpoints disabled
    allowed_origins: tuple[str, ...]
    brand_name: str
    watermark: str
    max_upload_mb: int
    max_pixels: int
    workers: int
    queue_max: int
    per_ip_active: int
    rate_general_per_min: int
    rate_generate_per_min: int
    retention_hours: int
    render_timeout_s: int
    trust_proxy_hops: int            # 0 = ignore X-Forwarded-For
    max_quality: str                 # draft | hd | fhd
    ffmpeg_preset: str

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "motioncraft.sqlite3"


def load() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    s = Settings(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_int("PORT", 8080, 1, 65535),
        data_dir=data_dir,
        access_keys=_list("ACCESS_KEYS"),
        admin_key=os.environ.get("ADMIN_KEY", "").strip(),
        allowed_origins=_list("ALLOWED_ORIGINS"),
        brand_name=os.environ.get("BRAND_NAME", "MotionCraft")[:40],
        watermark=os.environ.get("WATERMARK_TEXT", "")[:40],
        max_upload_mb=_int("MAX_UPLOAD_MB", 12, 1, 100),
        max_pixels=_int("MAX_PIXELS", 40_000_000, 1_000_000, 200_000_000),
        workers=_int("WORKERS", 1, 1, 16),
        queue_max=_int("QUEUE_MAX", 50, 1, 10_000),
        per_ip_active=_int("PER_IP_ACTIVE", 2, 1, 100),
        rate_general_per_min=_int("RATE_GENERAL_PER_MIN", 240, 10, 100_000),
        rate_generate_per_min=_int("RATE_GENERATE_PER_MIN", 8, 1, 10_000),
        retention_hours=_int("RETENTION_HOURS", 24, 1, 24 * 365),
        render_timeout_s=_int("RENDER_TIMEOUT_S", 1800, 30, 86_400),
        trust_proxy_hops=_int("TRUST_PROXY_HOPS", 0, 0, 5),
        max_quality=os.environ.get("MAX_QUALITY", "fhd").lower(),
        ffmpeg_preset=os.environ.get("FFMPEG_PRESET", "veryfast"),
    )
    s.jobs_dir.mkdir(parents=True, exist_ok=True)
    return s
