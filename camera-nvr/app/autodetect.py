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
import ipaddress
import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

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


def _local_subnet() -> str | None:
    """Ermittelt das lokale /24-Subnetz (z.B. '192.168.1.0/24') anhand der
    eigenen LAN-IP. Es wird kein Paket gesendet, nur die Route bestimmt."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()
    try:
        return str(ipaddress.ip_network(ip + "/24", strict=False))
    except Exception:  # noqa: BLE001
        return None


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


def scan_subnet(subnet: str, ports: list[int] | None = None) -> list[tuple[str, int]]:
    """Scannt ein Subnetz nach offenen ONVIF-Ports. Fallback, wenn WS-Discovery
    (Multicast) im Netz blockiert ist. Gibt [(ip, offener_port), ...] zurueck."""
    ports = ports or COMMON_ONVIF_PORTS
    net = ipaddress.ip_network(subnet, strict=False)

    def check(ip_obj) -> tuple[str, int] | None:
        ip = str(ip_obj)
        for p in ports:
            if _port_open(ip, p):
                return (ip, p)
        return None

    hits: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=64) as ex:
        for res in ex.map(check, net.hosts()):
            if res:
                hits.append(res)
    return hits


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
    ap.add_argument("--subnet", help="Subnetz scannen statt Multicast, z.B. 192.168.1.0/24.")
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
    elif args.subnet:
        print(f"Scanne Subnetz {args.subnet} nach ONVIF-Ports ...", file=sys.stderr)
        targets = [(ip, port) for ip, port in scan_subnet(args.subnet)]
    else:
        print("Suche ONVIF-Kameras im Netz (WS-Discovery) ...", file=sys.stderr)
        for d in discover(timeout=args.discover_timeout):
            if d.get("address"):
                targets.append((d["address"], d.get("onvif_port")))
        # Fallback: wenn Multicast nichts liefert, lokales /24 scannen.
        if not targets:
            subnet = _local_subnet()
            if subnet:
                print(
                    f"WS-Discovery leer - scanne stattdessen {subnet} ...",
                    file=sys.stderr,
                )
                targets = [(ip, port) for ip, port in scan_subnet(subnet)]
        if not targets:
            print(
                "Keine Kameras gefunden. Gib IPs mit --host oder ein Subnetz "
                "mit --subnet 192.168.1.0/24 an.",
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
