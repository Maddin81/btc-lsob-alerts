"""FastAPI-Anwendung: Web-Dashboard, MJPEG-Live-Streams, PTZ, Bewegungs-Ereignisse."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .camera import CameraWorker
from .config import AppConfig, load_config
from .onvif_ptz import discover

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("camera-nvr")

CONFIG_PATH = os.environ.get("CAMERA_NVR_CONFIG", "/config/config.yaml")

# Globaler Zustand, in lifespan gefuellt.
STATE: dict = {"config": None, "workers": {}}


def _cleanup_loop(cfg: AppConfig, stop: threading.Event) -> None:
    """Loescht alte Ereignis-Snapshots gemaess retention_days."""
    while not stop.wait(3600):  # stuendlich pruefen
        if cfg.retention_days <= 0:
            continue
        cutoff = time.time() - cfg.retention_days * 86400
        for root, _dirs, files in os.walk(cfg.events_dir):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(CONFIG_PATH)
    STATE["config"] = cfg
    os.makedirs(cfg.events_dir, exist_ok=True)

    workers: dict[str, CameraWorker] = {}
    for cam in cfg.cameras:
        w = CameraWorker(cam, cfg)
        w.start()
        workers[cam.id] = w
    STATE["workers"] = workers

    stop = threading.Event()
    threading.Thread(target=_cleanup_loop, args=(cfg, stop), daemon=True).start()
    log.info("Camera-NVR gestartet mit %d Kamera(s).", len(workers))

    yield

    stop.set()
    for w in workers.values():
        w.stop()


app = FastAPI(title="Camera-NVR", lifespan=lifespan)
security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    cfg: AppConfig = STATE["config"]
    if not cfg or not cfg.auth_user:
        return  # Kein Login konfiguriert.
    if (
        credentials is None
        or not secrets.compare_digest(credentials.username, cfg.auth_user)
        or not secrets.compare_digest(credentials.password, cfg.auth_pass)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht autorisiert",
            headers={"WWW-Authenticate": "Basic"},
        )


def _worker(camera_id: str) -> CameraWorker:
    w = STATE["workers"].get(camera_id)
    if not w:
        raise HTTPException(status_code=404, detail="Kamera nicht gefunden")
    return w


@app.get("/api/cameras")
def list_cameras(_: None = Depends(require_auth)) -> JSONResponse:
    workers = STATE["workers"]
    return JSONResponse([w.status() for w in workers.values()])


@app.get("/api/stream/{camera_id}")
def stream(camera_id: str, _: None = Depends(require_auth)) -> StreamingResponse:
    worker = _worker(camera_id)

    def gen():
        boundary = b"--frame\r\n"
        while True:
            jpeg = worker.get_jpeg()
            yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            time.sleep(0.1)  # ~10 fps im Browser, schont die CPU

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/snapshot/{camera_id}")
def snapshot(camera_id: str, _: None = Depends(require_auth)):
    worker = _worker(camera_id)
    return StreamingResponse(iter([worker.get_jpeg()]), media_type="image/jpeg")


@app.post("/api/ptz/{camera_id}")
def ptz(
    camera_id: str,
    pan: float = Query(0.0, ge=-1.0, le=1.0),
    tilt: float = Query(0.0, ge=-1.0, le=1.0),
    zoom: float = Query(0.0, ge=-1.0, le=1.0),
    stop_move: bool = Query(False, alias="stop"),
    _: None = Depends(require_auth),
) -> JSONResponse:
    worker = _worker(camera_id)
    if not worker.ptz:
        raise HTTPException(status_code=400, detail="Kamera unterstuetzt kein PTZ")
    ok = worker.ptz.stop() if stop_move else worker.ptz.move(pan, tilt, zoom)
    return JSONResponse({"ok": ok})


@app.get("/api/discover")
def discover_devices(_: None = Depends(require_auth)) -> JSONResponse:
    return JSONResponse({"devices": discover(timeout=4)})


@app.get("/api/events/{camera_id}")
def events(camera_id: str, limit: int = 100, _: None = Depends(require_auth)) -> JSONResponse:
    cfg: AppConfig = STATE["config"]
    base = os.path.join(cfg.events_dir, camera_id)
    items: list[dict] = []
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for f in sorted(files, reverse=True):
                if f.endswith(".jpg"):
                    rel = os.path.relpath(os.path.join(root, f), cfg.events_dir)
                    items.append({"file": rel, "ts": os.path.getmtime(os.path.join(root, f))})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return JSONResponse({"events": items[:limit]})


@app.get("/events-media/{path:path}")
def events_media(path: str, _: None = Depends(require_auth)):
    cfg: AppConfig = STATE["config"]
    # Path-Traversal verhindern.
    full = os.path.normpath(os.path.join(cfg.events_dir, path))
    if not full.startswith(os.path.abspath(cfg.events_dir)):
        raise HTTPException(status_code=400, detail="Ungueltiger Pfad")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    with open(full, "rb") as fh:
        data = fh.read()
    return StreamingResponse(iter([data]), media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)) -> HTMLResponse:
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "static", "index.html"), "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


# Statische Assets (JS/CSS).
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
