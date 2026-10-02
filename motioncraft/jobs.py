"""Persistent job queue (SQLite) with worker threads, cancellation and retention."""
from __future__ import annotations

import json
import queue
import secrets
import shutil
import sqlite3
import threading
import time
import traceback
from pathlib import Path

from .config import Settings
from .engine.render import Cancelled, render_video

ACTIVE = ("queued", "running")


class JobManager:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._db = sqlite3.connect(cfg.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._q: queue.Queue[str] = queue.Queue()
        self._cancel: set[str] = set()
        self._stop = threading.Event()
        with self._lock:
            self._db.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, status TEXT, stage TEXT, progress REAL, params TEXT, plan TEXT,
                error TEXT, owner TEXT, created REAL, updated REAL, width INT, height INT, duration REAL)""")

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self._lock:     # crash recovery: re-queue anything unfinished
            rows = self._db.execute("SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY created").fetchall()
            self._db.execute("UPDATE jobs SET status='queued', stage='Queued', progress=0 WHERE status='running'")
        for r in rows:
            self._q.put(r["id"])
        for i in range(self.cfg.workers):
            threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True).start()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ api
    def active_count(self, owner: str | None = None) -> int:
        with self._lock:
            if owner:
                r = self._db.execute("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running') AND owner=?", (owner,))
            else:
                r = self._db.execute("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running')")
            return r.fetchone()["c"]

    def create(self, params: dict, image_png: bytes | None, owner: str) -> str:
        jid = secrets.token_urlsafe(16)
        d = self.cfg.jobs_dir / jid
        d.mkdir(parents=True)
        if image_png:
            (d / "input.png").write_bytes(image_png)
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO jobs(id,status,stage,progress,params,owner,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                             (jid, "queued", "Queued", 0.0, json.dumps(params), owner, now, now))
        self._q.put(jid)
        return jid

    def get(self, jid: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return None
        params = json.loads(r["params"])
        out = dict(id=r["id"], status=r["status"], stage=r["stage"], progress=round(r["progress"] or 0, 3),
                   created=r["created"], error=r["error"], duration=params.get("duration"), aspect=params.get("aspect"),
                   quality=params.get("quality"), prompt=params.get("prompt"), width=r["width"], height=r["height"])
        if r["plan"]:
            out["plan"] = json.loads(r["plan"])
        if r["status"] == "done":
            out["video_url"] = f"/api/v1/generations/{jid}/video"
            out["thumb_url"] = f"/api/v1/generations/{jid}/thumb"
        if r["status"] == "queued":
            with self._lock:
                out["queue_position"] = self._db.execute(
                    "SELECT COUNT(*) c FROM jobs WHERE status='queued' AND created<?", (r["created"],)).fetchone()["c"] + 1
        return out

    def cancel_or_delete(self, jid: str) -> bool:
        job = self.get(jid)
        if not job:
            return False
        if job["status"] in ACTIVE:
            self._cancel.add(jid)
            self._set(jid, status="cancelled", stage="Cancelled")
            return True
        self._remove(jid)
        return True

    def stats(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        return {"jobs": {r["status"]: r["c"] for r in rows}, "queue": self._q.qsize(), "workers": self.cfg.workers}

    def path(self, jid: str, name: str) -> Path | None:
        if name not in ("output.mp4", "thumb.jpg"):
            return None
        p = self.cfg.jobs_dir / jid / name
        return p if p.is_file() else None

    # ------------------------------------------------------------- internal
    def _set(self, jid: str, **kw) -> None:
        kw["updated"] = time.time()
        cols = ",".join(f"{k}=?" for k in kw)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), jid))

    def _remove(self, jid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE id=?", (jid,))
        shutil.rmtree(self.cfg.jobs_dir / jid, ignore_errors=True)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                jid = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._run(jid)
            except Exception:
                traceback.print_exc()

    def _run(self, jid: str) -> None:
        job = self.get(jid)
        if not job or job["status"] != "queued":
            return
        with self._lock:
            params = json.loads(self._db.execute("SELECT params FROM jobs WHERE id=?", (jid,)).fetchone()["params"])
        self._set(jid, status="running", stage="Starting", progress=0.01)
        d = self.cfg.jobs_dir / jid
        img = (d / "input.png").read_bytes() if (d / "input.png").exists() else None
        last = [0.0]

        def progress(stage: str, p: float) -> None:
            if time.monotonic() - last[0] > 0.4 or p >= 1:
                last[0] = time.monotonic()
                self._set(jid, stage=stage, progress=p)

        try:
            res = render_video(prompt=params["prompt"], duration=params["duration"], aspect=params["aspect"],
                               quality=params["quality"], seed=params["seed"], overrides=params["overrides"],
                               image_png=img, out_dir=d, preset=self.cfg.ffmpeg_preset, brand=self.cfg.brand_name,
                               watermark=self.cfg.watermark, with_audio=params["audio"],
                               timeout_s=self.cfg.render_timeout_s, progress=progress,
                               cancelled=lambda: jid in self._cancel)
            self._set(jid, status="done", stage="Done", progress=1.0, plan=json.dumps(res.plan),
                      width=res.width, height=res.height, duration=res.duration)
        except Cancelled:
            self._remove(jid)
        except Exception as exc:
            traceback.print_exc()
            self._set(jid, status="error", stage="Failed", error=("Render failed: " + str(exc))[:300])
        finally:
            self._cancel.discard(jid)

    def _janitor(self) -> None:
        while not self._stop.wait(600):
            cutoff = time.time() - self.cfg.retention_hours * 3600
            with self._lock:
                ids = [r["id"] for r in self._db.execute(
                    "SELECT id FROM jobs WHERE created<? AND status NOT IN ('queued','running')", (cutoff,))]
            for jid in ids:
                self._remove(jid)
