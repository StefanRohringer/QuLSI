"""Gemeinsame Helfer für alle QuLSI-Connectoren.

Ein Connector ist ein MCP-Server mit Manifest (manifest.yaml im selben Ordner).
Ins Internet kommt er nur über den Egress-Proxy (HTTPS_PROXY), der nur die Ziele
aus dem Manifest durchlässt. httpx nutzt den Proxy automatisch, für IMAP/SMTP
gibt es tunnel().
"""

from __future__ import annotations

import base64
import os
import socket
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx
import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))


async def db() -> psycopg.AsyncConnection:
    """Eigene Datenbank-Rolle des Connectors (nur seine Tabellen, siehe db/init/02_rollen.sh)."""
    return await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


def zeit(ts: datetime | None) -> str | None:
    return ts.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if ts else None


def minuten_her(ts: datetime) -> int:
    return int((datetime.now(timezone.utc) - ts).total_seconds() // 60)


def internet(**kw) -> httpx.AsyncClient:
    """HTTP-Client für Ziele außerhalb von QuLSI: geht über den Egress-Proxy."""
    kw.setdefault("timeout", 30)
    return httpx.AsyncClient(trust_env=True, **kw)


def intern(**kw) -> httpx.AsyncClient:
    """HTTP-Client für Dienste innerhalb von QuLSI (LiteLLM, Vision, Qdrant): ohne Proxy."""
    kw.setdefault("timeout", 60)
    return httpx.AsyncClient(trust_env=False, **kw)


def _tresor_kopf() -> dict[str, str]:
    # Im Docker-Netz erkennt der Tresor den Connector am Containernamen; ohne Docker (RunPod) zählt dieser Kopf
    return {"X-QuLSI-Connector": os.environ.get("CONNECTOR_NAME", "")}


def geheimnis(name: str, standard: str = "") -> str:
    """Passwort oder API-Schlüssel aus dem Tresor (nur was im Manifest unter "secrets" steht).

    Ohne Tresor (TRESOR_URL leer, z. B. RunPod) aus der Umgebungsvariable gleichen Namens."""
    url = os.environ.get("TRESOR_URL", "")
    if not url:
        return os.environ.get(name, standard)
    import time

    for versuch in range(10):  # der Tresor startet evtl. gleichzeitig
        try:
            r = httpx.get(f"{url}/geheimnis/{name}", headers=_tresor_kopf(), timeout=5, trust_env=False)
        except httpx.HTTPError:
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r.json()["wert"]
        if r.status_code == 404:
            return standard
        raise PermissionError(f"Tresor verweigert {name}: {r.text}")
    return standard


_frisch: dict[str, tuple[float, str]] = {}


def geheimnis_aktuell(name: str, standard: str = "", sekunden: float = 60) -> str:
    """Wie geheimnis(), aber höchstens `sekunden` zwischengespeichert. So gilt ein Passwort, das in der
    App geändert wurde, ohne Neustart des Connectors. Ist der Tresor kurz weg, bleibt der letzte Wert."""
    import time

    jetzt = time.monotonic()
    alt = _frisch.get(name)
    if alt and jetzt - alt[0] < sekunden:
        return alt[1]
    url = os.environ.get("TRESOR_URL", "")
    if not url:
        return os.environ.get(name, standard)
    try:
        r = httpx.get(f"{url}/geheimnis/{name}", headers=_tresor_kopf(), timeout=5, trust_env=False)
    except httpx.HTTPError:
        return alt[1] if alt else standard
    if r.status_code == 200:
        wert = r.json()["wert"]
    elif r.status_code == 404:
        wert = standard
    else:
        raise PermissionError(f"Tresor verweigert {name}: {r.text}")
    _frisch[name] = (jetzt, wert)
    return wert


def tunnel(host: str, port: int, timeout: float = 30) -> socket.socket:
    """TCP-Verbindung zu host:port, über den Egress-Proxy per HTTP CONNECT, falls gesetzt."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)
    p = urlsplit(proxy)
    sock = socket.create_connection((p.hostname, p.port or 3128), timeout=timeout)
    kopf = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if p.username:
        cred = base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
        kopf += f"Proxy-Authorization: Basic {cred}\r\n"
    sock.sendall((kopf + "\r\n").encode())
    antwort = b""
    while b"\r\n\r\n" not in antwort:
        teil = sock.recv(4096)
        if not teil:
            break
        antwort += teil
    status = antwort.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200 " not in status + " ":
        sock.close()
        raise ConnectionError(f"Egress-Proxy lässt {host}:{port} nicht durch ({status})")
    return sock


def run(server: MCPServer) -> None:
    """Startet den Server per Streamable HTTP. Nur für den Core erreichbar, nie nach außen veröffentlichen."""
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(hosts), allowed_hosts=hosts)
    server.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        transport_security=security,
    )


def objekt_ablegen(daten: bytes, endung: str = "") -> str:
    """Legt eine Datei im Object Storage ab (inhaltsadressiert, wie im Core)."""
    import hashlib
    from pathlib import Path

    h = hashlib.sha256(daten).hexdigest()
    key = f"{h[:2]}/{h}{endung}"
    ziel = Path(os.environ.get("OBJECT_STORE_DIR", "/data/objects")) / key
    if not ziel.exists():
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(ziel.suffix + ".tmp")
        tmp.write_bytes(daten)
        tmp.replace(ziel)
    return key


def im_hintergrund(coro_fn, minuten: float) -> None:
    """Startet eine Schleife (z. B. Kalender-Sync) in einem eigenen Thread neben dem MCP-Server."""
    import asyncio
    import logging
    import threading

    async def schleife():
        while True:
            pause = minuten * 60
            try:
                await coro_fn()
            except Exception as exc:  # noqa: BLE001
                # z. B. LiteLLM startet noch: bald nochmal versuchen statt erst nach dem Intervall
                logging.getLogger(coro_fn.__module__).warning("Hintergrundaufgabe fehlgeschlagen: %r", exc)
                pause = min(pause, 30)
            await asyncio.sleep(pause)

    threading.Thread(target=lambda: asyncio.run(schleife()), daemon=True).start()
