"""Web Push: Hinweise und Freigaben direkt aus der QuLSI-App aufs Handy (und weiter auf die Uhr).

- Standard-Web-Push (RFC 8030, Verschlüsselung RFC 8291, VAPID RFC 8292), keine fremde App nötig.
  iPhone ab iOS 16.4, wenn QuLSI auf dem Home-Bildschirm liegt; Android und Desktop-Browser auch.
  Eine gekoppelte Apple Watch oder Wear-OS-Uhr zeigt die Mitteilungen des Handys von selbst.
- Der VAPID-Schlüssel entsteht beim ersten Start und liegt in Postgres (system_state "webpush_vapid").
  Er erscheint nie im Protokoll oder in einer Antwort, nur der öffentliche Teil.
- Abos (je Gerät) stehen in push_abos. Gesendet wird nur an Apple, Google, Mozilla und Microsoft
  (PUSH_DIENSTE), auf der Box über den Egress-Proxy.
- Sperrbildschirm: standardmäßig nur der Titel ("Gleich ein Termin"), keine Inhalte. Details pro
  Gerät einschalten geht nur mit der Freigabe-PIN, ausschalten immer.
- Freigaben kommen als Hinweis, entschieden wird weiter in der App mit PIN.
- Ruhezeit und Stundenlimit der Hintergrund-Agents gelten vorher (proaktiv/laeufer.py). Hier gibt es
  zusätzlich eine harte Grenze pro Stunde gegen Schleifen.

Benachrichtiger ersetzt in main.py den ntfy-Notifier: erst Web Push, dann ntfy wie bisher (ntfy tut
ohne NTFY_URL nichts).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from collections import deque
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger(__name__)

# Nur an diese Push-Dienste wird gesendet. Sonst könnte ein angemeldetes "Gerät" den Core
# beliebige Adressen aufrufen lassen. Gleiche Liste im Egress-Proxy (Eintrag "webpush").
PUSH_DIENSTE = {
    "push.apple.com": "Apple",
    "fcm.googleapis.com": "Google",
    "push.services.mozilla.com": "Mozilla",
    "notify.windows.com": "Microsoft",
}
MAX_PRO_STUNDE = 30
MAX_GERAETE = 10
STATE_KEY = "webpush_vapid"

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_abos (
    id          TEXT PRIMARY KEY,                -- sha256(endpoint)[:16], für die App
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    geraet      TEXT NOT NULL DEFAULT '',
    details     BOOLEAN NOT NULL DEFAULT false,  -- Inhalte auf dem Sperrbildschirm
    erstellt    TIMESTAMPTZ NOT NULL DEFAULT now(),
    zuletzt_ok  TIMESTAMPTZ,
    fehler      INT NOT NULL DEFAULT 0
);
"""


# ---------------------------------------------------------------------------- Kryptografie


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(schluessel: bytes, daten: bytes) -> bytes:
    return hmac.new(schluessel, daten, hashlib.sha256).digest()


def _oeffentlich(privat: ec.EllipticCurvePrivateKey) -> bytes:
    return privat.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def verschluesseln(nutzlast: bytes, ua_public: bytes, auth_secret: bytes, *, salt: bytes | None = None,
                   as_privat: ec.EllipticCurvePrivateKey | None = None, rs: int = 4096) -> bytes:
    """RFC 8291 (aes128gcm, ein Datensatz). salt und as_privat nur für Tests vorgeben."""
    salt = salt or os.urandom(16)
    as_privat = as_privat or ec.generate_private_key(ec.SECP256R1())
    as_public = _oeffentlich(as_privat)
    ua = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = as_privat.exchange(ec.ECDH(), ua)
    prk_key = _hmac(auth_secret, ecdh)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(nutzlast) + 17 > rs:
        raise ValueError("Nachricht zu lang für einen Datensatz")
    chiffre = AESGCM(cek).encrypt(nonce, nutzlast + b"\x02", None)
    return salt + struct.pack("!IB", rs, len(as_public)) + as_public + chiffre


def vapid_jwt(privat: ec.EllipticCurvePrivateKey, aud: str, sub: str, gueltig: int = 3600) -> str:
    kopf = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    inhalt = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + gueltig, "sub": sub},
                             separators=(",", ":")).encode())
    r, s = decode_dss_signature(privat.sign(f"{kopf}.{inhalt}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{kopf}.{inhalt}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def dienst(endpoint: str) -> str | None:
    """Name des Push-Dienstes, wenn die Adresse erlaubt ist (https, bekannter Anbieter), sonst None."""
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return None
    if u.scheme != "https" or not u.hostname or u.port not in (None, 443) or u.username or u.password:
        return None
    host = u.hostname.lower()
    for basis, name in PUSH_DIENSTE.items():
        if host == basis or host.endswith("." + basis):
            return name
    return None


def abo_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def proxy_url(egress_url: str) -> str | None:
    """Auf der Box erkennt der Egress-Proxy den Core am Containernamen, auf RunPod (127.0.0.1) am
    Benutzernamen im Proxy-Link."""
    if not egress_url:
        return None
    u = urlsplit(egress_url)
    try:
        lokal = ip_address(u.hostname or "").is_loopback
    except ValueError:
        lokal = u.hostname == "localhost"
    if lokal and not u.username:
        return urlunsplit((u.scheme, f"webpush:x@{u.netloc}", u.path, "", ""))
    return egress_url


# ---------------------------------------------------------------------------- Versand


class WebPush:
    def __init__(self, store, egress_url: str = "", kontakt: str = ""):
        self.store = store
        self.proxy = proxy_url(egress_url)
        # Apple verlangt eine gültige Kontaktadresse im VAPID-Token.
        self.kontakt = kontakt or os.environ.get("QULSI_PUSH_KONTAKT") or "mailto:qulsi@example.com"
        self._privat: ec.EllipticCurvePrivateKey | None = None
        self._gesendet: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.gesendet: list[dict] = []  # für Tests und Fehlersuche (nur Titel und Ziel-ID)
        self.transport: httpx.AsyncBaseTransport | None = None  # nur Tests

    async def vorbereiten(self) -> None:
        async with await self.store.conn() as c:
            await c.execute(SCHEMA)
        await self.schluessel()

    async def schluessel(self) -> ec.EllipticCurvePrivateKey:
        async with self._lock:
            if self._privat:
                return self._privat
            row = await self.store.eine("SELECT value FROM system_state WHERE key = %s", (STATE_KEY,))
            wert = ((row or {}).get("value") or {}).get("privat")
            if wert:
                self._privat = ec.derive_private_key(int.from_bytes(unb64u(wert), "big"), ec.SECP256R1())
            else:
                privat = ec.generate_private_key(ec.SECP256R1())
                roh = privat.private_numbers().private_value.to_bytes(32, "big")
                async with await self.store.conn() as c:
                    # DO NOTHING: starten zwei Prozesse gleichzeitig, gewinnt der erste.
                    await c.execute("""INSERT INTO system_state (key, value) VALUES (%s, %s::jsonb)
                                       ON CONFLICT (key) DO NOTHING""", (STATE_KEY, json.dumps({"privat": b64u(roh)})))
                row = await self.store.eine("SELECT value FROM system_state WHERE key = %s", (STATE_KEY,))
                self._privat = ec.derive_private_key(int.from_bytes(unb64u(row["value"]["privat"]), "big"),
                                                     ec.SECP256R1())
                log.info("Web Push: neuer VAPID-Schlüssel angelegt")
            return self._privat

    async def oeffentlich(self) -> str:
        return b64u(_oeffentlich(await self.schluessel()))

    # ------------------------------------------------------------ Abos

    async def geraete(self) -> list[dict]:
        rows = await self.store.alle("SELECT id, endpoint, geraet, details, erstellt, zuletzt_ok, fehler "
                                     "FROM push_abos ORDER BY erstellt")
        return [{"id": r["id"], "geraet": r["geraet"], "dienst": dienst(r["endpoint"]), "details": r["details"],
                 "erstellt": r["erstellt"].isoformat(), "zuletzt_ok": r["zuletzt_ok"].isoformat() if r["zuletzt_ok"] else None,
                 "fehler": r["fehler"]} for r in rows]

    async def anmelden(self, abo: dict, geraet: str = "") -> tuple[str, bool]:
        """Speichert ein Abo aus PushManager.subscribe(). Gibt (id, neu) zurück."""
        endpoint = str(abo.get("endpoint") or "")
        keys = abo.get("keys") or {}
        if len(endpoint) > 1000 or not dienst(endpoint):
            raise HTTPException(400, "Unbekannter Push-Dienst")
        try:
            p256dh, auth = unb64u(str(keys.get("p256dh") or "")), unb64u(str(keys.get("auth") or ""))
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Ungültige Schlüssel im Abo") from None
        if len(auth) != 16:
            raise HTTPException(400, "Ungültige Schlüssel im Abo")
        aid = abo_id(endpoint)
        geraet = " ".join(str(geraet or "").split())[:60]
        alt = await self.store.eine("SELECT id FROM push_abos WHERE id = %s", (aid,))
        if not alt:
            n = await self.store.eine("SELECT count(*) AS n FROM push_abos")
            if n and n["n"] >= MAX_GERAETE:
                raise HTTPException(409, f"Schon {MAX_GERAETE} Geräte angemeldet. Entferne erst eins.")
        async with await self.store.conn() as c:
            await c.execute(
                """INSERT INTO push_abos (id, endpoint, p256dh, auth, geraet) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,
                       geraet = CASE WHEN EXCLUDED.geraet <> '' THEN EXCLUDED.geraet ELSE push_abos.geraet END,
                       fehler = 0""",
                (aid, endpoint, b64u(p256dh), b64u(auth), geraet))
        return aid, not alt

    async def abmelden(self, aid: str) -> bool:
        row = await self.store.eine("DELETE FROM push_abos WHERE id = %s RETURNING id", (aid,))
        return row is not None

    async def details_setzen(self, aid: str, an: bool) -> bool:
        row = await self.store.eine("UPDATE push_abos SET details = %s WHERE id = %s RETURNING id", (an, aid))
        return row is not None

    # ------------------------------------------------------------ Senden

    def _grenze(self) -> bool:
        jetzt = time.monotonic()
        while self._gesendet and jetzt - self._gesendet[0] > 3600:
            self._gesendet.popleft()
        if len(self._gesendet) >= MAX_PRO_STUNDE:
            return False
        self._gesendet.append(jetzt)
        return True

    async def senden(self, titel: str, privat: str, details: str = "", *, url: str = "/", tag: str = "",
                     dringend: bool = False, ttl: int = 3600, nur: str | None = None, ausser: str | None = None,
                     test: bool = False) -> dict:
        """Schickt an alle Geräte (oder nur eins). privat = Text ohne Inhalte für den Sperrbildschirm,
        details = voller Text für Geräte mit Details an. Wirft nie, gibt Zähler zurück."""
        if not test and not self._grenze():
            log.warning("Web Push: Grenze von %s pro Stunde erreicht, nur in der Timeline", MAX_PRO_STUNDE)
            return {"gesendet": 0, "fehler": 0, "grenze": True}
        rows = await self.store.alle("SELECT * FROM push_abos")
        rows = [r for r in rows if (nur is None or r["id"] == nur) and r["id"] != ausser]
        if not rows:
            return {"gesendet": 0, "fehler": 0}
        privat_key = await self.schluessel()
        k = b64u(_oeffentlich(privat_key))
        ergebnisse = await asyncio.gather(*(self._an_geraet(r, privat_key, k, {
            "titel": titel[:120],
            "text": (details if r["details"] and details else privat)[:1500],
            "url": url if url.startswith("/") else "/",
            "tag": tag[:64],
        }, dringend, ttl) for r in rows))
        return {"gesendet": sum(ergebnisse), "fehler": len(ergebnisse) - sum(ergebnisse)}

    async def _an_geraet(self, r: dict, privat_key, k: str, nutzlast: dict, dringend: bool, ttl: int) -> bool:
        endpoint = r["endpoint"]
        if not dienst(endpoint):
            return False
        u = urlsplit(endpoint)
        try:
            koerper = verschluesseln(json.dumps(nutzlast, ensure_ascii=False).encode(), unb64u(r["p256dh"]),
                                     unb64u(r["auth"]))
            kopf = {
                "Authorization": f"vapid t={vapid_jwt(privat_key, f'{u.scheme}://{u.netloc}', self.kontakt)}, k={k}",
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(ttl),
                "Urgency": "high" if dringend else "normal",
            }
            if nutzlast["tag"]:
                kopf["Topic"] = hashlib.sha256(nutzlast["tag"].encode()).hexdigest()[:32]
            self.gesendet.append({"titel": nutzlast["titel"], "an": r["id"]})
            async with httpx.AsyncClient(timeout=15, proxy=None if self.transport else self.proxy,
                                         transport=self.transport) as c:
                antwort = await c.post(endpoint, content=koerper, headers=kopf)
        except Exception as exc:  # noqa: BLE001
            log.warning("Web Push an %s nicht zugestellt: %s", r["id"], type(exc).__name__)
            await self._fehler(r["id"])
            return False
        if antwort.status_code in (404, 410):  # Abo erloschen (App gelöscht, Erlaubnis entzogen)
            log.info("Web Push: Abo %s erloschen, entfernt", r["id"])
            await self.abmelden(r["id"])
            return False
        if antwort.status_code >= 300:
            log.warning("Web Push an %s abgelehnt: HTTP %s", r["id"], antwort.status_code)
            await self._fehler(r["id"])
            return False
        async with await self.store.conn() as c:
            await c.execute("UPDATE push_abos SET zuletzt_ok = now(), fehler = 0 WHERE id = %s", (r["id"],))
        return True

    async def _fehler(self, aid: str) -> None:
        try:
            async with await self.store.conn() as c:
                await c.execute("UPDATE push_abos SET fehler = fehler + 1 WHERE id = %s", (aid,))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------- Für main.py


def _ohne_marke(titel: str) -> str:
    for vorne in ("QuLSI: ", "QuLSI: "):
        if titel.startswith(vorne):
            return titel[len(vorne):]
    return titel


class Benachrichtiger:
    """Gleiche Schnittstelle wie confirm.Notifier: Web Push an alle Geräte, danach ntfy wie bisher."""

    def __init__(self, ntfy, webpush: WebPush):
        self.ntfy = ntfy
        self.webpush = webpush

    def __getattr__(self, name):  # url, topic, gesendet, ... vom ntfy-Notifier
        return getattr(self.ntfy, name)

    async def _push(self, **kw) -> None:
        try:
            await self.webpush.senden(**kw)
        except Exception:  # noqa: BLE001
            log.exception("Web Push fehlgeschlagen")

    async def senden(self, titel: str, text: str) -> None:
        kurz = _ohne_marke(titel)
        await self._push(titel=kurz, privat="Öffne QuLSI für Details.", details=text, url="/#heute")
        await self.ntfy.senden(titel, text)

    async def senden_freigabe(self, fid: str, tool: str, arguments: dict, belastet: bool, url: str,
                              vorschau: str | None = None) -> None:
        details = (vorschau or "\n".join(f"{k}: {v}" for k, v in arguments.items()))[:600]
        await self._push(titel="Freigabe nötig", privat="QuLSI wartet auf deine Freigabe. Entscheide in der App.",
                         details=f"{tool}: {details}\nEntscheide in der App.", url="/#heute",
                         tag=f"freigabe-{fid}", dringend=True, ttl=900)
        await self.ntfy.senden_freigabe(fid, tool, arguments, belastet, url, vorschau)


# ---------------------------------------------------------------------------- API (mit App-Schlüssel)

router = APIRouter(prefix="/api/push")


def _wp(request: Request) -> WebPush:
    wp = getattr(request.app.state, "webpush", None)
    if wp is None:
        raise HTTPException(503, "Web Push ist nicht eingerichtet")
    return wp


@router.get("")
async def uebersicht(request: Request):
    wp = _wp(request)
    return {"schluessel": await wp.oeffentlich(), "geraete": await wp.geraete()}


@router.post("/abo")
async def abo(request: Request, body: dict):
    """{"abo": PushSubscription.toJSON(), "geraet": "iPhone"}. Ein neues Gerät wird allen anderen gemeldet."""
    wp = _wp(request)
    aid, neu = await wp.anmelden(body.get("abo") or {}, body.get("geraet") or "")
    if neu:
        name = " ".join(str(body.get("geraet") or "").split())[:60] or "Neues Gerät"
        try:
            await request.app.state.store.ereignis("system", f"Benachrichtigungen an: {name}", "push")
        except Exception:  # noqa: BLE001
            log.warning("Ereignis für neues Push-Gerät nicht geschrieben")
        await wp.senden("Neues Gerät", f"{name} bekommt jetzt QuLSI-Benachrichtigungen. Warst du das nicht, "
                        "entferne es in der App unter Einstellungen › Benachrichtigungen.", ausser=aid, test=True)
    return {"id": aid, "neu": neu}


@router.delete("/abo/{aid}")
async def abmelden(request: Request, aid: str):
    return {"entfernt": await _wp(request).abmelden(aid)}


@router.post("/abo/{aid}/details")
async def details(request: Request, aid: str, body: dict):
    """Inhalte auf dem Sperrbildschirm: an nur mit Freigabe-PIN, aus immer."""
    an = bool(body.get("an"))
    if an:
        from .confirm import PinFehler  # spät, damit Tests ohne confirm-Abhängigkeiten laufen
        try:
            await request.app.state.app_pin.pruefen(str(body.get("pin") or ""))
        except PinFehler as f:
            return JSONResponse({"fehler": f.text, **f.daten}, status_code=f.status)
    if not await _wp(request).details_setzen(aid, an):
        raise HTTPException(404, "Gerät unbekannt")
    return {"details": an}


@router.post("/test")
async def test(request: Request, body: dict | None = None):
    """Testnachricht an alle Geräte oder nur an {"id": ...}."""
    erg = await _wp(request).senden("Test", "Benachrichtigungen von QuLSI kommen an.",
                                    "Benachrichtigungen von QuLSI kommen an.", nur=(body or {}).get("id"),
                                    url="/#einstellungen/benachrichtigungen", test=True)
    return erg


# ---------------------------------------------------------------------------- Service Worker (ohne Schlüssel)

seiten = APIRouter()
WEB = Path(__file__).parent / "web"


@seiten.get("/sw.js", include_in_schema=False)
async def service_worker():
    # Muss unter / liegen, damit er für die ganze App gilt. Nicht cachen, sonst kommen Updates spät an.
    return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})
