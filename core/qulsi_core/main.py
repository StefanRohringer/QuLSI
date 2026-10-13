"""QuLSI Core: HTTP-Schnittstelle für App, Web, Stimme und andere Programme.

- /                     QuLSI-Web-App (auch als App auf dem Handy-Startbildschirm)
- /api/chat             Nachricht an QuLSI
- /api/stimme           Audio rein, Text und Antwort raus (STT über den Voice-Dienst)
- /api/tts              Text rein, Audio raus
- /api/stimme/probe     eigene Stimme aus der App, wird erst nach Freigabe übernommen (stimmprobe.py)
- /api/heute            Termine, Aufgaben, neue Mails für die Startseite
- /api/timeline         Was an einem Tag passiert ist
- /api/events           Event Bus live (Server-Sent Events)
- /api/freigaben        Freigaben der letzten 24 h; POST /api/freigaben/<id> entscheidet in der App
                        (Erlauben nur mit Freigabe-PIN, Ablehnen immer)
- /v1/chat/completions  OpenAI-kompatibel für Open WebUI, Home Assistant usw.
                        Modelle: "qulsi" (Router entscheidet), "qulsi-stimme", "qulsi-gross"
- /bestaetigung/<id>    Ziel der Knöpfe in der Handy-Benachrichtigung
- /api/agenten          Hintergrund-Agents (proaktiv/, config/agenten.yaml)
- /api/einstellungen    Einstellungen aus der App (einstellungen.py)
- /api/umgebung         Box oder RunPod, passende Befehle für Konten und Geheimnisse
- /api/konten           Konten verbinden und Zugangsdaten setzen, erst nach Freigabe (konten.py)
- /api/push             Web Push: Geräte anmelden, Test, Details auf dem Sperrbildschirm (push.py)
- /api/sitzung          Cookie für EventSource und Bilder (nie den Schlüssel in die URL)
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from .agent import LLM, Agent, Antwort, _text_und_bild
from .config import Settings
from .confirm import AppPin, Freigaben, Notifier, PinFehler
from .einstellungen import Einstellungen
from .einstellungen import router as einstellungen_router
from .hub import Hub
from .memory import Graph, ObjectStore, Store, Vektoren
from .memory.api import router as gedaechtnis_api
from .memory.schema import migrieren
from .policy import Kontext, Policy
from .proaktiv import Hintergrund
from .proaktiv import router as agenten_router
from . import hochladen, konten, logschutz, push, schutz, stimmprobe
from .router import Router
from .tools import TZ, Kern, zeit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logschutz.installieren()  # keine Schlüssel, Tokens oder Passwörter in den Logs
log = logging.getLogger("qulsi")

WEB = Path(__file__).parent / "web"
MODELLE = {"qulsi": ("chat", None), "qulsi-stimme": ("stimme", None), "qulsi-gross": ("chat", "gross")}


class BelasteteAntworten:
    """Merkt sich Antworten, die mit Mail-/Web-Inhalt im Kontext entstanden sind.

    Clients schicken bei jeder Nachricht den ganzen Verlauf. Taucht darin eine
    solche Antwort wieder auf, gilt auch die neue Anfrage als belastet. Gemerkt wird
    das Paar aus Frage und Antwort: kurze Standardantworten wie „Gemerkt.“ würden
    sonst jedes andere Gespräch mitbelasten.
    """

    def __init__(self, max_eintraege: int = 5000):
        self.hashes: OrderedDict[str, None] = OrderedDict()
        self.max = max_eintraege

    @staticmethod
    def _h(frage: str, antwort: str) -> str:
        return hashlib.sha256(f"{frage.strip()}\x00{antwort.strip()}".encode()).hexdigest()

    def merken(self, frage: str, antwort: str) -> None:
        self.hashes[self._h(frage, antwort)] = None
        while len(self.hashes) > self.max:
            self.hashes.popitem(last=False)

    def pruefen(self, verlauf: list[dict]) -> bool:
        frage = ""
        for m in verlauf:
            if m.get("role") == "user":
                frage = _text_und_bild(m.get("content"))[0]
            elif m.get("role") == "assistant" and isinstance(m.get("content"), str) \
                    and self._h(frage, m["content"]) in self.hashes:
                return True
        return False


async def _wiederholen(name: str, fn, versuche: int = 30) -> None:
    for i in range(versuche):
        try:
            await fn()
            return
        except Exception as exc:  # noqa: BLE001
            if i == versuche - 1:
                log.error("%s nicht bereit: %s", name, exc)
                return
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = Settings()
    store = Store(s.database_url)
    vektoren = Vektoren(s.qdrant_url, s.llm_base_url, s.llm_api_key, s.embed_dim)
    await _wiederholen("Qdrant", vektoren.vorbereiten)
    await _wiederholen("Gedächtnis-Schema", lambda: migrieren(store))
    if s.api_key in ("", "bitte-aendern"):
        # Jede Box braucht ihren eigenen Schlüssel (scripts/erststart.sh bzw. runpod/start.sh legen ihn an)
        raise RuntimeError("QULSI_API_KEY fehlt oder ist der Beispielwert. Box: scripts/erststart.sh")
    policy = Policy.load(s.policy_path)
    # Web Push an die App auf Handy/Uhr, danach ntfy nur, wenn eingerichtet (sonst nur protokolliert)
    webpush = push.WebPush(store, s.egress_url)
    await _wiederholen("Web Push", webpush.vorbereiten)
    notifier = push.Benachrichtiger(Notifier(s.ntfy_url, s.ntfy_topic, s.ntfy_token), webpush)
    freigaben = Freigaben(s.database_url, s.public_url, policy.gueltig_sekunden, notifier)
    kern = Kern(store, vektoren, Graph(store))
    await _wiederholen("Nutzername", kern.nutzer_laden)
    hub = Hub(s.connectors_dir, policy, freigaben, store, kern, s.offline, s.egress_url)
    # Connectoren starten evtl. später als der Core: bis zu 60 s warten, bis alle antworten.
    for _ in range(30):
        await hub.laden()
        if hub.alle_erreichbar():
            break
        await asyncio.sleep(2)
    router = Router.load(s.router_path, s.timezone, {"klein": s.model_small, "standard": s.model_standard,
                                                     "gross": s.model_large})
    app.state.settings = s
    app.state.store = store
    app.state.objekte = ObjectStore()
    app.state.notifier = notifier
    app.state.webpush = webpush
    app.state.freigaben = freigaben
    app.state.app_pin = AppPin(store, s.freigabe_pin)
    try:
        await app.state.app_pin.laden()
    except Exception:  # noqa: BLE001
        log.exception("Freigabe-PIN konnte nicht aus der Datenbank geladen werden")
    hub.app_freigabe = app.state.app_pin.eingerichtet
    app.state.hub = hub
    app.state.agent = Agent(LLM(s.llm_base_url, s.llm_api_key), hub, router, s.prompt_path, s.timezone, s.max_schritte)
    app.state.belastet = BelasteteAntworten()
    hintergrund = Hintergrund(store, hub, app.state.agent, notifier, Path(s.policy_path).parent / "agenten.yaml",
                              s.timezone)
    await _wiederholen("Hintergrund-Agents", hintergrund.starten)
    app.state.hintergrund = hintergrund
    kern.organisator.hub = hub  # Mails holt der Organisator über den Hub (mit Policy und Protokoll)
    organisator = asyncio.create_task(kern.organisator.schleife())
    app.state.organisator_task = organisator
    app.state.einstellungen = Einstellungen(app.state)
    await _wiederholen("Einstellungen", app.state.einstellungen.laden)
    waechter = asyncio.create_task(_sicherheit_schleife(hub))
    yield
    waechter.cancel()
    organisator.cancel()
    await hintergrund.stoppen()


async def _sicherheit_schleife(hub: Hub) -> None:
    """Quarantäne-Meldungen des Egress-Proxys alle 15 Sekunden abholen."""
    while True:
        try:
            await hub.sicherheit_pruefen()
        except Exception:  # noqa: BLE001
            log.exception("Sicherheitsprüfung fehlgeschlagen")
        await asyncio.sleep(15)


app = FastAPI(title="QuLSI", lifespan=lifespan)


def _bearer_ok(request: Request, authorization: str) -> bool:
    soll = request.app.state.settings.api_key
    return not soll or secrets.compare_digest(authorization.encode(), f"Bearer {soll}".encode())


def auth(request: Request, authorization: str = Header(default="")) -> None:
    """Nur der Bearer-Header. Schlüssel in der URL landen in Logs und Proxys, deshalb nie ?key=."""
    if not _bearer_ok(request, authorization):
        raise HTTPException(401, "Falscher API-Schlüssel")


SITZUNG_COOKIE = "qulsi_sitzung"
SITZUNG_SEKUNDEN = 12 * 3600


class Sitzungen:
    """Cookie für das, was der Browser ohne Header lädt (EventSource, <img>). Gilt nur für GET auf
    /api/events und /api/objekt/..., ändern kann man damit nichts. Liegt nur im Speicher."""

    def __init__(self):
        self.offen: dict[str, float] = {}

    def neu(self) -> str:
        jetzt = time.time()
        self.offen = {k: v for k, v in self.offen.items() if v > jetzt}
        wert = secrets.token_urlsafe(32)
        self.offen[hashlib.sha256(wert.encode()).hexdigest()] = jetzt + SITZUNG_SEKUNDEN
        return wert

    def gueltig(self, wert: str) -> bool:
        return bool(wert) and self.offen.get(hashlib.sha256(wert.encode()).hexdigest(), 0) > time.time()

    def beenden(self, wert: str) -> None:
        self.offen.pop(hashlib.sha256(wert.encode()).hexdigest(), None)


SITZUNGEN = Sitzungen()


def auth_lesen(request: Request, authorization: str = Header(default="")) -> None:
    """Bearer-Header oder Sitzungs-Cookie (nur für lesende GETs ohne Header-Möglichkeit)."""
    if _bearer_ok(request, authorization) or SITZUNGEN.gueltig(request.cookies.get(SITZUNG_COOKIE, "")):
        return
    raise HTTPException(401, "Falscher API-Schlüssel oder Sitzung abgelaufen")


@app.post("/api/sitzung", dependencies=[Depends(auth)])
async def sitzung_starten(request: Request):
    """Setzt das Cookie für Live-Ereignisse und Bilder. Die App ruft das nach dem Anmelden auf
    (und erneut, wenn /api/events mit 401 abbricht, z. B. nach einem Neustart)."""
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    r = JSONResponse({"gueltig_sekunden": SITZUNG_SEKUNDEN})
    r.set_cookie(SITZUNG_COOKIE, SITZUNGEN.neu(), max_age=SITZUNG_SEKUNDEN, httponly=True, samesite="strict",
                 secure=https, path="/api")
    return r


@app.delete("/api/sitzung")
async def sitzung_beenden(request: Request):
    SITZUNGEN.beenden(request.cookies.get(SITZUNG_COOKIE, ""))
    r = JSONResponse({"ok": True})
    r.delete_cookie(SITZUNG_COOKIE, path="/api")
    return r


app.include_router(gedaechtnis_api, dependencies=[Depends(auth)])


app.include_router(agenten_router, dependencies=[Depends(auth)])
app.include_router(einstellungen_router, dependencies=[Depends(auth)])
app.include_router(stimmprobe.router, dependencies=[Depends(auth)])
app.include_router(hochladen.router, dependencies=[Depends(auth)])
app.include_router(push.router, dependencies=[Depends(auth)])
app.include_router(push.seiten)  # /sw.js ohne Schlüssel (enthält nichts Geheimes)
app.include_router(konten.router, dependencies=[Depends(auth)])


# ---------------------------------------------------------------------------
# Web-App
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def startseite():
    return FileResponse(WEB / "index.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
async def webmanifest():
    return JSONResponse({
        "name": "QuLSI – Quantized Local Superintelligence", "short_name": "QuLSI", "start_url": "/", "display": "standalone",
        "background_color": "#000000", "theme_color": "#000000",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                  {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_icon():  # iPhone nimmt für den Home-Bildschirm nur PNG
    return FileResponse(WEB / "apple-touch-icon.png", media_type="image/png")


@app.get("/icon.svg", include_in_schema=False)
async def icon():
    return FileResponse(WEB / "icon.svg", media_type="image/svg+xml")


BEFEHLE = {
    "box": {
        "google": "docker compose exec connector-google python google/server.py anmelden",
        "microsoft": "docker compose exec connector-microsoft python microsoft/server.py anmelden",
        "nas": "docker compose run --rm tresor setzen NAS_PASSWORD",
        "mail": "docker compose run --rm tresor setzen IMAP_PASSWORD",
        "home_assistant": "docker compose run --rm tresor setzen HA_TOKEN",
        "geheimnis": "docker compose run --rm tresor setzen NAME",
        "freigabe_pin": "docker compose exec core python -m qulsi_core.pin_code",
        "daten_zeigen": "docker compose exec core python -m qulsi_core.zuruecksetzen zeigen",
        "daten_leeren": "docker compose exec core python -m qulsi_core.zuruecksetzen leeren && docker compose restart core",
        "pruefen": "docker compose exec core python -m qulsi_core.abnahme",
    },
    "runpod": {
        "google": "bash runpod/qulsi.sh anmelden google",
        "microsoft": "bash runpod/qulsi.sh anmelden microsoft",
        "nas": "bash runpod/qulsi.sh geheimnis NAS_PASSWORD connector-nas",
        "mail": "bash runpod/qulsi.sh geheimnis IMAP_PASSWORD connector-mail",
        "home_assistant": "bash runpod/qulsi.sh geheimnis HA_TOKEN connector-home_assistant",
        "geheimnis": "bash runpod/qulsi.sh geheimnis NAME [dienst]",
        "freigabe_pin": "bash runpod/qulsi.sh pin",
        "daten_zeigen": "bash runpod/qulsi.sh daten zeigen",
        "daten_leeren": "bash runpod/qulsi.sh daten leeren",
        "pruefen": "bash runpod/qulsi.sh pruefen",
    },
}


@app.get("/api/umgebung", dependencies=[Depends(auth)])
async def api_umgebung():
    """Wo läuft QuLSI? Die App zeigt danach die passenden Befehle (Terminal der Box bzw. des Pods)."""
    laufzeit = "runpod" if os.environ.get("QULSI_LAUFZEIT") == "runpod" else "box"
    warnung = None
    if laufzeit == "runpod":
        warnung = ("Testbetrieb auf RunPod: Es gibt keinen Tresor, Passwörter und Konten-Anmeldungen liegen "
                   "unverschlüsselt im Pod-Speicher (/workspace). Bitte nur Test-Konten verbinden.")
    from .einstellungen import _version
    return {"laufzeit": laufzeit, "tresor": laufzeit == "box", "warnung": warnung, "befehle": BEFEHLE[laufzeit],
            **_version()}


@app.get("/api/nutzer", dependencies=[Depends(auth)])
async def api_nutzer_lesen(request: Request):
    """Name und Bundesland (Kürzel aus feiertage.LAENDER) des Nutzers, je null, solange nicht eingetragen."""
    k = request.app.state.hub.kern
    return {"name": k.nutzer_name, "bundesland": k.bundesland}


@app.post("/api/nutzer", dependencies=[Depends(auth)])
async def api_nutzer_setzen(request: Request):
    """{"name": "..."|null} und/oder {"bundesland": "BY"|null}: nur die mitgeschickten Felder ändern sich."""
    daten = await request.json()
    if not isinstance(daten, dict):
        raise HTTPException(422, "JSON-Objekt erwartet")
    k = request.app.state.hub.kern
    if "name" in daten:
        if daten["name"] is not None and not isinstance(daten["name"], str):
            raise HTTPException(422, "name muss Text oder null sein")
        await k.nutzer_setzen(daten["name"])
    if "bundesland" in daten:
        if daten["bundesland"] is not None and not isinstance(daten["bundesland"], str):
            raise HTTPException(422, "bundesland muss ein Kürzel wie BY oder null sein")
        try:
            await k.bundesland_setzen(daten["bundesland"])
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
    return {"name": k.nutzer_name, "bundesland": k.bundesland}


@app.get("/health")
async def health(request: Request):
    hub: Hub = request.app.state.hub
    return {"ok": True, "werkzeuge": sorted(hub.werkzeuge), "connectoren": hub.erreichbar,
            "offline": await hub.offline()}


# ---------------------------------------------------------------------------
# Gespräch
# ---------------------------------------------------------------------------


async def _gespraech(request: Request, verlauf: list[dict], kanal: str, erzwingen: str | None = None,
                     anhang: bool = False) -> tuple[Antwort | None, str, Kontext]:
    st = request.app.state
    eigene = [_text_und_bild(m.get("content"))[0] for m in verlauf if m.get("role") == "user"][-3:]
    if name := st.hub.kern.nutzer_name:
        eigene.append(f"Ich heiße {name}.")  # der eigene Name ist eigene Angabe ("Kim ist 2,20 m groß")
    ctx = Kontext(kanal=kanal, belastet=st.belastet.pruefen(verlauf), nutzer_texte=eigene)
    if ctx.belastet:
        ctx.belastet_durch.append("früheren Antworten")
    if anhang:  # Hochgeladenes kann von Dritten stammen
        ctx.belastet = True
        ctx.belastet_durch.append("Anhang")
    if not st.hub.alle_erreichbar():
        await st.hub.laden()  # ausgefallener Connector ist evtl. wieder da
    try:
        a = await st.agent.antworten(verlauf, ctx, erzwingen)
        text = a.text
    except httpx.HTTPError as exc:
        log.error("Sprachmodell nicht erreichbar: %s", exc)
        a, text = None, "Das Sprachmodell ist gerade nicht erreichbar. Bitte versuch es gleich noch einmal."
    frage = next((_text_und_bild(m.get("content"))[0] for m in reversed(verlauf) if m.get("role") == "user"), "")
    if ctx.belastet:
        st.belastet.merken(frage, text)
    if frage and a:
        try:  # für conversations.search ("worüber haben wir gesprochen?"), bleibt auf der Box
            async with await st.store.conn() as conn:
                await conn.execute("INSERT INTO gespraeche (kanal, frage, antwort, belastet) VALUES (%s, %s, %s, %s)",
                                   (kanal, frage[:4000], text[:8000], ctx.belastet))
        except Exception:  # noqa: BLE001
            log.warning("Gespräch nicht gespeichert", exc_info=True)
    if a:
        log.info("kanal=%s route=%s modell=%s belastet=%s werkzeuge=%s", kanal, a.route.art, a.route.modell,
                 ctx.belastet, [x["tool"] for x in a.aufrufe])
    return a, text, ctx


def _info(a: Antwort | None, ctx: Kontext) -> dict:
    return {
        "route": a.route.art if a else "fehler",
        "modell": a.route.modell if a else None,
        "werkzeuge": [x["tool"] for x in a.aufrufe] if a else [],
        "belastet": ctx.belastet,
        "warnungen": schutz.spur_von(ctx).warnungen,  # z. B. "In dieser Mail steht eine versteckte Anweisung ..."
        "offline": a.offline if a else None,
    }


@app.post("/api/chat", dependencies=[Depends(auth)])
async def api_chat(request: Request):
    body = await request.json()
    verlauf = [m for m in body.get("verlauf", []) if m.get("role") in ("user", "assistant")]
    inhalt, anhang = await hochladen.anhaenge_einbauen(request.app.state, str(body.get("nachricht") or ""),
                                                       body.get("anhaenge") or [])
    verlauf.append({"role": "user", "content": inhalt})
    a, text, ctx = await _gespraech(request, verlauf, body.get("kanal", "app"), body.get("modell"), anhang)
    return {"antwort": text, **_info(a, ctx)}


def _tts_vorwaermen(st) -> None:
    url = st.settings.tts_url
    if not url:
        return
    e = getattr(st, "einstellungen", None)
    stimme = (e.werte.get("stimme", {}).get("stimme") if e else None) or None
    if stimme and stimme.startswith("chatterbox"):
        url = getattr(st.settings, "tts_chatterbox_url", "") or url

    async def los():
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                await c.post(f"{url}/vorwaermen", json={"stimme": stimme})
        except Exception:  # noqa: BLE001  ohne TTS-Dienst spricht Piper
            pass
    aufgabe = asyncio.create_task(los())
    st.tts_aufgaben = getattr(st, "tts_aufgaben", set())
    st.tts_aufgaben.add(aufgabe)
    aufgabe.add_done_callback(st.tts_aufgaben.discard)


@app.post("/api/stimme", dependencies=[Depends(auth)])
async def api_stimme(request: Request, audio: UploadFile):
    """Push-to-talk aus der App: Audio -> STT -> Agent (Kanal Stimme)."""
    s: Settings = request.app.state.settings
    _tts_vorwaermen(request.app.state)  # Sprachausgabe lädt, während STT und Sprachmodell arbeiten
    daten = await audio.read()
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        r = await c.post(f"{s.voice_url}/stt", files={"audio": (audio.filename or "audio.webm", daten, audio.content_type)})
    if r.status_code >= 400:
        raise HTTPException(502, f"Spracherkennung: {r.text[:200]}")
    gehoert = r.json().get("text", "").strip()
    if not gehoert:
        return {"text": "", "antwort": "Ich habe nichts verstanden.", "route": "keine"}
    a, text, ctx = await _gespraech(request, [{"role": "user", "content": gehoert}], "stimme")
    return {"text": gehoert, "antwort": text, **_info(a, ctx)}


@app.post("/api/tts", dependencies=[Depends(auth)])
async def api_tts(request: Request):
    s: Settings = request.app.state.settings
    body = await request.json()
    e = getattr(request.app.state, "einstellungen", None)
    vorgabe = e.werte.get("stimme", {}) if e else {}  # Sprecher aus den App-Einstellungen
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
            r = await c.post(f"{s.voice_url}/tts", json={"text": body.get("text", ""),
                                                          "stimme": body.get("stimme") or vorgabe.get("stimme")})
    except httpx.HTTPError as exc:
        log.warning("Sprachausgabe nicht erreichbar: %s", type(exc).__name__)
        raise HTTPException(503, "Die Sprachausgabe antwortet gerade nicht.") from None
    if r.status_code == 503:
        raise HTTPException(503, "Die Sprachausgabe antwortet gerade nicht.")
    if r.status_code >= 400:
        raise HTTPException(502, f"Sprachausgabe: {r.text[:200]}")
    return Response(r.content, media_type=r.headers.get("content-type", "audio/wav"))


# ---------------------------------------------------------------------------
# Startseite der App: Heute, Timeline, Ereignisse, Freigaben
# ---------------------------------------------------------------------------


@app.get("/api/heute", dependencies=[Depends(auth)])
async def api_heute(request: Request):
    st = request.app.state
    store: Store = st.store
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    termine = await store.alle(
        """SELECT titel, start, ende, ganztags, ort, quelle FROM termine
           WHERE start < %s AND coalesce(ende, start) >= %s ORDER BY ganztags DESC, start""",
        (start + timedelta(days=1), start))
    aufgaben = await store.alle(
        """SELECT a.id, a.titel, a.faellig, p.name AS projekt FROM aufgaben a LEFT JOIN projekte p ON p.id = a.projekt_id
           WHERE a.status = 'offen' ORDER BY a.faellig NULLS LAST, a.erstellt LIMIT 20""")
    hub: Hub = st.hub
    offline = await hub.offline()
    mails: list | str = "Mail-Connector nicht eingerichtet."
    if hub.finden("email.search"):
        mails = "Offline: keine Mails." if offline else await hub.daten("email.search", {"ungelesen": True, "anzahl": 5})
    return {
        "datum": start.date().isoformat(),
        "offline": offline,
        "termine": [{**t, "start": t["start"].astimezone(TZ).strftime("%H:%M"),
                     "ende": t["ende"].astimezone(TZ).strftime("%H:%M") if t["ende"] else None} for t in termine],
        "aufgaben": [{**a, "faellig": str(a["faellig"]) if a["faellig"] else None} for a in aufgaben],
        "mails": mails,
    }


@app.get("/api/timeline", dependencies=[Depends(auth)])
async def api_timeline(request: Request, tag: str | None = None):
    return await request.app.state.hub.kern.memory_timeline(tag)


@app.get("/api/freigaben", dependencies=[Depends(auth)])
async def api_freigaben(request: Request):
    """Freigaben der letzten 24 h. Abgelaufene offene zeigen den Status "abgelaufen"."""
    rows = await request.app.state.store.alle(
        """SELECT id, tool, arguments, begruendung, vorschau, kanal, belastet, ergebnis, erstellt, laeuft_ab, entschieden,
                  CASE WHEN status = 'offen' AND laeuft_ab <= now() THEN 'abgelaufen' ELSE status END AS status
           FROM confirmations WHERE erstellt > now() - interval '1 day' ORDER BY erstellt DESC LIMIT 20""")
    return [{**r, "id": str(r["id"]), "erstellt": zeit(r["erstellt"]), "laeuft_ab": zeit(r["laeuft_ab"]),
             "laeuft_ab_iso": r["laeuft_ab"].isoformat(),
             "entschieden": zeit(r["entschieden"]) if r["entschieden"] else None} for r in rows]


@app.get("/api/freigaben/pin", dependencies=[Depends(auth)])
async def api_freigabe_pin(request: Request):
    """Kann die App freigeben? (PIN eingerichtet, gerade gesperrt bis ...)"""
    pin: AppPin = request.app.state.app_pin
    await pin.laden()
    request.app.state.hub.app_freigabe = pin.eingerichtet
    bis = AppPin.gesperrt(await pin.zustand()) if pin.eingerichtet else None
    code = await pin.code_offen()
    return {"eingerichtet": pin.eingerichtet, "gesperrt_bis": bis.isoformat() if bis else None,
            "einrichtungscode_offen_bis": code.isoformat() if code else None}


@app.post("/api/freigabe-pin", dependencies=[Depends(auth)])
async def api_freigabe_pin_setzen(request: Request, body: dict):
    """Freigabe-PIN in der App festlegen oder ändern.

    {"einrichtungscode": "ABCD-EFGH", "neue_pin": "..."}  erste PIN oder vergessene PIN; der Code steht nur auf
                                                         der Box (start.sh, qulsi.sh status/pin), 15 min, einmal
    {"alte_pin": "...", "neue_pin": "..."}                PIN ändern
    Der App-Schlüssel allein reicht also nie. Falsche Versuche zählen wie beim Freigeben (Sperre nach 5)."""
    st = request.app.state
    pin: AppPin = st.app_pin
    await pin.laden()
    neue = str(body.get("neue_pin") or "")
    code, alte = str(body.get("einrichtungscode") or ""), str(body.get("alte_pin") or "")
    if not AppPin.pin_gueltig(neue):
        raise HTTPException(400, "Die neue PIN muss aus 6 bis 12 Ziffern bestehen.")
    if not code and not alte:
        raise HTTPException(400, "einrichtungscode oder alte_pin fehlt")
    weg = "einrichtungscode" if code else "alte_pin"
    try:
        if code:
            await pin.code_einloesen(code)
        else:
            await pin.pruefen(alte)
    except PinFehler as f:
        await _pin_protokoll(st, f"pin_{weg}_falsch" if f.status == 403 else f"pin_{weg}_gesperrt", f.text)
        return JSONResponse({"fehler": f.text, **f.daten}, status_code=f.status)
    erste = not pin.eingerichtet
    await pin.setzen(neue, f"app_{weg}")
    st.hub.app_freigabe = True
    await _pin_protokoll(st, "pin_gesetzt" if erste else "pin_geaendert", f"über {weg}")
    await st.store.ereignis("sicherheit", "Freigabe-PIN festgelegt" if erste else "Freigabe-PIN geändert")
    return {"eingerichtet": True, "erste": erste}


async def _pin_protokoll(st, entscheid: str, text: str) -> None:
    """Jede PIN-Änderung und jeder falsche Versuch steht im audit_log (ohne PIN oder Code)."""
    try:
        async with await st.store.conn() as conn:
            await conn.execute(
                """INSERT INTO audit_log (tool, connector, stufe, entscheid, kanal, belastet, arguments, ergebnis)
                   VALUES ('freigabe.pin', 'core', 'high', %s, 'app', false, '{}'::jsonb, %s)""", (entscheid, text))
    except Exception:  # noqa: BLE001
        log.exception("audit_log für die Freigabe-PIN konnte nicht geschrieben werden")


@app.post("/api/freigaben/{fid}", dependencies=[Depends(auth)])
async def api_freigabe_entscheiden(fid: str, request: Request, body: dict):
    """Erlauben oder Ablehnen aus der App: {"entscheidung": "erlauben"|"ablehnen", "pin": "..."}"""
    entscheidung = body.get("entscheidung")
    if entscheidung not in ("erlauben", "ablehnen"):
        raise HTTPException(400, "entscheidung muss erlauben oder ablehnen sein")
    st = request.app.state
    ja = entscheidung == "erlauben"
    if ja:
        try:
            await st.app_pin.pruefen(str(body.get("pin") or ""))
        except PinFehler as f:
            if f.status != 403 or st.app_pin.eingerichtet:  # falsche PIN oder Sperre: protokollieren
                row = await st.store.eine("SELECT * FROM confirmations WHERE id::text = %s", (fid,))
                if row:
                    await st.hub.freigabe_protokoll(row, "app_pin_falsch" if f.status == 403 else "app_pin_gesperrt")
                await st.store.ereignis("freigabe", f"App-Freigabe verweigert: {f.text}")
                if f.daten.get("neu_gesperrt"):
                    try:
                        await st.notifier.senden("Freigabe-PIN gesperrt",
                                                 "In der App wurde mehrmals eine falsche Freigabe-PIN eingegeben. "
                                                 "Freigeben geht vorübergehend nur übers Handy.")
                    except Exception:  # noqa: BLE001
                        log.warning("Warnung an ntfy fehlgeschlagen")
            return JSONResponse({"fehler": f.text, **f.daten}, status_code=f.status)
    row = await st.freigaben.entscheiden_app(fid, ja)
    if row is None:
        raise HTTPException(409, "Freigabe unbekannt, abgelaufen oder schon entschieden")
    return JSONResponse(await _entscheidung_ausfuehren(st, row, ja, "app"))


@app.get("/api/events", dependencies=[Depends(auth_lesen)])
async def api_events(request: Request):
    """Event Bus live: jedes neue Ereignis (Postgres NOTIFY qulsi_events) als Server-Sent Event."""
    store: Store = request.app.state.store

    async def strom():
        async with await store.conn() as conn:
            await conn.execute("LISTEN qulsi_events")
            yield ": verbunden\n\n"
            while not await request.is_disconnected():
                async for n in conn.notifies(timeout=15):
                    yield f"data: {n.payload}\n\n"
                yield ": ping\n\n"

    return StreamingResponse(strom(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/status", dependencies=[Depends(auth)])
async def api_status(request: Request):
    hub: Hub = request.app.state.hub
    return {
        "offline": await hub.offline(),
        "connectoren": {n: {"titel": m.titel, "erreichbar": hub.erreichbar.get(n, False), "faehigkeiten": m.capabilities,
                            "internet": m.allowed} for n, m in hub.manifeste.items()},
        "werkzeuge": {w.faehigkeit: {"connector": w.connector, "stufe": hub.policy.stufe(w.faehigkeit).value,
                                     "offline": w.offline} for w in hub.werkzeuge.values()},
        "modelle": request.app.state.agent.router.modelle,
    }


@app.get("/api/connectoren", dependencies=[Depends(auth)])
async def api_connectoren(request: Request):
    """Je Connector: Modus, erreichbar, Quarantäne, Signatur, nicht auflösbare Domains, Werkzeuge."""
    hub: Hub = request.app.state.hub
    await hub.sicherheit_pruefen()
    return hub.connector_uebersicht()


@app.post("/api/connectoren/{name}", dependencies=[Depends(auth)])
async def api_connector_modus(request: Request, name: str, body: dict):
    """{"modus": "an" | "nur_lesen" | "aus"}"""
    try:
        await request.app.state.hub.modus_setzen(name, str(body.get("modus", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/connectoren/{name}/freigeben", dependencies=[Depends(auth)])
async def api_connector_freigeben(request: Request, name: str):
    """Quarantäne aufheben (der Egress-Proxy lässt den Connector wieder raus)."""
    return {"freigegeben": await request.app.state.hub.quarantaene_aufheben(name)}


@app.get("/api/objekt/{schluessel:path}", dependencies=[Depends(auth_lesen)])
async def api_objekt(request: Request, schluessel: str):
    """Datei aus dem Object Storage, z. B. Bildausschnitt einer Sichtung."""
    try:
        daten = await asyncio.to_thread(request.app.state.objekte.holen, schluessel)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "Nicht gefunden") from None
    typ = "image/jpeg" if schluessel.endswith((".jpg", ".jpeg")) else "application/octet-stream"
    return Response(daten, media_type=typ)


@app.post("/werkzeuge/neu-laden", dependencies=[Depends(auth)])
async def neu_laden(request: Request):
    await request.app.state.hub.laden()
    return {"werkzeuge": sorted(request.app.state.hub.werkzeuge)}


# ---------------------------------------------------------------------------
# OpenAI-kompatibel
# ---------------------------------------------------------------------------


@app.get("/v1/models", dependencies=[Depends(auth)])
async def models():
    now = int(time.time())
    return {"object": "list", "data": [{"id": m, "object": "model", "created": now, "owned_by": "qulsi"} for m in MODELLE]}


@app.post("/v1/chat/completions", dependencies=[Depends(auth)])
async def chat_completions(request: Request, x_qulsi_kanal: str | None = Header(default=None)):
    body = await request.json()
    model_name = body.get("model", "qulsi")
    kanal, erzwingen = MODELLE.get(model_name, ("chat", None))
    kanal = x_qulsi_kanal or kanal
    verlauf = [{"role": m.get("role"), "content": m.get("content")} for m in body.get("messages", [])]
    a, text, ctx = await _gespraech(request, verlauf, kanal, erzwingen)

    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    if body.get("stream"):
        return StreamingResponse(_stream(cid, created, model_name, text), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "qulsi": _info(a, ctx),
        }
    )


async def _stream(cid: str, created: int, model: str, text: str):
    def chunk(delta: dict, finish: str | None = None) -> str:
        data = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant"})
    for i in range(0, len(text), 64):
        yield chunk({"content": text[i : i + 64]})
    yield chunk({}, "stop")
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Freigaben aus der Handy-App
# ---------------------------------------------------------------------------

SEITE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QuLSI Freigabe</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;background:#000000;color:#e8e8e8}}
pre{{background:#1b1e25;padding:.8rem;white-space:pre-wrap;border-radius:6px}}
button{{font-size:1.1rem;padding:.7rem 1.4rem;margin:.3rem;border-radius:8px;border:0}}
.ja{{background:#1f7a4d;color:#fff}}.nein{{background:#444;color:#fff}}.warn{{color:#f0a24b}}</style></head><body>
{inhalt}</body></html>"""


@app.get("/bestaetigung/{fid}", response_class=HTMLResponse)
async def freigabe_seite(fid: str, request: Request, token: str = Query(...)):
    row = await request.app.state.freigaben.laden(fid, token)
    if not row:
        raise HTTPException(404, "Unbekannte Freigabe")
    args = html.escape(json.dumps(row["arguments"], ensure_ascii=False, indent=2))
    warn = '<p class="warn">Im Gespräch standen Inhalte aus Mail, Web oder Dateien.</p>' if row["belastet"] else ""
    if row["status"] == "offen":
        knoepfe = f"""<form method="post" action="/bestaetigung/{fid}?token={html.escape(token)}">
        <button class="ja" name="entscheid" value="ja">Freigeben</button>
        <button class="nein" name="entscheid" value="nein">Ablehnen</button></form>"""
    else:
        knoepfe = f"<p>Status: <b>{html.escape(row['status'])}</b></p><pre>{html.escape(row['ergebnis'] or '')}</pre>"
    vorschau = f"<pre>{html.escape(row['vorschau'])}</pre>" if row.get("vorschau") else ""
    inhalt = f"<h2>{html.escape(row['tool'])}</h2>{warn}{vorschau}<pre>{args}</pre>{knoepfe}"
    return SEITE.format(inhalt=inhalt)


@app.post("/bestaetigung/{fid}")
async def freigabe_entscheiden(fid: str, request: Request, token: str = Query(...), entscheid: str | None = Query(None)):
    if entscheid is None:  # Formular aus der Webseite
        form = await request.form()
        entscheid = str(form.get("entscheid", ""))
    if entscheid not in ("ja", "nein"):
        raise HTTPException(400, "entscheid muss ja oder nein sein")
    st = request.app.state
    row = await st.freigaben.entscheiden(fid, token, entscheid == "ja")
    if row is None:
        raise HTTPException(409, "Freigabe unbekannt, abgelaufen oder schon entschieden")
    return JSONResponse(await _entscheidung_ausfuehren(st, row, entscheid == "ja", "handy"))


async def _entscheidung_ausfuehren(st, row: dict, ja: bool, quelle: str) -> dict:
    """Gemeinsamer Weg für Handy und App: protokollieren, bei Erlauben ausführen und Ergebnis melden."""
    await st.hub.freigabe_protokoll(row, f"{quelle}_{'erlaubt' if ja else 'abgelehnt'}")
    wo = "in der App" if quelle == "app" else "am Handy"
    args = row["arguments"] if isinstance(row["arguments"], dict) else json.loads(row["arguments"])
    if not ja:
        if row["tool"] == stimmprobe.WERKZEUG:
            stimmprobe.verwerfen(args)
        elif konten.zustaendig(row["tool"]):
            konten.abgelehnt(st, args)
        await st.store.ereignis("freigabe", f"Abgelehnt {wo}: {row['tool']}")
        return {"status": "abgelehnt"}
    await st.store.ereignis("freigabe", f"Erlaubt {wo}: {row['tool']}")
    if row["tool"] == stimmprobe.WERKZEUG:  # kein Werkzeug des Sprachmodells, nur aus der App
        ergebnis = await stimmprobe.uebernehmen(args, st.settings.tts_url)
        await st.hub.freigabe_protokoll(row, "ausgefuehrt_nach_freigabe", ergebnis)
        e = getattr(st, "einstellungen", None)
        jetzt = (e.werte.get("stimme", {}).get("stimme") or "") if e else ""
        ziel = "chatterbox_eigene" if jetzt.startswith("chatterbox") else "eigene"
        if e and not ergebnis.startswith("Fehler") and jetzt not in ("", ziel):
            await e.setzen("stimme", {"stimme": ziel})  # neue Probe freigegeben: die will man auch hören
    elif konten.zustaendig(row["tool"]):  # Konten und Zugangsdaten aus der App (konten.py), nie vom Sprachmodell
        ergebnis = await konten.nach_freigabe(st, row)
    else:
        ergebnis = await st.hub.nach_freigabe(row)
    fid = str(row["id"])
    status = "fehler" if ergebnis.startswith("Fehler") else "freigegeben"
    await st.freigaben.ergebnis(fid, status, ergebnis)
    try:
        await st.notifier.senden(f"Erledigt: {row['tool']}" if status == "freigegeben" else f"Fehlgeschlagen: {row['tool']}",
                                 ergebnis[:500])
    except Exception:  # noqa: BLE001
        log.warning("Rückmeldung an ntfy fehlgeschlagen")
    return {"status": status, "ergebnis": ergebnis}
