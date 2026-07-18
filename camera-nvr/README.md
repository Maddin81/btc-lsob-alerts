# Camera-NVR — eigene Software für ONVIF-Kameras

Eine schlanke, selbst gehostete Kamera-Software als Ersatz für die alte
Hersteller-App billiger China-ONVIF-Kameras. Läuft als **Docker-Container**
(z. B. auf einer **Synology DiskStation**) und redet direkt per **ONVIF + RTSP**
mit den Kameras — unabhängig von der Original-Firmware-App.

## Funktionen

- 📺 **Live-Ansicht** aller Kameras im Browser (Gitter-Dashboard, mehrere Betrachter gleichzeitig)
- 🚨 **Bewegungserkennung + Alarm** mit Snapshot und Benachrichtigung (Telegram / Webhook)
- 🎮 **PTZ-Steuerung** (Schwenken / Neigen / Zoom) für Kameras, die das können
- 🔍 **ONVIF-Gerätesuche** im lokalen Netz (findet IP-Adressen automatisch)
- 💾 Ereignis-Snapshots werden persistent gespeichert (mit automatischer Aufräumung)

Es wird **keine** Kamera-Firmware verändert. Die Software ist ein *Client*, der
die Standard-Protokolle nutzt, die praktisch jede „ONVIF"-Kamera spricht.

## Voraussetzungen

- Die Kameras sind im selben Netz erreichbar und ONVIF/RTSP ist aktiviert
  (bei den meisten Modellen im Kamera-Webinterface einschaltbar).
- Docker + Docker Compose (auf Synology über **Container Manager** verfügbar).

## Schnellstart

```bash
# 1. Konfiguration anlegen
mkdir -p config data
cp config.example.yaml config/config.yaml
cp .env.example .env
#   -> config/config.yaml mit deinen Kameras füllen
#   -> .env mit den Kamera-Passwörtern füllen

# 2. Bauen & starten
docker compose up -d --build

# 3. Dashboard öffnen
#    http://<IP-der-Synology>:8080
```

### RTSP-URL automatisch finden (empfohlen)

Du musst die RTSP-Pfade **nicht selbst raten**. Die Software fragt die Kameras
per ONVIF direkt nach ihren echten Stream-URLs. Zwei Wege:

**A) Im Dashboard:** Klick oben auf **„Kameras suchen"**, gib ONVIF-Benutzer +
Passwort ein → du bekommst eine fertige `config.yaml` zum Kopieren.

**B) Per Kommandozeile** (findet auch die IPs automatisch):

```bash
# Ganzes Netz durchsuchen und Config gleich schreiben:
docker compose run --rm camera-nvr python -m app.autodetect \
    --user admin --pass DEIN_PASSWORT -o /config/config.yaml

# Oder gezielt einzelne IPs:
docker compose run --rm camera-nvr python -m app.autodetect \
    --host 192.168.1.50 --host 192.168.1.51 --user admin --pass DEIN_PASSWORT
```

Die Erkennung liefert automatisch: RTSP-Haupt- & Sub-Stream, ONVIF-Port,
PTZ-Fähigkeit und einen Vorschlag für Name/ID. Falls du das Passwort nicht
angibst, werden gängige Werks-Zugangsdaten durchprobiert.

### RTSP-URL manuell herausfinden

Falls die Auto-Erkennung mal nicht greift — jeder Hersteller nutzt ein eigenes
RTSP-Pfad-Schema. Häufige Beispiele:

| Hersteller (typisch) | Haupt-Stream | Sub-Stream |
|---|---|---|
| Hikvision-kompatibel | `rtsp://user:pass@IP:554/Streaming/Channels/101` | `.../102` |
| Dahua-kompatibel | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=0` | `subtype=1` |
| Generisch / China | `rtsp://user:pass@IP:554/live/ch0` | `/live/ch1` |

Wenn du den Pfad nicht kennst: Klick im Dashboard auf **„Kameras suchen"**
(ONVIF-Discovery) um die IPs zu finden, oder teste die URLs mit VLC
(*Medien → Netzwerkstream öffnen*).

> **Tipp:** Immer den **Sub-Stream** (niedrige Auflösung) für Live-Grid und
> Bewegungserkennung eintragen — das spart auf der Synology enorm viel CPU.

## Konfiguration

Alle Einstellungen in `config/config.yaml` — siehe ausführlich kommentierte
[`config.example.yaml`](config.example.yaml). Passwörter kommen über `${VAR}`
aus der `.env` (nicht im Klartext in der YAML).

### Bewegungserkennung feinjustieren

- `sensitivity_percent`: kleiner = empfindlicher (Standard 1.5).
- `region`: `[x, y, w, h]` in Prozent, um nur einen Bildbereich zu überwachen
  (z. B. nur die Einfahrt, nicht die Straße).

### Benachrichtigungen

- **Telegram:** `bot_token` (von @BotFather) und `chat_id` setzen, `enabled: true`.
- **Webhook:** beliebige URL, bekommt bei Bewegung ein JSON-POST.

## Zugriff absichern

Optionalen Login (Basic Auth) fürs Dashboard in `config.yaml` unter
`server.auth_user` / `auth_pass` setzen. Für Zugriff von außen empfiehlt sich
der **Synology Reverse Proxy** mit HTTPS statt Port-Weiterleitung.

## Hinweise zum Netzwerkmodus

`docker-compose.yml` nutzt `network_mode: host`, damit die **ONVIF-Gerätesuche**
(Multicast) funktioniert. In diesem Modus wird die `ports:`-Sektion ignoriert —
der Dienst hängt direkt auf Port `8080` des Hosts. Brauchst du die Auto-Suche
nicht, entferne `network_mode: host`; dann greift das normale Port-Mapping.

## Architektur (kurz)

```
Browser ──HTTP/MJPEG──▶ FastAPI (app/main.py)
                          │
                          ├─ CameraWorker (app/camera.py)  ── RTSP ──▶ Kamera
                          │     ├─ MotionDetector (motion.py)
                          │     └─ Alarm (notify.py)
                          └─ PTZController (onvif_ptz.py)   ── ONVIF ─▶ Kamera
```

Ein Hintergrund-Thread pro Kamera liest den Stream einmal und verteilt ihn an
alle Betrachter — die Kamera wird also nicht durch jeden Browser neu belastet.

## Lokal (ohne Docker) testen

```bash
pip install -r requirements.txt
export CAMERA_NVR_CONFIG=./config/config.yaml
uvicorn app.main:app --reload --port 8080
```
