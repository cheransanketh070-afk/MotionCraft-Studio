"""Hardened HTTP server (stdlib only) exposing the generation API and the web UI."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import sys
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, security as sec
from .config import Settings, load
from .engine.brain import build_plan
from .engine.render import QUALITY_ORDER
from .jobs import JobManager

WEB = Path(__file__).resolve().parents[1] / "web"
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{16,32}$")
DURATIONS, ASPECTS = (5, 10, 15), ("9:16", "16:9")


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


def make_handler(cfg: Settings, jobs: JobManager, limiter: sec.RateLimiter):
    max_body = cfg.max_upload_mb * 1024 * 1024 + 64 * 1024

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MotionCraft"
        sys_version = ""
        timeout = 30

        # ------------------------------------------------------- plumbing
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, re.sub(r"[\r\n]", "", self.path.split("?")[0])))

        def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            for k, v in sec.SECURITY_HEADERS.items():
                self.send_header(k, v)
            if self.headers.get("X-Forwarded-Proto") == "https":
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            origin = self.headers.get("Origin", "")
            if origin and origin in cfg.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, status: int, obj: dict, extra: dict | None = None):
            body = json.dumps(obj).encode()
            self._headers(status, "application/json; charset=utf-8", len(body), {"Cache-Control": "no-store", **(extra or {})})
            if self.command != "HEAD":
                self.wfile.write(body)

        def _ip(self) -> str:
            return sec.client_ip(self, cfg.trust_proxy_hops)

        def _guard(self, bucket: str, per_min: int):
            if not limiter.allow(bucket, self._ip(), per_min):
                raise ApiError(429, "Too many requests. Please slow down.")

        def _auth(self):
            if cfg.access_keys and not sec.key_ok(sec.bearer(self.headers), cfg.access_keys):
                raise ApiError(401, "A valid access key is required.")

        def _read_body(self) -> bytes:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                raise ApiError(411, "Content-Length required.")
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ApiError(400, "Bad Content-Length.")
            if n < 0 or n > max_body:
                raise ApiError(413, "Request body too large.")
            buf = bytearray()
            while len(buf) < n:
                chunk = self.rfile.read(min(65536, n - len(buf)))
                if not chunk:
                    raise ApiError(400, "Incomplete body.")
                buf += chunk
            return bytes(buf)

        def _form(self) -> tuple[dict, bytes | None]:
            body = self._read_body()
            ctype = self.headers.get("Content-Type", "")
            fields, image = {}, None
            if ctype.startswith("application/json"):
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    raise ApiError(400, "Invalid JSON.")
                if not isinstance(data, dict):
                    raise ApiError(400, "JSON object expected.")
                return data, None
            if ctype.startswith("multipart/form-data"):
                msg = BytesParser(policy=policy.HTTP).parsebytes(
                    b"Content-Type: " + ctype.encode("latin-1", "ignore") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if not name:
                        continue
                    payload = part.get_payload(decode=True) or b""
                    if name == "image":
                        image = payload or None
                    elif len(payload) < 8192:
                        fields[name] = payload.decode("utf-8", "replace")
                return fields, image
            raise ApiError(415, "Use multipart/form-data or application/json.")

        # ------------------------------------------------------- dispatch
        def _route(self, method: str):
            path = self.path.split("?", 1)[0]
            try:
                self._guard("general", cfg.rate_general_per_min)
                if method in ("GET", "HEAD"):
                    return self._get(path)
                if method == "POST":
                    return self._post(path)
                if method == "DELETE":
                    return self._delete(path)
                if method == "OPTIONS":
                    self._headers(204, "text/plain", 0, {
                        "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
                        "Access-Control-Allow-Headers": "Authorization,Content-Type,X-API-Key",
                        "Access-Control-Max-Age": "600"})
                    return
                raise ApiError(405, "Method not allowed.")
            except ApiError as e:
                self.close_connection = self.close_connection or e.status in (400, 411, 413, 415)
                self._json(e.status, {"error": e.msg})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:                      # never leak internals
                sys.stderr.write(f"internal error: {type(e).__name__}: {e}\n")
                self.close_connection = True
                try:
                    self._json(500, {"error": "Internal server error."})
                except Exception:
                    pass

        do_GET = lambda self: self._route("GET")
        do_HEAD = lambda self: self._route("HEAD")
        do_POST = lambda self: self._route("POST")
        do_DELETE = lambda self: self._route("DELETE")
        do_OPTIONS = lambda self: self._route("OPTIONS")

        # ------------------------------------------------------------ GET
        def _get(self, path: str):
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__})
            if path == "/api/v1/config":
                return self._json(200, {
                    "brand": cfg.brand_name, "version": __version__, "auth_required": bool(cfg.access_keys),
                    "durations": list(DURATIONS), "aspects": list(ASPECTS),
                    "qualities": QUALITY_ORDER[: QUALITY_ORDER.index(cfg.max_quality) + 1]
                    if cfg.max_quality in QUALITY_ORDER else QUALITY_ORDER,
                    "max_upload_mb": cfg.max_upload_mb, "limits": {"prompt": 1000, "field": 80}})
            if path == "/api/v1/admin/stats":
                if not cfg.admin_key or not sec.key_ok(sec.bearer(self.headers), (cfg.admin_key,)):
                    raise ApiError(404, "Not found.")
                return self._json(200, jobs.stats())
            m = re.match(r"^/api/v1/generations/([^/]+)(?:/(video|thumb))?$", path)
            if m:
                jid, what = m.group(1), m.group(2)
                if not ID_RE.match(jid):
                    raise ApiError(404, "Not found.")
                if what is None:
                    job = jobs.get(jid)
                    if not job:
                        raise ApiError(404, "Not found.")
                    return self._json(200, job)
                f = jobs.path(jid, "output.mp4" if what == "video" else "thumb.jpg")
                if not f:
                    raise ApiError(404, "Not found.")
                return self._file(f, "video/mp4" if what == "video" else "image/jpeg", download=self._wants_download(),
                                  name=f"motioncraft-{jid[:8]}.mp4")
            return self._static(path)

        def _wants_download(self) -> bool:
            return "download=1" in self.path.split("?", 1)[-1] if "?" in self.path else False

        def _static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            rel = path.lstrip("/")
            target = (WEB / rel).resolve()
            if not target.is_file() or not target.is_relative_to(WEB.resolve()):
                raise ApiError(404, "Not found.")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            cache = "no-cache" if target.suffix == ".html" else "public, max-age=3600"
            return self._file(target, ctype, cache=cache)

        def _file(self, f: Path, ctype: str, cache: str = "private, max-age=3600", download: bool = False, name: str = ""):
            size = f.stat().st_size
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
                if not m or (m.group(1) == "" and m.group(2) == ""):
                    return self._headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                if m.group(1) == "":
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(end, int(m.group(2)))
                if start > end or start >= size:
                    return self._headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                status = 206
            length = end - start + 1
            extra = {"Accept-Ranges": "bytes", "Cache-Control": cache}
            if status == 206:
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            if download:
                extra["Content-Disposition"] = f'attachment; filename="{name}"'
            self._headers(status, ctype, length, extra)
            if self.command == "HEAD":
                return
            with open(f, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        # ----------------------------------------------------------- POST
        def _params(self, f: dict) -> dict:
            prompt = sec.clean_text(f.get("prompt"), 1000)
            if len(prompt) < 3:
                raise ApiError(400, "Please describe your ad (at least 3 characters).")
            try:
                duration = int(f.get("duration", 5))
                seed = int(f.get("seed", 0) or 0)
            except (TypeError, ValueError):
                raise ApiError(400, "Invalid number.")
            aspect = str(f.get("aspect", "9:16"))
            quality = str(f.get("quality", "draft")).lower()
            allowed = QUALITY_ORDER[: QUALITY_ORDER.index(cfg.max_quality) + 1] if cfg.max_quality in QUALITY_ORDER else QUALITY_ORDER
            if duration not in DURATIONS:
                raise ApiError(400, "Duration must be 5, 10 or 15 seconds.")
            if aspect not in ASPECTS:
                raise ApiError(400, "Aspect ratio must be 9:16 or 16:9.")
            if quality not in allowed:
                raise ApiError(400, f"Quality must be one of: {', '.join(allowed)}.")
            audio = str(f.get("audio", "true")).lower() not in ("false", "0", "no", "off")
            ov = {k: sec.clean_text(f.get(k), 80) for k in ("product_name", "headline", "offer", "cta", "tagline")}
            return dict(prompt=prompt, duration=duration, aspect=aspect, quality=quality, seed=max(0, min(seed, 2**31 - 1)),
                        audio=audio, overrides=ov)

        def _post(self, path: str):
            if path == "/api/v1/plan":
                self._auth()
                fields, _ = self._form()
                p = self._params(fields)
                plan = build_plan(p["prompt"], p["duration"], p["aspect"], p["seed"], p["overrides"])
                return self._json(200, plan.to_dict())
            if path == "/api/v1/generations":
                self._auth()
                self._guard("generate", cfg.rate_generate_per_min)
                owner = self._ip()
                if jobs.active_count() >= cfg.queue_max:
                    raise ApiError(503, "The render queue is full. Try again shortly.")
                if jobs.active_count(owner) >= cfg.per_ip_active:
                    raise ApiError(429, "You already have videos rendering. Please wait for them to finish.")
                fields, raw = self._form()
                params = self._params(fields)
                png = None
                if raw:
                    try:
                        png = sec.validate_image(raw, cfg.max_upload_mb * 1024 * 1024, cfg.max_pixels).png
                    except sec.UploadError as e:
                        raise ApiError(400, str(e))
                elif isinstance(fields.get("image_base64"), str) and fields["image_base64"]:
                    import base64
                    try:
                        png = sec.validate_image(base64.b64decode(fields["image_base64"], validate=True),
                                                 cfg.max_upload_mb * 1024 * 1024, cfg.max_pixels).png
                    except (sec.UploadError, ValueError) as e:
                        raise ApiError(400, str(e) if isinstance(e, sec.UploadError) else "Invalid base64 image.")
                jid = jobs.create(params, png, owner)
                return self._json(202, jobs.get(jid), {"Location": f"/api/v1/generations/{jid}"})
            raise ApiError(404, "Not found.")

        def _delete(self, path: str):
            self._auth()
            m = re.match(r"^/api/v1/generations/([^/]+)$", path)
            if not m or not ID_RE.match(m.group(1)) or not jobs.cancel_or_delete(m.group(1)):
                raise ApiError(404, "Not found.")
            self._json(200, {"ok": True})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def main() -> None:
    cfg = load()
    jobs = JobManager(cfg)
    jobs.start()
    httpd = Server((cfg.host, cfg.port), make_handler(cfg, jobs, sec.RateLimiter()))
    mode = "private (access keys required)" if cfg.access_keys else "OPEN (rate-limited, no access key)"
    print(f"MotionCraft {__version__} listening on {cfg.host}:{cfg.port} — {mode}; workers={cfg.workers}", flush=True)

    def _bye(*_):
        jobs.stop()
        httpd.shutdown()
    signal.signal(signal.SIGTERM, _bye)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.stop()


if __name__ == "__main__":
    main()
