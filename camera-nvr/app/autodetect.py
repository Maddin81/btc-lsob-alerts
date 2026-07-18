"""Automatische Kamera-Erkennung.

Findet ONVIF-Kameras im Netz, fragt sie per ONVIF nach ihren echten RTSP-URLs,
ONVIF-Ports, PTZ-Faehigkeit und Aufloesungen und schreibt eine fertige
config.yaml-Vorlage. So muss man RTSP-Pfade NICHT selbst raten.

Nutzung (auf der Synology / im lokalen Netz):

    # Ganzes Netz durchsuchen (WS-Discovery) und Zugangsdaten durchprobieren:
    python -m app.autodetect --user admin --pass geheim

    # Gezielt einzelne IPs:
    python -m app.autodetect --host 192.168.1.50 --host 192.168.1.51 \
        --user admin --pass geheim

    # Ergebnis direkt als Config speichern:
    python -m app.autodetect --user admin --pass geheim -o config/config.yaml
"""
from __future__ import annotations

import argparse
import re
import sys

from .onvif_ptz import COMMON_ONVIF_PORTS, discover, probe_onvif

# Haeufige Werks-Zugangsdaten billiger China-Kameras (User, Passwort).
DEFAULT_CREDENTIALS = [
    ("admin", ""),
    ("admin", "admin"),
    ("admin", "12345"),
    ("admin", "123456"),
    ("admin", "888888"),
    ("admin", "9999"),
    ("root", "root"),
]


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or fallback


def _try_probe(host: str, port_hint: int | None, creds: list[tuple[str, str]]) -> dict | None:
    """Probiert alle Zugangsdaten (und ggf. den entdeckten Port zuerst)."""
    ports = None
    if port_hint:
        ports = [port_hint] + [p for p in COMMON_ONVIF_PORTS if p != port_hint]
    for user, pw in creds:
        info = probe_onvif(host, user, pw, ports=ports)
        if info:
            info["username"] = user
            info["password"] = pw
            return info
    return None


def build_config_yaml(cameras: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Automatisch erzeugt von app.autodetect.")
    lines.append("# Passwoerter bei Bedarf durch ${VAR} ersetzen (siehe README).")
    lines.append("server:")
    lines.append('  host: "0.0.0.0"')
    lines.append("  port: 8080")
    lines.append('  auth_user: ""')
    lines.append('  auth_pass: ""')
    lines.append("storage:")
    lines.append('  events_dir: "/data/events"')
    lines.append("  retention_days: 14")
    lines.append("notify:")
    lines.append("  telegram:")
    lines.append("    enabled: false")
    lines.append('    bot_token: "${TELEGRAM_BOT_TOKEN}"')
    lines.append('    chat_id: "${TELEGRAM_CHAT_ID}"')
    lines.append("  webhook:")
    lines.append("    enabled: false")
    lines.append('    url: ""')
    lines.append("  cooldown_seconds: 60")
    lines.append("cameras:")

    used_ids: set[str] = set()
    for i, cam in enumerate(cameras, 1):
        base = _slug(cam.get("model", ""), f"cam{i}")
        cid = base
        n = 2
        while cid in used_ids:
            cid = f"{base}-{n}"
            n += 1
        used_ids.add(cid)

        name = " ".join(x for x in [cam.get("manufacturer", ""), cam.get("model", "")] if x) or cid
        lines.append(f'  - id: "{cid}"')
        lines.append(f'    name: "{name}"')
        lines.append(f'    host: "{cam["host"]}"')
        lines.append(f'    username: "{cam.get("username", "admin")}"')
        lines.append(f'    password: "{cam.get("password", "")}"')
        lines.append(f'    rtsp_main: "{cam.get("rtsp_main", "")}"')
        lines.append(f'    rtsp_sub: "{cam.get("rtsp_sub", "")}"')
        lines.append(f'    onvif_port: {cam.get("onvif_port", 80)}')
        lines.append(f'    ptz: {"true" if cam.get("ptz") else "false"}')
        lines.append("    motion:")
        lines.append("      enabled: true")
        lines.append("      sensitivity_percent: 1.5")
        lines.append("      region: []")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ONVIF-Kameras automatisch erkennen und konfigurieren.")
    ap.add_argument("--host", action="append", default=[], help="IP gezielt pruefen (mehrfach moeglich).")
    ap.add_argument("--user", action="append", default=[], help="ONVIF-Benutzer (mehrfach moeglich).")
    ap.add_argument("--pass", dest="password", action="append", default=[], help="ONVIF-Passwort (parallel zu --user).")
    ap.add_argument("--discover-timeout", type=int, default=5, help="WS-Discovery Timeout in Sekunden.")
    ap.add_argument("-o", "--output", help="Config-YAML in Datei schreiben statt auf stdout.")
    args = ap.parse_args(argv)

    # Zugangsdaten zusammenstellen: erst die angegebenen, dann Werks-Defaults.
    creds: list[tuple[str, str]] = []
    if args.user:
        for i, u in enumerate(args.user):
            pw = args.password[i] if i < len(args.password) else ""
            creds.append((u, pw))
    creds += DEFAULT_CREDENTIALS

    # Ziel-Hosts bestimmen.
    targets: list[tuple[str, int | None]] = []
    if args.host:
        targets = [(h, None) for h in args.host]
    else:
        print("Suche ONVIF-Kameras im Netz (WS-Discovery) ...", file=sys.stderr)
        for d in discover(timeout=args.discover_timeout):
            if d.get("address"):
                targets.append((d["address"], d.get("onvif_port")))
        if not targets:
            print(
                "Keine Kameras per Auto-Suche gefunden. Gib IPs mit --host an "
                "(Multicast wird von manchen Netzen/Containern blockiert).",
                file=sys.stderr,
            )
            return 1

    print(f"Pruefe {len(targets)} Geraet(e) ...", file=sys.stderr)
    cameras: list[dict] = []
    for host, port_hint in targets:
        info = _try_probe(host, port_hint, creds)
        if not info:
            print(f"  - {host}: keine ONVIF-Antwort / falsche Zugangsdaten", file=sys.stderr)
            continue
        print(
            f"  + {host}:{info['onvif_port']}  {info['manufacturer']} {info['model']}  "
            f"PTZ={info['ptz']}  main={'ja' if info['rtsp_main'] else 'nein'}",
            file=sys.stderr,
        )
        cameras.append(info)

    if not cameras:
        print("Keine Kamera erfolgreich abgefragt.", file=sys.stderr)
        return 1

    yaml_text = build_config_yaml(cameras)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(yaml_text)
        print(f"\n{len(cameras)} Kamera(s) erkannt -> {args.output} geschrieben.", file=sys.stderr)
    else:
        print(yaml_text)
        print(f"\n{len(cameras)} Kamera(s) erkannt.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
