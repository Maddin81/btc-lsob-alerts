"""FastAPI-Anwendung: Web-Dashboard, MJPEG-Live-Streams, PTZ, Bewegungs-Ereignisse."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .autodetect import DEFAULT_CREDENTIALS, build_config_yaml
from .camera import CameraWorker
from .config import AppConfig, load_config
from .onvif_ptz import COMMON_ONVIF_PORTS, discover, probe_onvif

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("camera-nvr")

CONFIG_PATH = os.environ.get("CAMERA_NVR_CONFIG", "/config/config.yaml")

# Globaler Zustand, in lifespan gefuellt.
STATE: dict = {"config": None, "workers": {}, "setup_mode": False}
_reload_lock = threading.Lock()


def _start_workers(cfg: AppConfig) -> dict:
    workers: dict[str, CameraWorker] = {}
    for cam in cfg.cameras:
        w = CameraWorker(cam, cfg)
        w.start()
        workers[cam.id] = w
    return workers


def reload_from_config() -> int:
    """Laedt config.yaml neu und startet die Kamera-Worker neu.
    Gibt die Anzahl konfigurierter Kameras zurueck."""
    with _reload_lock:
        cfg = load_config(CONFIG_PATH)
        for w in STATE.get("workers", {}).values():
            w.stop()
        STATE["config"] = cfg
        STATE["workers"] = _start_workers(cfg)
        STATE["setup_mode"] = False
        os.makedirs(cfg.events_dir, exist_ok=True)
        return len(cfg.cameras)


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
    try:
        cfg = load_config(CONFIG_PATH)
        STATE["config"] = cfg
        STATE["setup_mode"] = False
        os.makedirs(cfg.events_dir, exist_ok=True)
        STATE["workers"] = _start_workers(cfg)
        log.info("Camera-NVR gestartet mit %d Kamera(s).", len(cfg.cameras))
    except (FileNotFoundError, ValueError) as exc:
        # Noch keine (gueltige) Konfiguration -> Einrichtungsmodus.
        # Das Dashboard fuehrt dann grafisch durch die Kamera-Erkennung.
        cfg = AppConfig()
        STATE["config"] = cfg
        STATE["workers"] = {}
        STATE["setup_mode"] = True
        os.makedirs(cfg.events_dir, exist_ok=True)
        log.warning("Keine gueltige config.yaml (%s) - starte im Einrichtungsmodus.", exc)

    stop = threading.Event()
    threading.Thread(target=_cleanup_loop, args=(cfg, stop), daemon=True).start()

    yield

    stop.set()
    for w in STATE.get("workers", {}).values():
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


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Unauthentifizierter Health-Check fuer Docker/Synology."""
    workers = STATE.get("workers", {})
    return JSONResponse(
        {
            "status": "ok",
            "cameras": len(workers),
            "connected": sum(1 for w in workers.values() if w.connected),
        }
    )


@app.get("/api/state")
def app_state(_: None = Depends(require_auth)) -> JSONResponse:
    """Sagt dem Dashboard, ob es in den Einrichtungsmodus gehen soll."""
    return JSONResponse(
        {"setup_mode": bool(STATE.get("setup_mode")), "cameras": len(STATE.get("workers", {}))}
    )


@app.get("/api/cameras")
def list_cameras(_: None = Depends(require_auth)) -> JSONResponse:
    workers = STATE["workers"]
    return JSONResponse([w.status() for w in workers.values()])


@app.post("/api/save-config")
def save_config(payload: dict = Body(...), _: None = Depends(require_auth)) -> JSONResponse:
    """Schreibt die (per Assistent erzeugte) config.yaml und startet die
    Kamera-Worker neu - ohne Container-Neustart, komplett aus dem Browser."""
    yaml_text = (payload or {}).get("config_yaml", "")
    if not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Leere Konfiguration")

    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(yaml_text)
    # Vor dem Uebernehmen validieren.
    try:
        load_config(tmp)
    except Exception as exc:  # noqa: BLE001
        os.remove(tmp)
        raise HTTPException(status_code=400, detail=f"Ungueltige Konfiguration: {exc}")

    os.replace(tmp, CONFIG_PATH)
    count = reload_from_config()
    log.info("Konfiguration gespeichert, %d Kamera(s) aktiv.", count)
    return JSONResponse({"ok": True, "cameras": count})


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


@app.post("/api/autoconfig")
def autoconfig(
    user: str = Query("", description="ONVIF-Benutzer"),
    password: str = Query("", description="ONVIF-Passwort"),
    host: str = Query("", description="Optional: einzelne IP statt Netz-Suche"),
    _: None = Depends(require_auth),
) -> JSONResponse:
    """Sucht Kameras, fragt sie per ONVIF nach ihren echten RTSP-URLs ab und
    liefert eine fertige config.yaml zurueck. Der Nutzer muss die RTSP-Pfade
    also nicht selbst kennen."""
    creds: list[tuple[str, str]] = []
    if user:
        creds.append((user, password))
    creds += DEFAULT_CREDENTIALS

    if host:
        targets: list[tuple[str, int | None]] = [(host, None)]
    else:
        targets = [(d["address"], d.get("onvif_port")) for d in discover(timeout=4) if d.get("address")]

    found: list[dict] = []
    for h, port_hint in targets:
        ports = [port_hint] + [p for p in COMMON_ONVIF_PORTS if p != port_hint] if port_hint else None
        info = None
        for u, pw in creds:
            info = probe_onvif(h, u, pw, ports=ports)
            if info:
                info["username"], info["password"] = u, pw
                break
        if info:
            found.append(info)

    return JSONResponse(
        {
            "count": len(found),
            "cameras": [
                {
                    "host": c["host"],
                    "onvif_port": c["onvif_port"],
                    "manufacturer": c.get("manufacturer", ""),
                    "model": c.get("model", ""),
                    "ptz": c["ptz"],
                    "rtsp_main": c["rtsp_main"],
                    "rtsp_sub": c["rtsp_sub"],
                }
                for c in found
            ],
            "config_yaml": build_config_yaml(found) if found else "",
        }
    )


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
    # Path-Traversal verhindern (auch Geschwister-Ordner wie /data/events-x).
    events_root = os.path.abspath(cfg.events_dir)
    full = os.path.abspath(os.path.join(events_root, path))
    try:
        if os.path.commonpath([full, events_root]) != events_root:
            raise ValueError
    except ValueError:
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
