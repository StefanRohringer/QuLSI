"""Web Push (core/qulsi_core/push.py): Verschlüsselung, VAPID, erlaubte Dienste, Versand mit echter
Datenbank (temporäres PostgreSQL wie in test_hintergrund_db, sonst übersprungen)."""

from __future__ import annotations

import asyncio
import base64
import glob
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from qulsi_core import push
from qulsi_core.push import b64u, unb64u

ROOT = Path(__file__).resolve().parents[2]


def _schluessel(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(b64), "big"), ec.SECP256R1())


def _hm(k: bytes, d: bytes) -> bytes:
    return hmac.new(k, d, hashlib.sha256).digest()


def entschluesseln(koerper: bytes, ua_privat: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """Gegenstück wie im Browser, unabhängig von push.verschluesseln geschrieben."""
    salt, idlen = koerper[:16], koerper[20]
    as_public = koerper[21:21 + idlen]
    ua_public = push._oeffentlich(ua_privat)
    ecdh = ua_privat.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = _hm(_hm(auth, ecdh), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hm(salt, ikm)
    klar = AESGCM(_hm(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
        _hm(prk, b"Content-Encoding: nonce\x00\x01")[:12], koerper[21 + idlen:], None)
    assert klar.endswith(b"\x02")
    return klar[:-1]


# ---------------------------------------------------------------- RFC 8291, Beispiel aus Abschnitt 5

RFC_AS_PRIVAT = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_UA_PRIVAT = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_KOERPER = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6"
               "PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_rfc8291_beispiel():
    koerper = push.verschluesseln(b"When I grow up, I want to be a watermelon", unb64u(RFC_UA_PUBLIC),
                                  unb64u(RFC_AUTH), salt=unb64u(RFC_SALT), as_privat=_schluessel(RFC_AS_PRIVAT))
    assert b64u(koerper) == RFC_KOERPER


def test_hin_und_zurueck():
    ua = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    text = json.dumps({"titel": "Gleich ein Termin", "text": "Zahnarzt um 14:30 ✓"}).encode()
    k1 = push.verschluesseln(text, push._oeffentlich(ua), auth)
    k2 = push.verschluesseln(text, push._oeffentlich(ua), auth)
    assert k1 != k2  # jedes Mal neues Salz und neuer Schlüssel
    assert entschluesseln(k1, ua, auth) == text
    with pytest.raises(ValueError):
        push.verschluesseln(b"x" * 5000, push._oeffentlich(ua), auth)


def test_vapid_jwt():
    privat = ec.generate_private_key(ec.SECP256R1())
    jwt = push.vapid_jwt(privat, "https://web.push.apple.com", "mailto:a@b.de")
    kopf, inhalt, sig = jwt.split(".")
    assert json.loads(unb64u(kopf)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(unb64u(inhalt))
    assert claims["aud"] == "https://web.push.apple.com" and claims["sub"] == "mailto:a@b.de"
    roh = unb64u(sig)
    assert len(roh) == 64
    der = encode_dss_signature(int.from_bytes(roh[:32], "big"), int.from_bytes(roh[32:], "big"))
    privat.public_key().verify(der, f"{kopf}.{inhalt}".encode(), ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("endpoint,erwartet", [
    ("https://web.push.apple.com/QGx1", "Apple"),
    ("https://fcm.googleapis.com/fcm/send/abc", "Google"),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", "Mozilla"),
    ("https://wns2-db5p.notify.windows.com/w/?token=x", "Microsoft"),
    ("http://web.push.apple.com/x", None),                  # kein https
    ("https://push.apple.com.boese.de/x", None),             # nur Name ähnlich
    ("https://boesepush.apple.com/x", None),
    ("https://web.push.apple.com:8443/x", None),
    ("https://nutzer:pw@web.push.apple.com/x", None),
    ("https://127.0.0.1/x", None),
    ("https://postgres:5432/", None),
    ("kein link", None),
])
def test_nur_bekannte_push_dienste(endpoint, erwartet):
    assert push.dienst(endpoint) == erwartet


def test_proxy_url():
    assert push.proxy_url("http://egress:3128") == "http://egress:3128"
    assert push.proxy_url("http://127.0.0.1:3128") == "http://webpush:x@127.0.0.1:3128"
    assert push.proxy_url("") is None


def test_egress_kennt_webpush():
    import qulsi_egress
    c = qulsi_egress.connectoren_laden(str(ROOT / "connectors"))["webpush"]
    assert c.dienst == "core"
    assert c.erlaubt("web.push.apple.com", 443) and c.erlaubt("fcm.googleapis.com", 443)
    assert not c.erlaubt("web.push.apple.com", 80) and not c.erlaubt("example.com", 443)


def test_sw_datei_da():
    sw = (ROOT / "core/qulsi_core/web/sw.js").read_text()
    assert "showNotification" in sw and "notificationclick" in sw and "fetch" not in sw


# ---------------------------------------------------------------- Mit Datenbank

psycopg = pytest.importorskip("psycopg")


def _pg_bin() -> Path | None:
    if shutil.which("pg_ctl"):
        return Path(shutil.which("pg_ctl")).parent
    treffer = sorted(glob.glob("/usr/lib/postgresql/*/bin/pg_ctl"))
    return Path(treffer[-1]).parent if treffer else None


@pytest.fixture(scope="module")
def dsn():
    if os.environ.get("QULSI_TEST_DSN"):
        yield os.environ["QULSI_TEST_DSN"]
        return
    bin_ = _pg_bin()
    if bin_ is None:
        pytest.skip("PostgreSQL nicht installiert")
    d = Path(tempfile.mkdtemp(prefix="qulsi-pg-"))
    os.chmod(d, 0o777)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    als = ["runuser", "-u", "nobody", "--"] if os.geteuid() == 0 else []
    run = lambda *a: subprocess.run([*als, *a], check=True, capture_output=True, text=True)  # noqa: E731
    try:
        run(str(bin_ / "initdb"), "-D", str(d / "data"), "-U", "qulsi", "-A", "trust")
        run(str(bin_ / "pg_ctl"), "-D", str(d / "data"), "-l", str(d / "log"), "-w", "start",
            "-o", f"-p {port} -k {d} -c listen_addresses=127.0.0.1")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"PostgreSQL startet nicht: {exc}")
    url = f"postgresql://qulsi@127.0.0.1:{port}/postgres"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute((ROOT / "db/init/01_schema.sql").read_text())
    yield url
    run(str(bin_ / "pg_ctl"), "-D", str(d / "data"), "-m", "immediate", "stop")
    shutil.rmtree(d, ignore_errors=True)


class Geraet:
    """Ein Handy: eigener Schlüssel, entschlüsselt, was ankommt."""

    def __init__(self, endpoint: str):
        self.privat = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = endpoint

    def abo(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": b64u(push._oeffentlich(self.privat)), "auth": b64u(self.auth)}}


class Dienst:
    """Ersatz für Apple/Google: nimmt an, merkt sich Kopf und Inhalt, oder antwortet mit status."""

    def __init__(self, geraete: list[Geraet]):
        self.geraete = {g.endpoint: g for g in geraete}
        self.status: dict[str, int] = {}
        self.empfangen: list[tuple[str, dict, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        g = self.geraete[url]
        daten = json.loads(entschluesseln(req.content, g.privat, g.auth))
        self.empfangen.append((url, dict(req.headers), daten))
        return httpx.Response(self.status.get(url, 201))


@pytest.fixture
def wp(dsn):
    from qulsi_core.memory import Store
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS push_abos")
        c.execute("DELETE FROM system_state WHERE key = 'webpush_vapid'")
    w = push.WebPush(Store(dsn), kontakt="mailto:test@example.com")
    asyncio.run(w.vorbereiten())
    return w


def test_schluessel_bleibt(wp, dsn):
    from qulsi_core.memory import Store
    a = asyncio.run(wp.oeffentlich())
    neu = push.WebPush(Store(dsn))
    assert asyncio.run(neu.oeffentlich()) == a
    assert len(unb64u(a)) == 65


def test_versand_sperrbildschirm_und_details(wp):
    iphone, laptop = Geraet("https://web.push.apple.com/iphone"), Geraet("https://fcm.googleapis.com/fcm/send/laptop")
    dienst = Dienst([iphone, laptop])
    wp.transport = httpx.MockTransport(dienst)

    async def ablauf():
        aid, neu = await wp.anmelden(iphone.abo(), "iPhone")
        assert neu and (await wp.anmelden(iphone.abo(), ""))[1] is False  # zweimal = ein Gerät
        lid, _ = await wp.anmelden(laptop.abo(), "Laptop")
        await wp.details_setzen(lid, True)
        b = push.Benachrichtiger(_Ntfy(), wp)
        await b.senden("QuLSI: Gleich ein Termin", "In 15 Min (14:30): Zahnarzt, Hauptstraße 3")
        return aid, lid, b

    aid, lid, b = asyncio.run(ablauf())
    nach = {url: d for url, _, d in dienst.empfangen}
    assert nach[iphone.endpoint]["titel"] == "Gleich ein Termin"
    assert "Zahnarzt" not in nach[iphone.endpoint]["text"]          # Sperrbildschirm ohne Inhalt
    assert "Zahnarzt" in nach[laptop.endpoint]["text"]               # Details an
    assert b.ntfy.gesendet == [("QuLSI: Gleich ein Termin", "In 15 Min (14:30): Zahnarzt, Hauptstraße 3")]
    kopf = dienst.empfangen[0][1]
    assert kopf["content-encoding"] == "aes128gcm" and kopf["authorization"].startswith("vapid t=")
    assert kopf["ttl"] == "3600"
    geraete = asyncio.run(wp.geraete())
    assert {g["geraet"] for g in geraete} == {"iPhone", "Laptop"} and all(g["zuletzt_ok"] for g in geraete)
    assert "endpoint" not in geraete[0]  # die App sieht keine Push-Adressen


def test_freigabe_dringend_und_ohne_inhalt(wp):
    iphone = Geraet("https://web.push.apple.com/i2")
    dienst = Dienst([iphone])
    wp.transport = httpx.MockTransport(dienst)

    async def ablauf():
        await wp.anmelden(iphone.abo(), "iPhone")
        await push.Benachrichtiger(_Ntfy(), wp).senden_freigabe("f1", "email.send", {"an": "chef@firma.de"}, False,
                                                               "http://x/bestaetigung/f1?t=geheim")
    asyncio.run(ablauf())
    _, kopf, daten = dienst.empfangen[0]
    assert kopf["urgency"] == "high" and kopf["ttl"] == "900" and "topic" in kopf
    assert daten["titel"] == "Freigabe nötig" and "chef@" not in daten["text"] and daten["url"] == "/#heute"
    assert "geheim" not in json.dumps(daten)  # der Link mit Token aus ntfy landet nie im Push


def test_erloschenes_abo_wird_entfernt(wp):
    alt = Geraet("https://web.push.apple.com/alt")
    dienst = Dienst([alt])
    dienst.status[alt.endpoint] = 410
    wp.transport = httpx.MockTransport(dienst)

    async def ablauf():
        await wp.anmelden(alt.abo(), "altes iPhone")
        erg = await wp.senden("Test", "x")
        return erg, await wp.geraete()
    erg, geraete = asyncio.run(ablauf())
    assert erg == {"gesendet": 0, "fehler": 1} and geraete == []


def test_abo_pruefungen(wp):
    async def anmelden(abo):
        return await wp.anmelden(abo)
    from fastapi import HTTPException
    g = Geraet("https://web.push.apple.com/ok")
    for falsch in [
        {**g.abo(), "endpoint": "http://127.0.0.1:8100/api/chat"},
        {**g.abo(), "endpoint": "https://evil.example/x"},
        {"endpoint": g.endpoint, "keys": {"p256dh": "AAAA", "auth": b64u(os.urandom(16))}},
        {"endpoint": g.endpoint, "keys": {"p256dh": g.abo()["keys"]["p256dh"], "auth": "AAAA"}},
    ]:
        with pytest.raises(HTTPException):
            asyncio.run(anmelden(falsch))


def test_stundengrenze(wp, monkeypatch):
    iphone = Geraet("https://web.push.apple.com/g")
    dienst = Dienst([iphone])
    wp.transport = httpx.MockTransport(dienst)
    monkeypatch.setattr(push, "MAX_PRO_STUNDE", 3)

    async def ablauf():
        await wp.anmelden(iphone.abo())
        return [await wp.senden("T", "x") for _ in range(4)] + [await wp.senden("T", "x", test=True)]
    erg = asyncio.run(ablauf())
    assert [e["gesendet"] for e in erg] == [1, 1, 1, 0, 1]


def test_api(wp):
    """Endpunkte mit echtem FastAPI: Abo, neues Gerät wird gemeldet, Details nur mit PIN, Test, Abmelden."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from qulsi_core.confirm import AppPin, pin_hash

    iphone, ipad = Geraet("https://web.push.apple.com/api1"), Geraet("https://web.push.apple.com/api2")
    dienst = Dienst([iphone, ipad])
    wp.transport = httpx.MockTransport(dienst)
    app = FastAPI()
    app.include_router(push.router)
    app.include_router(push.seiten)
    app.state.webpush = wp
    app.state.store = wp.store
    app.state.app_pin = AppPin(wp.store, pin_hash("4711"))
    with psycopg.connect(wp.store.url, autocommit=True) as c:
        c.execute("DELETE FROM system_state WHERE key = 'freigabe_pin'")
    c = TestClient(app)

    assert c.get("/sw.js").headers["content-type"].startswith("text/javascript")
    a = c.post("/api/push/abo", json={"abo": iphone.abo(), "geraet": "iPhone"}).json()
    assert a["neu"] and dienst.empfangen == []  # niemand sonst zum Warnen
    b = c.post("/api/push/abo", json={"abo": ipad.abo(), "geraet": "iPad"}).json()
    assert [(u, d["titel"]) for u, _, d in dienst.empfangen] == [(iphone.endpoint, "Neues Gerät")]
    assert "iPad" in dienst.empfangen[0][2]["text"]
    uebersicht = c.get("/api/push").json()
    assert len(unb64u(uebersicht["schluessel"])) == 65 and len(uebersicht["geraete"]) == 2

    assert c.post(f"/api/push/abo/{a['id']}/details", json={"an": True, "pin": "0000"}).status_code == 403
    assert c.post(f"/api/push/abo/{a['id']}/details", json={"an": True, "pin": "4711"}).json() == {"details": True}
    assert c.post(f"/api/push/abo/{a['id']}/details", json={"an": False}).json() == {"details": False}

    dienst.empfangen.clear()
    assert c.post("/api/push/test", json={"id": b["id"]}).json() == {"gesendet": 1, "fehler": 0}
    assert [u for u, _, _ in dienst.empfangen] == [ipad.endpoint]
    assert c.delete(f"/api/push/abo/{b['id']}").json() == {"entfernt": True}
    assert len(c.get("/api/push").json()["geraete"]) == 1


class _Ntfy:
    def __init__(self):
        self.gesendet = []

    async def senden(self, titel, text):
        self.gesendet.append((titel, text))

    async def senden_freigabe(self, *a, **kw):
        self.gesendet.append(("freigabe", a[0]))
