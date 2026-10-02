# MotionCraft Studio

Self-hosted, free, open-source **prompt + image → MP4 ad generator**. No third-party APIs, no API keys to vendors, no token metering: the whole "AI" stack runs inside this repo.

Output: playable **H.264 + AAC MP4**, **5 / 10 / 15 s**, **9:16 or 16:9**, 540p / 720p / 1080p, with synthesised music.

## How it works (Layer 1 — "Aurora Engine")

```
prompt ──► Prompt Director ──► storyboard (style, shots, camera, copy, music brief)
image  ──► Vision layer    ──► subject cutout + palette + ambient plate
                 │
                 ▼
        Scene compositor  (2.5D perspective camera, parallax, halo, reflection, light sweep,
                           particles, bloom, grade, kinetic typography, shot transitions)
        Audio synthesiser (pad, bass, kick/hat, arpeggio, risers, impacts — numpy, no samples)
                 │
                 ▼
        ffmpeg (local binary) → H.264/AAC MP4 → job queue → HTTP API → web UI
```

| Layer | Path | Role |
|---|---|---|
| Director | `motioncraft/engine/brain.py` | Local NLP: style/motion/offer/CTA/product extraction, shot planning |
| Vision | `motioncraft/engine/vision.py` | Alpha / background keying / GrabCut / card fallback, k-means palette |
| Compositor | `motioncraft/engine/scene.py`, `typography.py` | Frame renderer |
| Audio | `motioncraft/engine/audio.py` | Procedural soundtrack |
| Pipeline | `motioncraft/engine/render.py` | Frames → ffmpeg, cancel, timeout, progress |
| Platform | `motioncraft/jobs.py`, `server.py`, `security.py` | SQLite queue, hardened API, auth, limits |
| UI | `web/` | Dark studio UI, library, plan preview |

### What this is — and isn't
This is a **procedural / computer-vision generative engine**: it directs, animates and scores a *real* product image with cinematic camera work. It does **not** invent new pixels from nothing like Sora or Veo — that needs a trained diffusion model and GPUs (tens of GB of weights), which cannot honestly be "built from scratch" inside a repo. Layer 2 (below) adds a local neural engine behind the same API.

"Unlimited tokens": there is no token or credit system at all — only a render queue. Throughput is bounded by your CPU, nothing else.

## Run locally

```bash
# needs Python 3.10+ and ffmpeg on PATH
pip install -r requirements.txt
cp .env.example .env            # optional; export the vars or use your own loader
python run.py                   # http://localhost:8080
python -m unittest discover -s tests -v
```
Or `docker build -t motioncraft . && docker run -p 8080:8080 -v mc:/data motioncraft`.

## Your private data
Everything private is an environment variable (see `.env.example`):
`ACCESS_KEYS` (who may generate; empty = public), `ADMIN_KEY`, `BRAND_NAME`, `WATERMARK_TEXT`, `ALLOWED_ORIGINS`, limits.
Drop your own fonts at `assets/fonts/display.ttf` and `display-serif.ttf` to brand the typography.

## Push to GitHub
```bash
cd motioncraft
git init -b main && git add . && git commit -m "MotionCraft Studio: Aurora Engine"
git remote add origin https://github.com/<you>/motioncraft-studio.git
git push -u origin main
```

## Deploy on Render
1. Render → **New → Blueprint** → select the repo (`render.yaml` is picked up).
2. Set `ACCESS_KEYS` in the dashboard (e.g. `my-long-random-key`). `ADMIN_KEY` is auto-generated.
3. Deploy. Health check: `/healthz`.

**Capacity note:** measured on 1 CPU core: a 5 s 540p clip ≈ 12 s, a 10 s 720p clip ≈ 70 s. Use a Starter-or-larger instance and raise `WORKERS` with more cores. Renders run in a queue, so extra requests wait instead of failing.

## API
```bash
curl -X POST $HOST/api/v1/generations -H "Authorization: Bearer $KEY" \
  -F prompt="Luxury ad for Aurora Perfume, 20% off, order now" \
  -F duration=10 -F aspect=9:16 -F quality=hd -F image=@bottle.png
# → 202 {"id": "..."}     poll: GET /api/v1/generations/{id}     file: GET /api/v1/generations/{id}/video
```
Other endpoints: `POST /api/v1/plan` (storyboard preview), `DELETE /api/v1/generations/{id}`, `GET /api/v1/admin/stats`, `GET /healthz`.
Fields: `prompt`, `duration` (5|10|15), `aspect` (9:16|16:9), `quality` (draft|hd|fhd), `audio`, `seed`, optional `product_name`, `headline`, `offer`, `cta`, `tagline`.

## Roadmap (layer by layer)
1. ✅ **Aurora Engine** — director, vision, compositor, audio, queue, secure API, UI, Docker/Render/CI.
2. **Neural engine adapter** — `Engine` interface so a self-hosted image-to-video model (open weights, GPU worker) can replace/blend with the compositor, selected per job.
3. **Scene library** — more camera rigs, lens effects, depth-from-image (monocular depth model), background replacement, multi-image storyboards.
4. **Accounts & projects** — users, saved projects, shareable links, usage dashboards.
5. **Editor** — timeline, text/brand kits, voice-over (local TTS), captions, resize/export presets.
6. **Scale-out** — Redis/Postgres, separate GPU/CPU workers, object storage.

MIT licensed. See `SECURITY.md` for the security model.
