"""FastAPI-Anwendung: Web-Dashboard, MJPEG-Live-Streams, PTZ, Bewegungs-Ereignisse."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

import yaml

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .autodetect import DEFAULT_CREDENTIALS, camera_entry, scan_subnet, _local_subnet
from .camera import CameraWorker
from .config import (
    AppConfig,
    dump_raw,
    load_raw,
    normalize_camera,
    parse_config,
    save_raw,
    slugify,
    unique_id,
)
from .onvif_ptz import COMMON_ONVIF_PORTS, discover, probe_onvif

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("camera-nvr")

CONFIG_PATH = os.environ.get("CAMERA_NVR_CONFIG", "/config/config.yaml")

# Globaler Zustand, in lifespan gefuellt.
# "raw" ist der unveraenderte YAML-Baum (mit ${VAR}), "config" die aufgeloeste
# Fassung fuer die Laufzeit.
STATE: dict = {"config": None, "raw": None, "workers": {}, "setup_mode": False}
_reload_lock = threading.Lock()


def _apply(cfg: AppConfig) -> int:
    """Uebernimmt eine neue Konfiguration und faehrt NUR die Kameras neu an,
    die sich geaendert haben - die uebrigen Live-Bilder laufen weiter."""
    old: dict[str, CameraWorker] = STATE.get("workers", {})
    new: dict[str, CameraWorker] = {}

    for cam in cfg.cameras:
        if not cam.enabled:
            continue
        w = old.get(cam.id)
        if w is not None and w.cam == cam:
            w.app_cfg = cfg  # z.B. geaenderter Alarm-Cooldown
            new[cam.id] = w
            continue
        if w is not None:
            w.stop()
        nw = CameraWorker(cam, cfg)
        nw.start()
        new[cam.id] = nw

    for cid, w in old.items():
        if cid not in new:
            w.stop()

    STATE["config"] = cfg
    STATE["workers"] = new
    STATE["setup_mode"] = not cfg.cameras
    try:
        os.makedirs(cfg.events_dir, exist_ok=True)
    except OSError as exc:
        # Kein Grund, den Dienst nicht zu starten - nur Ereignis-Snapshots
        # koennen dann nicht abgelegt werden.
        log.warning("Ereignis-Ordner %s nicht anlegbar: %s", cfg.events_dir, exc)
    return len(new)


def reload_from_config() -> int:
    """Liest config.yaml neu von der Platte und uebernimmt sie."""
    with _reload_lock:
        raw = load_raw(CONFIG_PATH)
        STATE["raw"] = raw
        return _apply(parse_config(raw))


def _save_and_apply(raw: dict) -> int:
    """Schreibt den geaenderten YAML-Baum und uebernimmt ihn sofort."""
    with _reload_lock:
        try:
            cfg = parse_config(raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Ungueltige Konfiguration: {exc}")
        save_raw(CONFIG_PATH, raw)
        STATE["raw"] = raw
        return _apply(cfg)


def _raw_cameras() -> list[dict]:
    raw = STATE.get("raw") or load_raw(CONFIG_PATH)
    STATE["raw"] = raw
    raw.setdefault("cameras", [])
    return raw["cameras"]


def _cleanup_loop(_cfg: AppConfig, stop: threading.Event) -> None:
    """Loescht alte Ereignis-Snapshots gemaess retention_days."""
    while not stop.wait(3600):  # stuendlich pruefen
        cfg: AppConfig = STATE["config"]  # kann sich zur Laufzeit aendern
        if not cfg or cfg.retention_days <= 0:
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
        count = reload_from_config()
        log.info("Camera-NVR gestartet mit %d aktiven Kamera(s).", count)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Noch keine (gueltige) Konfiguration -> Einrichtungsmodus.
        # Das Dashboard fuehrt dann grafisch durch die Kamera-Erkennung.
        STATE["raw"] = None
        STATE["config"] = AppConfig()
        STATE["workers"] = {}
        STATE["setup_mode"] = True
        log.warning("Keine gueltige config.yaml (%s) - starte im Einrichtungsmodus.", exc)

    cfg: AppConfig = STATE["config"]
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
    """Laufzeit-Status aller AKTIVEN Kameras (fuer das Live-Gitter)."""
    workers = STATE["workers"]
    return JSONResponse([w.status() for w in workers.values()])


@app.get("/api/config/cameras")
def config_cameras(_: None = Depends(require_auth)) -> JSONResponse:
    """Alle KONFIGURIERTEN Kameras - auch abgeschaltete. Grundlage der
    Verwaltungsliste. Passwoerter werden nicht ausgeliefert."""
    out = []
    for c in _raw_cameras():
        entry = {k: v for k, v in c.items() if k != "password"}
        entry["has_password"] = bool(c.get("password"))
        out.append(entry)
    return JSONResponse({"cameras": out})


@app.post("/api/config/cameras")
def add_camera(payload: dict = Body(...), _: None = Depends(require_auth)) -> JSONResponse:
    """Eine einzelne Kamera hinzufuegen - beliebig oft wiederholbar, jede mit
    eigenen Zugangsdaten, eigenem Port und eigenem Bildformat."""
    cams = _raw_cameras()
    taken = {str(c.get("id", "")) for c in cams}
    data = dict(payload or {})
    if not str(data.get("id", "")).strip():
        data["id"] = unique_id(slugify(str(data.get("name", "")), "cam"), taken)
    try:
        cam = normalize_camera(data, taken)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw = STATE["raw"]
    raw["cameras"] = cams + [cam]
    count = _save_and_apply(raw)
    log.info("Kamera '%s' hinzugefuegt, %d aktiv.", cam["id"], count)
    return JSONResponse({"ok": True, "camera": cam["id"], "cameras": count})


@app.put("/api/config/cameras/{camera_id}")
def update_camera(camera_id: str, payload: dict = Body(...), _: None = Depends(require_auth)) -> JSONResponse:
    cams = _raw_cameras()
    idx = next((i for i, c in enumerate(cams) if str(c.get("id")) == camera_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Kamera nicht gefunden")

    merged = dict(cams[idx])
    merged.update({k: v for k, v in (payload or {}).items() if v is not None})
    # Leeres Passwortfeld im Formular = Passwort unveraendert lassen.
    if not str((payload or {}).get("password", "")).strip():
        merged["password"] = cams[idx].get("password", "")
    merged["id"] = camera_id

    taken = {str(c.get("id")) for c in cams} - {camera_id}
    try:
        cam = normalize_camera(merged, taken)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw = STATE["raw"]
    raw["cameras"] = cams[:idx] + [cam] + cams[idx + 1:]
    count = _save_and_apply(raw)
    log.info("Kamera '%s' geaendert, %d aktiv.", camera_id, count)
    return JSONResponse({"ok": True, "camera": camera_id, "cameras": count})


@app.delete("/api/config/cameras/{camera_id}")
def delete_camera(camera_id: str, _: None = Depends(require_auth)) -> JSONResponse:
    cams = _raw_cameras()
    rest = [c for c in cams if str(c.get("id")) != camera_id]
    if len(rest) == len(cams):
        raise HTTPException(status_code=404, detail="Kamera nicht gefunden")
    raw = STATE["raw"]
    raw["cameras"] = rest
    count = _save_and_apply(raw)
    log.info("Kamera '%s' entfernt, %d aktiv.", camera_id, count)
    return JSONResponse({"ok": True, "cameras": count})


@app.get("/api/config/yaml")
def config_yaml(_: None = Depends(require_auth)) -> JSONResponse:
    """Die aktuelle config.yaml zum Ansehen (Passwoerter wie gespeichert -
    bei ${VAR}-Nutzung also nur die Platzhalter)."""
    return JSONResponse({"config_yaml": dump_raw(STATE.get("raw") or load_raw(CONFIG_PATH))})


@app.post("/api/save-config")
def save_config(payload: dict = Body(...), _: None = Depends(require_auth)) -> JSONResponse:
    """Komplette config.yaml ersetzen (Rohtext-Weg / Assistent)."""
    yaml_text = (payload or {}).get("config_yaml", "")
    if not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Leere Konfiguration")
    try:
        raw = yaml.safe_load(yaml_text) or {}
        if not isinstance(raw, dict):
            raise ValueError("kein Objekt auf oberster Ebene")
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML-Fehler: {exc}")

    count = _save_and_apply(raw)
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


def _probe_with_credentials(
    host: str, port_hint: int | None, creds: list[tuple[str, str]]
) -> dict | None:
    ports = None
    if port_hint:
        ports = [port_hint] + [p for p in COMMON_ONVIF_PORTS if p != port_hint]
    for u, pw in creds:
        info = probe_onvif(host, u, pw, ports=ports)
        if info:
            info["username"], info["password"] = u, pw
            return info
    return None


@app.post("/api/probe")
def probe(
    host: str = Query(..., description="IP der Kamera"),
    user: str = Query("", description="ONVIF-Benutzer"),
    password: str = Query("", description="ONVIF-Passwort"),
    onvif_port: int = Query(0, description="Optional: bekannter ONVIF-Port"),
    _: None = Depends(require_auth),
) -> JSONResponse:
    """Fragt EINE Kamera per ONVIF ab und liefert fertige Felder fuers Formular.
    So bekommt jede Kamera ihre eigenen Zugangsdaten, ihren eigenen Port und
    ihr echtes Bildformat - genau das, was mit einem einzigen globalen
    Assistenten nicht ging."""
    creds = ([(user, password)] if user else []) + DEFAULT_CREDENTIALS
    info = _probe_with_credentials(host, onvif_port or None, creds)
    if not info:
        raise HTTPException(
            status_code=404,
            detail="Keine ONVIF-Antwort. Zugangsdaten, Port und ob ONVIF an der Kamera aktiv ist pruefen.",
        )
    return JSONResponse({"ok": True, "camera": camera_entry(info)})


@app.post("/api/autoconfig")
def autoconfig(
    user: str = Query("", description="ONVIF-Benutzer"),
    password: str = Query("", description="ONVIF-Passwort"),
    host: str = Query("", description="Optional: einzelne IP statt Netz-Suche"),
    _: None = Depends(require_auth),
) -> JSONResponse:
    """Sucht Kameras im Netz und fragt sie per ONVIF nach ihren echten
    RTSP-URLs ab. Liefert Vorschlaege - uebernommen wird erst auf Klick,
    und zwar ERGAENZEND zu den schon eingerichteten Kameras."""
    creds: list[tuple[str, str]] = []
    if user:
        creds.append((user, password))
    creds += DEFAULT_CREDENTIALS

    if host:
        targets: list[tuple[str, int | None]] = [(host, None)]
    else:
        targets = [(d["address"], d.get("onvif_port")) for d in discover(timeout=4) if d.get("address")]
        # Fallback: findet Multicast nichts, das lokale /24-Subnetz scannen.
        if not targets:
            subnet = _local_subnet()
            if subnet:
                targets = list(scan_subnet(subnet))

    known_hosts = {str(c.get("host", "")) for c in _raw_cameras()}
    found: list[dict] = []
    for h, port_hint in targets:
        info = _probe_with_credentials(h, port_hint, creds)
        if info:
            entry = camera_entry(info)
            entry["already_configured"] = entry["host"] in known_hosts
            found.append(entry)

    return JSONResponse({"count": len(found), "cameras": found})


@app.post("/api/config/cameras/bulk")
def add_cameras_bulk(payload: dict = Body(...), _: None = Depends(require_auth)) -> JSONResponse:
    """Mehrere gefundene Kameras auf einmal uebernehmen - ohne die bereits
    eingerichteten zu verlieren."""
    items = (payload or {}).get("cameras") or []
    if not items:
        raise HTTPException(status_code=400, detail="Keine Kameras uebergeben")

    cams = _raw_cameras()
    taken = {str(c.get("id", "")) for c in cams}
    added: list[str] = []
    errors: list[str] = []
    for item in items:
        data = dict(item)
        if not str(data.get("id", "")).strip():
            data["id"] = unique_id(slugify(str(data.get("name", "")), "cam"), taken)
        try:
            cam = normalize_camera(data, taken)
        except ValueError as exc:
            errors.append(f"{data.get('name') or data.get('host')}: {exc}")
            continue
        taken.add(cam["id"])
        cams = cams + [cam]
        added.append(cam["id"])

    if not added:
        raise HTTPException(status_code=400, detail="; ".join(errors) or "Nichts uebernommen")

    raw = STATE["raw"]
    raw["cameras"] = cams
    count = _save_and_apply(raw)
    log.info("%d Kamera(s) uebernommen, %d aktiv.", len(added), count)
    return JSONResponse({"ok": True, "added": added, "errors": errors, "cameras": count})


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
