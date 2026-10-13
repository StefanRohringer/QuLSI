"""Egress-Proxy: der einzige Weg aus QuLSI ins Internet.

Jeder Connector darf nur die Ziele erreichen, die in seinem Manifest stehen
(network.allowed_domains + allowed_ports, oder network.allowed als host:port).
Alles andere wird abgelehnt. Die Modelle (Zone 1) und der Core (Zone 2) kommen gar nicht raus.

Absicherung je Connector:
- Protokoll: jede Verbindung (erlaubt oder nicht) als JSON-Zeile in PROTOKOLL_DIR/egress.jsonl.
- Limits: Verbindungen pro Minute und Datenmenge pro Minute (Manifest "limits", sonst Standard).
  Zu viele Verbindungen werden abgelehnt, zu viele Daten gebremst.
- Quarantäne: Häufen sich Ablehnungen (unerlaubte Ziele, Limit überschritten), sperrt der Proxy
  den Connector komplett, bis du ihn in der App wieder freigibst. Der Core blendet seine Werkzeuge aus.
- Signatur: Passt ein Connector nicht zu connectors/SIGNATUR.json, bekommt er keinen Zugang.
- DNS-Prüfung: einmal am Tag, ob die erlaubten Domains noch auflösbar sind (Status für die App).

Wer fragt?
- Docker: über den Containernamen (Reverse-DNS im Docker-Netz), nicht fälschbar.
- Ein Container ohne Docker (RunPod): über den Benutzernamen im Proxy-Link
  (HTTPS_PROXY=http://calendar:x@127.0.0.1:3128). Das ist kooperativ, nicht fälschungssicher,
  und wird nur von 127.0.0.1 akzeptiert.

Unterstützt HTTP CONNECT (HTTPS, IMAP, SMTP) und einfache HTTP-Anfragen.
GET /__qulsi/internet sagt dem Core, ob es gerade Internet gibt (Offline-Modus).
GET /__qulsi/status liefert Quarantäne, Zähler und DNS-Prüfung; POST /__qulsi/freigeben/<name>
hebt eine Quarantäne auf (nur vom Core).
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

log = logging.getLogger("egress")
STANDARD_PORTS = {80, 443}
STANDARD_LIMITS = {"anfragen_pro_minute": 120, "mb_pro_minute": 50}
QUARANTAENE_AB = int(os.environ.get("QULSI_QUARANTAENE_AB", "10"))  # Ablehnungen in 10 Minuten


@dataclass
class Regel:
    host: str  # "calendar.google.com", "*.example.com" oder "*" (jede öffentliche Adresse)
    ports: set[int] = field(default_factory=lambda: set(STANDARD_PORTS))

    def erlaubt(self, host: str, port: int) -> bool:
        host = host.lower().rstrip(".")
        if port not in self.ports:
            return False
        if self.host == "*":
            return True  # nur öffentliche Adressen, das prüft Egress.behandeln
        if self.host.startswith("*."):
            basis = self.host[2:]
            return host == basis or host.endswith("." + basis)
        return host == self.host


def regeln_aus(eintrag: str, env: dict[str, str] | None = None) -> list[Regel]:
    """Ein Eintrag aus network.allowed. Umgebungsvariablen werden ersetzt; eine Variable darf
    mehrere Ziele enthalten (getrennt durch ; oder ,), auch als URL oder "Name=URL"."""
    env = os.environ if env is None else env
    text = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), str(eintrag))
    out = []
    for teil in filter(None, (t.strip() for t in re.split(r"[;,\s]+", text))):
        if "=" in teil.split("://")[0]:
            teil = teil.split("=", 1)[1]
        if "://" in teil:
            u = urlsplit(teil.replace("webcal://", "https://", 1))
            if not u.hostname:
                continue
            port = u.port or (80 if u.scheme == "http" else 443)
            out.append(Regel(u.hostname.lower(), {port}))
            continue
        host, _, port = teil.rpartition(":") if teil.count(":") == 1 else (teil, "", "")
        host = host.lower().strip("[]")
        if not host:
            continue
        if port and not port.isdigit():
            continue
        out.append(Regel(host, {int(port)} if port else set(STANDARD_PORTS)))
    return out


@dataclass
class Connector:
    name: str
    dienst: str
    regeln: list[Regel]
    limits: dict[str, float] = field(default_factory=lambda: dict(STANDARD_LIMITS))
    domains: list[str] = field(default_factory=list)  # für die DNS-Prüfung

    def erlaubt(self, host: str, port: int) -> bool:
        return any(r.erlaubt(host, port) for r in self.regeln)

    def nur_oeffentlich(self, host: str, port: int) -> bool:
        """Erlaubt nur über "*": dann darf das Ziel nicht im Heimnetz oder auf der Box liegen."""
        return not any(r.host != "*" and r.erlaubt(host, port) for r in self.regeln)


def connectoren_laden(verzeichnis: str, extra: str = "") -> dict[str, Connector]:
    out: dict[str, Connector] = {}
    for pfad in sorted(Path(verzeichnis).glob("*/manifest.yaml")):
        raw = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
        name = raw["name"]
        netz = raw.get("network") or {}
        regeln = [r for e in netz.get("allowed") or [] for r in regeln_aus(e)]
        ports = {int(p) for p in netz.get("allowed_ports") or [443]}
        for e in netz.get("allowed_domains") or []:
            for r in regeln_aus(e):
                r.ports = ports
                regeln.append(r)
        limits = {**STANDARD_LIMITS, **{k: float(v) for k, v in (raw.get("limits") or {}).items()}}
        domains = sorted({r.host for r in regeln if r.host != "*" and not r.host.startswith("*.")})
        out[name] = Connector(name, raw.get("dienst", f"connector-{name}"), regeln, limits, domains)
    # Der Tresor erneuert OAuth-Tokens: er darf nur zu den Token-Endpunkten aus den Manifesten.
    token_urls = [(yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("konto", {}).get("token_url")
                  for p in sorted(Path(verzeichnis).glob("*/manifest.yaml"))]
    out["tresor"] = Connector("tresor", "tresor", [r for u in token_urls if u for r in regeln_aus(u)])
    # Der Core schickt Web Push (core/qulsi_core/push.py) nur an die Push-Dienste der Handys.
    out["webpush"] = Connector("webpush", "core", [r for h in ("*.push.apple.com", "fcm.googleapis.com",
                               "*.push.services.mozilla.com", "*.notify.windows.com") for r in regeln_aus(f"{h}:443")])
    # Zusätzliche Ziele ohne Manifest-Änderung, z. B. für Tests: "calendar=fake-ha:8123,mail=..."
    for teil in filter(None, (t.strip() for t in extra.split(","))):
        name, _, ziel = teil.partition("=")
        if name in out:
            out[name].regeln += regeln_aus(ziel)
    return out


class Egress:
    def __init__(self, connectoren: dict[str, Connector], probe: list[tuple[str, int]],
                 protokoll_dir: str | None = None, gesperrt: dict[str, str] | None = None):
        self.connectoren = connectoren
        self.probe = probe
        self._internet: tuple[float, bool] = (0.0, False)
        self.protokoll_dir = Path(protokoll_dir) if protokoll_dir else None
        self.gesperrt: dict[str, str] = dict(gesperrt or {})  # Signatur passt nicht: kein Zugang
        self.quarantaene: dict[str, dict] = self._quarantaene_laden()
        self._anfragen: dict[str, deque] = {}
        self._ablehnungen: dict[str, deque] = {}
        self._bytes: dict[str, list[float]] = {}  # [Minutenbeginn, Bytes]
        self.dns: dict[str, list[str]] = {}  # Connector -> nicht auflösbare Domains
        self.dns_stand: float | None = None
        self.eigene = eigene_hosts()  # QuLSI selbst (PUBLIC_URL, ntfy, RunPod-Proxy): für alle gesperrt

    # ------------------------------------------------------------ Protokoll und Quarantäne

    def protokoll(self, **eintrag) -> None:
        eintrag = {"zeit": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **eintrag}
        log.info("%s connector=%s ziel=%s:%s %s", eintrag.get("ergebnis", "").upper(), eintrag.get("connector"),
                 eintrag.get("ziel"), eintrag.get("port"), json.dumps(eintrag, ensure_ascii=False))
        if not self.protokoll_dir:
            return
        try:
            datei = self.protokoll_dir / "egress.jsonl"
            if datei.exists() and datei.stat().st_size > 20_000_000:
                datei.replace(datei.with_suffix(".jsonl.1"))
            with datei.open("a", encoding="utf-8") as f:
                f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Protokoll nicht schreibbar: %s", exc)

    def _quarantaene_datei(self) -> Path | None:
        return self.protokoll_dir / "quarantaene.json" if self.protokoll_dir else None

    def _quarantaene_laden(self) -> dict[str, dict]:
        d = self._quarantaene_datei()
        try:
            return json.loads(d.read_text(encoding="utf-8")) if d else {}
        except (OSError, ValueError):
            return {}

    def _quarantaene_speichern(self) -> None:
        d = self._quarantaene_datei()
        if d:
            try:
                d.write_text(json.dumps(self.quarantaene, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                log.warning("Quarantäne nicht speicherbar: %s", exc)

    def ablehnung(self, connector: str | None, jetzt: float | None = None) -> None:
        """Zählt eine Ablehnung. Zu viele in 10 Minuten: Quarantäne."""
        if not connector or connector not in self.connectoren or connector in self.quarantaene:
            return
        jetzt = time.monotonic() if jetzt is None else jetzt
        q = self._ablehnungen.setdefault(connector, deque())
        q.append(jetzt)
        while q and q[0] < jetzt - 600:
            q.popleft()
        if len(q) >= QUARANTAENE_AB:
            self.quarantaene[connector] = {"seit": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                           "grund": f"{len(q)} abgelehnte Verbindungen in 10 Minuten"}
            q.clear()
            self._quarantaene_speichern()
            log.error("QUARANTAENE connector=%s: %s", connector, self.quarantaene[connector]["grund"])

    def freigeben(self, connector: str) -> bool:
        if self.quarantaene.pop(connector, None) is None:
            return False
        self._quarantaene_speichern()
        log.warning("Quarantäne aufgehoben: %s", connector)
        return True

    def anfrage_zaehlen(self, connector: str, jetzt: float | None = None) -> bool:
        """False, wenn das Limit "anfragen_pro_minute" erreicht ist."""
        jetzt = time.monotonic() if jetzt is None else jetzt
        q = self._anfragen.setdefault(connector, deque())
        while q and q[0] <= jetzt - 60:
            q.popleft()
        if len(q) >= self.connectoren[connector].limits["anfragen_pro_minute"]:
            return False
        q.append(jetzt)
        return True

    async def bremsen(self, connector: str, anzahl: int) -> None:
        """Datenmenge pro Minute begrenzen: über dem Limit bis zur nächsten Minute warten."""
        jetzt = time.monotonic()
        stand = self._bytes.setdefault(connector, [jetzt, 0.0])
        if jetzt - stand[0] >= 60:
            stand[0], stand[1] = jetzt, 0.0
        stand[1] += anzahl
        if stand[1] > self.connectoren[connector].limits["mb_pro_minute"] * 1_000_000:
            await asyncio.sleep(max(0.0, 60 - (jetzt - stand[0])))

    async def dns_pruefen(self) -> None:
        """Sind die erlaubten Domains noch auflösbar? Ergebnis im Status, Fehler im Log."""
        loop = asyncio.get_running_loop()
        ergebnis = {}
        for c in self.connectoren.values():
            fehlt = []
            for d in c.domains:
                try:
                    await asyncio.wait_for(loop.getaddrinfo(d, 443), 10)
                except Exception:  # noqa: BLE001
                    fehlt.append(d)
            if fehlt:
                ergebnis[c.name] = fehlt
                log.warning("DNS-Pruefung: %s nicht aufloesbar: %s", c.name, ", ".join(fehlt))
        self.dns, self.dns_stand = ergebnis, time.time()

    def status(self) -> dict:
        return {
            "quarantaene": self.quarantaene,
            "gesperrt": self.gesperrt,
            "dns_nicht_aufloesbar": self.dns,
            "dns_geprueft": self.dns_stand,
            "anfragen_letzte_minute": {n: len(q) for n, q in self._anfragen.items()},
        }

    # ------------------------------------------------------------ Wer fragt?

    async def wer(self, ip: str, kopf: dict[str, str]) -> str | None:
        if ipaddress.ip_address(ip).is_loopback:
            auth = kopf.get("proxy-authorization", "")
            if auth.lower().startswith("basic "):
                try:
                    return base64.b64decode(auth[6:]).decode().split(":", 1)[0] or None
                except Exception:  # noqa: BLE001
                    return None
            return None
        try:
            name = (await asyncio.to_thread(socket.gethostbyaddr, ip))[0]
        except OSError:
            return None
        kurz = name.split(".")[0].lower()
        for c in self.connectoren.values():
            if re.fullmatch(rf"(.+[-_])?{re.escape(c.dienst.lower())}([-_]\d+)?", kurz):
                return c.name
        return None

    # ------------------------------------------------------------ Internet?

    async def internet(self) -> bool:
        stand, wert = self._internet
        if time.monotonic() - stand < 20:
            return wert
        wert = False
        for host, port in self.probe:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
                w.close()
                wert = True
                break
            except Exception:  # noqa: BLE001
                continue
        self._internet = (time.monotonic(), wert)
        return wert

    # ------------------------------------------------------------ Verbindungen

    async def behandeln(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = writer.get_extra_info("peername")[0]
        try:
            kopf_roh = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        except Exception:  # noqa: BLE001
            writer.close()
            return
        zeilen = kopf_roh.decode("latin-1").split("\r\n")
        try:
            methode, ziel, version = zeilen[0].split(" ", 2)
        except ValueError:
            await self._antwort(writer, 400, "Ungueltige Anfrage")
            return
        kopf = {k.strip().lower(): v.strip() for k, _, v in (z.partition(":") for z in zeilen[1:] if z)}

        if methode == "GET" and ziel.startswith("/__qulsi/internet"):
            await self._antwort(writer, 200, json.dumps({"internet": await self.internet()}), "application/json")
            return
        if methode == "GET" and ziel.startswith("/__qulsi/status"):
            await self._antwort(writer, 200, json.dumps(self.status(), ensure_ascii=False), "application/json")
            return
        if methode == "POST" and ziel.startswith("/__qulsi/freigeben/"):
            if not await self.ist_core(ip):
                await self._antwort(writer, 403, "Nur der Core darf freigeben")
                return
            ok = self.freigeben(ziel.rsplit("/", 1)[1])
            await self._antwort(writer, 200, json.dumps({"freigegeben": ok}), "application/json")
            return

        if methode == "CONNECT":
            host, _, port = ziel.rpartition(":")
            host, port_i = host.strip("[]"), int(port or 443)
        else:
            u = urlsplit(ziel)
            if not u.hostname:
                await self._antwort(writer, 400, "Nur Proxy-Anfragen")
                return
            host, port_i = u.hostname, u.port or 80

        connector = await self.wer(ip, kopf)
        c = self.connectoren.get(connector or "")
        eintrag = {"connector": connector or "unbekannt", "ip": ip, "ziel": host, "port": port_i, "methode": methode}

        async def ablehnen(status: int, grund: str, zaehlt: bool = True) -> None:
            self.protokoll(**eintrag, ergebnis="verweigert", grund=grund)
            if zaehlt:
                self.ablehnung(connector)
            await self._antwort(writer, status, f"QuLSI-Egress: {grund}")

        if c is None:
            await ablehnen(403, f"{host}:{port_i} ist fuer diesen Absender nicht freigegeben", zaehlt=False)
            return
        if c.name in self.gesperrt:
            await ablehnen(403, f"{c.name} ist gesperrt: {self.gesperrt[c.name]}", zaehlt=False)
            return
        if c.name in self.quarantaene:
            await ablehnen(403, f"{c.name} ist in Quarantaene ({self.quarantaene[c.name]['grund']})", zaehlt=False)
            return
        if ist_eigener_host(host, self.eigene):
            await ablehnen(403, f"{host} ist QuLSI selbst, dahin darf kein Connector")
            return
        if not c.erlaubt(host, port_i):
            await ablehnen(403, f"{host}:{port_i} ist fuer {c.name} nicht freigegeben")
            return
        if not self.anfrage_zaehlen(c.name):
            await ablehnen(429, f"{c.name}: zu viele Verbindungen pro Minute")
            return
        adresse = host
        if c.nur_oeffentlich(host, port_i):
            adresse = await oeffentliche_adresse(host, port_i)
            if adresse is None:
                await ablehnen(403, f"{host} liegt nicht im oeffentlichen Internet")
                return
        try:
            # mit der geprüften IP verbinden, damit ein zweiter DNS-Abruf nicht doch ins Heimnetz zeigt
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(adresse, port_i), 15)
        except Exception as exc:  # noqa: BLE001
            self.protokoll(**eintrag, ergebnis="nicht_erreichbar", grund=str(exc))
            await self._antwort(writer, 502, f"Ziel nicht erreichbar: {exc}")
            return
        start = time.monotonic()

        if methode == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            u = urlsplit(ziel)
            pfad = (u.path or "/") + (f"?{u.query}" if u.query else "")
            neu = [f"{methode} {pfad} {version}"]
            for z in zeilen[1:]:
                k = z.partition(":")[0].strip().lower()
                if z and not k.startswith("proxy-") and k != "connection":
                    neu.append(z)
            neu.append("Connection: close")
            up_w.write(("\r\n".join(neu) + "\r\n\r\n").encode("latin-1"))
            await up_w.drain()
        menge = [0, 0]  # hoch, runter
        await asyncio.gather(_pumpe(reader, up_w, self, c.name, menge, 0), _pumpe(up_r, writer, self, c.name, menge, 1))
        self.protokoll(**eintrag, ergebnis="erlaubt", bytes_hoch=menge[0], bytes_runter=menge[1],
                       sekunden=round(time.monotonic() - start, 2))

    async def ist_core(self, ip: str) -> bool:
        if ipaddress.ip_address(ip).is_loopback:
            return True
        try:
            name = (await asyncio.to_thread(socket.gethostbyaddr, ip))[0]
        except OSError:
            return False
        return re.fullmatch(r"(.+[-_])?core([-_]\d+)?", name.split(".")[0].lower()) is not None

    @staticmethod
    async def _antwort(writer: asyncio.StreamWriter, status: int, text: str, typ: str = "text/plain") -> None:
        daten = text.encode()
        grund = {200: "OK", 400: "Bad Request", 403: "Forbidden", 429: "Too Many Requests", 502: "Bad Gateway"}[status]
        writer.write(f"HTTP/1.1 {status} {grund}\r\nContent-Type: {typ}; charset=utf-8\r\n"
                     f"Content-Length: {len(daten)}\r\nConnection: close\r\n\r\n".encode() + daten)
        try:
            await writer.drain()
        finally:
            writer.close()


def eigene_hosts(env: dict[str, str] | None = None) -> list[str]:
    """Adressen, unter denen QuLSI selbst erreichbar ist. Dahin darf kein Connector: sonst könnte ein
    Browser- oder Web-Werkzeug die eigene App und ihre API von außen abklopfen (Fall: RunPod-Proxy-Adresse)."""
    env = os.environ if env is None else env
    hosts = []
    for name in ("PUBLIC_URL", "NTFY_PUBLIC_URL"):
        if h := urlsplit(env.get(name, "") or "").hostname:
            hosts.append(h.lower())
    hosts += [h.strip().lower() for h in env.get("QULSI_EIGENE_HOSTS", "").split(",") if h.strip()]
    if pod := env.get("RUNPOD_POD_ID", "").strip().lower():  # jeder Port des Pods: <pod>-<port>.proxy.runpod.net
        hosts.append(f"{pod}-*.proxy.runpod.net")
    return [h for h in hosts if h not in ("localhost", "qulsi.local")]


def ist_eigener_host(host: str, eigene: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(fnmatch.fnmatchcase(host, h) for h in eigene)


async def oeffentliche_adresse(host: str, port: int) -> str | None:
    """Erste IP des Ziels, aber nur, wenn alle seine IPs öffentlich sind (kein Router, NAS, Docker, localhost)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    ips = [info[4][0] for info in infos]
    if not ips or not all(ipaddress.ip_address(i.split("%")[0]).is_global for i in ips):
        return None
    return ips[0]


async def _pumpe(r: asyncio.StreamReader, w: asyncio.StreamWriter, egress: "Egress | None" = None,
                 connector: str = "", menge: list[int] | None = None, richtung: int = 0) -> None:
    try:
        while daten := await r.read(65536):
            if egress is not None:
                menge[richtung] += len(daten)
                await egress.bremsen(connector, len(daten))
            w.write(daten)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


def probe_ziele(text: str) -> list[tuple[str, int]]:
    out = []
    for t in filter(None, (x.strip() for x in text.split(","))):
        host, _, port = t.rpartition(":")
        out.append((host, int(port)))
    return out


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    verzeichnis = os.environ.get("CONNECTORS_DIR", "/connectors")
    connectoren = connectoren_laden(verzeichnis, os.environ.get("QULSI_EGRESS_EXTRA", ""))
    for c in connectoren.values():
        ziele = ", ".join(f"{r.host}:{'/'.join(map(str, sorted(r.ports)))}" for r in c.regeln) or "keine"
        log.info("Connector %s (%s) darf: %s", c.name, c.dienst, ziele)
    gesperrt = signatur_pruefen(verzeichnis, connectoren)
    egress = Egress(connectoren, probe_ziele(os.environ.get("QULSI_PROBE", "1.1.1.1:443,9.9.9.9:443")),
                    os.environ.get("PROTOKOLL_DIR") or None, gesperrt)
    server = await asyncio.start_server(egress.behandeln, os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "3128")))

    async def dns_schleife() -> None:
        await asyncio.sleep(60)
        while True:
            await egress.dns_pruefen()
            await asyncio.sleep(24 * 3600)

    aufgabe = asyncio.create_task(dns_schleife())
    async with server:
        await server.serve_forever()
    aufgabe.cancel()


def signatur_pruefen(verzeichnis: str, connectoren: dict[str, Connector]) -> dict[str, str]:
    """Connectoren, deren Dateien nicht zur Signatur passen, bekommen keinen Internetzugang."""
    from qulsi_integritaet import pruefen

    fehler, alle = pruefen(verzeichnis, os.environ.get("QULSI_SIGNATUR_PUB", ""))
    ordner = {pfad.parent.name: yaml.safe_load(pfad.read_text(encoding="utf-8")).get("name")
              for pfad in Path(verzeichnis).glob("*/manifest.yaml")}
    if alle:
        log.error("SIGNATUR: %s. Kein Connector bekommt Internetzugang.", alle)
        return {n: alle for n in connectoren}
    gesperrt = {ordner.get(d, d): grund for d, grund in fehler.items()}
    for n, grund in gesperrt.items():
        log.error("SIGNATUR: %s gesperrt (%s)", n, grund)
    return gesperrt


if __name__ == "__main__":
    asyncio.run(main())
