"""Render pipeline: plan -> vision -> scene frames + synthesised audio -> H.264/AAC MP4."""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from . import audio as audio_mod
from .brain import STYLE_PRESETS, Plan, build_plan
from .scene import Scene
from .vision import analyze

QUALITY_SHORT_SIDE = {"draft": 540, "hd": 720, "fhd": 1080}
QUALITY_ORDER = ["draft", "hd", "fhd"]
FPS = 30


class Cancelled(Exception):
    pass


@dataclass
class RenderResult:
    path: Path
    thumb: Path
    width: int
    height: int
    duration: float
    plan: dict
    has_audio: bool


def canvas_size(aspect: str, quality: str) -> tuple[int, int]:
    s = QUALITY_SHORT_SIDE[quality]
    long_ = int(round(s * 16 / 9 / 2)) * 2
    return (s, long_) if aspect == "9:16" else (long_, s)


def render_video(*, prompt: str, duration: int, aspect: str, quality: str, seed: int, overrides: dict,
                 image_png: bytes | None, out_dir: Path, preset: str = "veryfast", brand: str = "",
                 watermark: str = "", with_audio: bool = True, timeout_s: int = 1800,
                 progress: Callable[[str, float], None] = lambda s, p: None,
                 cancelled: Callable[[], bool] = lambda: False,
                 ffmpeg: str = "ffmpeg") -> RenderResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    progress("Directing", 0.02)
    plan: Plan = build_plan(prompt, duration, aspect, seed, overrides)
    W, H = canvas_size(aspect, quality)

    subject = None
    if image_png:
        progress("Analysing image", 0.05)
        subject = analyze(image_png, STYLE_PRESETS[plan.style]["accent"])
        plan.notes.append(f"Subject isolation: {subject.method}.")
    progress("Building scene", 0.10)
    scene = Scene(plan, subject, W, H, FPS, brand=brand, watermark=watermark)

    wav_path = None
    tmp = tempfile.TemporaryDirectory(prefix="mc_", dir=str(out_dir))
    try:
        if with_audio:
            progress("Composing music", 0.14)
            hits = [s.end for s in plan.shots[:-1]]
            pcm = audio_mod.synth(duration, plan.bpm, plan.root, plan.scale, plan.wave, plan.seed, hits)
            wav_path = Path(tmp.name) / "score.wav"
            audio_mod.write_wav(wav_path, pcm)
        if cancelled():
            raise Cancelled()

        out = out_dir / "output.mp4"
        part = out_dir / "output.part.mp4"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
        if wav_path:
            cmd += ["-i", str(wav_path)]
        cmd += ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-preset", preset,
                "-crf", "24" if quality != "draft" else "26", "-maxrate", {"draft": "3M", "hd": "6M", "fhd": "10M"}[quality], "-bufsize", "12M", "-g", str(FPS * 2),
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
        if wav_path:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-shortest"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", "-f", "mp4", str(part)]

        errlog = open(Path(tmp.name) / "ffmpeg.log", "wb+")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog, stdout=subprocess.DEVNULL)
        total = duration * FPS
        thumb_frame = None
        import time
        t0 = time.monotonic()
        try:
            for i in range(total):
                if cancelled():
                    raise Cancelled()
                if time.monotonic() - t0 > timeout_s:
                    raise TimeoutError("Render exceeded the time limit.")
                frame = scene.render(i / FPS)
                if i == int(total * 0.3):
                    thumb_frame = frame
                proc.stdin.write(frame.tobytes())
                if i % 3 == 0:
                    progress("Rendering frames", 0.16 + 0.78 * (i / total))
            proc.stdin.close()
            progress("Encoding", 0.95)
            rc = proc.wait(timeout=300)
        except BaseException:
            proc.kill()
            proc.wait()
            part.unlink(missing_ok=True)
            raise
        if rc != 0:
            errlog.seek(0)
            part.unlink(missing_ok=True)
            raise RuntimeError("Encoder failed: " + errlog.read().decode(errors="replace")[-300:])
        part.replace(out)
    finally:
        try:
            errlog.close()
        except NameError:
            pass
        tmp.cleanup()

    thumb = out_dir / "thumb.jpg"
    im = Image.fromarray(thumb_frame)
    im.thumbnail((480, 480))
    im.save(thumb, "JPEG", quality=82)
    progress("Done", 1.0)
    return RenderResult(out, thumb, W, H, float(duration), plan.to_dict(), bool(wav_path))


def probe(path: Path, ffprobe: str = "ffprobe") -> dict:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_name,codec_type,width,height",
                        "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, timeout=30, check=True)
    return json.loads(r.stdout)
