"""Eigene Stimme aus der App: Stimmprobe aufnehmen oder hochladen, übernehmen erst nach Freigabe.

- GET  /api/stimme/probe   kann das TTS-Modell Stimmen klonen? gibt es schon eine eigene Stimme, eine offene Probe?
- POST /api/stimme/probe   WAV (5 bis 15 s) plus gesprochener Text. Legt eine Freigabe "stimme.probe" an
                           (Handy oder App mit Freigabe-PIN). Erst danach wird sie zu stimme.wav/stimme.txt.

Die Probe wartet bis dahin in <STIMME_DIR>/ausstehend/. Eine Stimme klonen können Qwen3-TTS, CosyVoice 3
und Chatterbox, Piper nicht. Eine gestohlene App-Sitzung kann so keine fremde Stimme unterschieben.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import uuid
import wave
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

log = logging.getLogger(__name__)

WERKZEUG = "stimme.probe"
KLONEN = {"qwen3-tts", "cosyvoice3", "chatterbox", "fake"}  # fake: Teststack
MIN_SEKUNDEN, MAX_SEKUNDEN = 5.0, 15.0
SPIELRAUM = 1.0  # die App stoppt bei 15 s, die fertige Datei ist oft ein paar Zehntel länger
MAX_BYTES = 6 * 1024 * 1024

router = APIRouter()


def ordner() -> Path:
    return Path(os.environ.get("STIMME_DIR", "/stimme"))


def wav_pruefen(daten: bytes) -> float:
    """Liefert die Dauer in Sekunden oder wirft HTTPException mit verständlichem Grund."""
    if len(daten) > MAX_BYTES:
        raise HTTPException(413, "Die Aufnahme ist zu groß.")
    try:
        with wave.open(io.BytesIO(daten)) as w:
            kanaele, breite, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise HTTPException(415, "Bitte eine WAV-Datei (PCM, 16 Bit) schicken.") from exc
    if kanaele not in (1, 2) or breite != 2 or not 16000 <= rate <= 48000:
        raise HTTPException(415, "Bitte WAV mit 16 Bit, Mono oder Stereo, 16 bis 48 kHz.")
    sekunden = frames / rate
    if sekunden < MIN_SEKUNDEN:
        raise HTTPException(422, f"Die Probe ist nur {sekunden:.1f} s lang. Bitte mindestens 5 Sekunden sprechen.")
    if sekunden > MAX_SEKUNDEN + SPIELRAUM:
        raise HTTPException(422, f"Die Probe ist {sekunden:.1f} s lang. Bitte höchstens 15 Sekunden.")
    return sekunden


async def faehigkeit(voice_url: str) -> dict:
    """Fragt den Voice-Dienst, welches TTS-Modell läuft."""
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            h = (await c.get(f"{voice_url}/health")).json()
    except Exception:  # noqa: BLE001
        return {"klonen": False, "modell": None, "hinweis": "Die Sprachausgabe ist gerade nicht erreichbar."}
    modell = h.get("tts_modell") if str(h.get("tts", "")).startswith("openai") else h.get("tts")
    if modell in KLONEN:
        return {"klonen": True, "modell": modell, "hinweis": None}
    if os.environ.get("QULSI_LAUFZEIT") == "runpod":
        hinweis = ("Die Sprachausgabe läuft gerade mit Piper, das keine Stimme nachsprechen kann. Qwen3-TTS "
                   "richtet start.sh auf dem Pod ein; danach steht die eigene Stimme hier bereit.")
    else:
        hinweis = ("Die Sprachausgabe läuft mit Piper. Piper kann keine Stimme nachsprechen, dafür "
                   "Qwen3-TTS, CosyVoice 3 oder Chatterbox einstellen (TTS_MODELL in .env).")
    return {"klonen": False, "modell": modell, "hinweis": hinweis}


async def text_erkennen(voice_url: str, daten: bytes) -> str:
    """Was in der Probe gesprochen wird, per Spracherkennung. Leer, wenn das nicht klappt."""
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            r = await c.post(f"{voice_url}/stt", files={"audio": ("probe.wav", daten, "audio/wav")})
            r.raise_for_status()
            text = re.sub(r"\s+", " ", str(r.json().get("text") or "")).strip()[:500]
    except Exception as exc:  # noqa: BLE001
        log.info("Text der Stimmprobe nicht erkannt (%s), klone nur die Sprecher-Merkmale", type(exc).__name__)
        return ""
    return text if len(text.split()) >= 3 else ""


@router.get("/api/stimme/probe")
async def probe_status(request: Request):
    st = request.app.state
    offen = await st.store.eine(
        """SELECT id FROM confirmations WHERE tool = %s AND status = 'offen' AND laeuft_ab > now()
           ORDER BY erstellt DESC LIMIT 1""", (WERKZEUG,))
    return {**await faehigkeit(st.settings.voice_url), "vorhanden": (ordner() / "stimme.wav").is_file(),
            "offen": str(offen["id"]) if offen else None}


@router.post("/api/stimme/probe")
async def probe_hochladen(request: Request, audio: UploadFile | None = File(None), text: str = Form("")):
    st = request.app.state
    f = await faehigkeit(st.settings.voice_url)
    if not f["klonen"]:
        raise HTTPException(409, f["hinweis"])
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 500:
        raise HTTPException(422, "Der Text ist zu lang (höchstens 500 Zeichen).")
    if audio is None:
        raise HTTPException(400, "Keine Aufnahme angekommen. Bitte noch einmal aufnehmen.")
    daten = await audio.read(MAX_BYTES + 1)
    if not daten:
        raise HTTPException(400, "Die Aufnahme ist leer. Bitte noch einmal aufnehmen.")
    sekunden = wav_pruefen(daten)
    erkannt = False
    if not text:  # mit Text klont Qwen3-TTS deutlich ähnlicher (Vorlage statt nur Sprecher-Merkmale)
        text = await text_erkennen(st.settings.voice_url, daten)
        erkannt = bool(text)
    probe = str(uuid.uuid4())
    warte = ordner() / "ausstehend"
    try:
        warte.mkdir(parents=True, exist_ok=True)
        (warte / f"{probe}.wav").write_bytes(daten)
        (warte / f"{probe}.txt").write_text(text, encoding="utf-8")
    except OSError as exc:
        log.error("Stimmprobe nicht speicherbar: %s", exc)
        raise HTTPException(500, "Die Probe konnte auf der Box nicht gespeichert werden.") from exc
    vorschau = (f"Neue Stimme für die Sprachausgabe ({sekunden:.1f} s)\n"
                f"Text{' (erkannt)' if erkannt else ''}: „{text or 'ohne Text'}“")
    args = {"probe": probe, "text": text, "sekunden": round(sekunden, 1), "text_erkannt": erkannt}
    fr = await st.freigaben.anlegen(WERKZEUG, args, "app", False, "Stimmprobe aus der App", vorschau)
    await st.hub.freigabe_protokoll({"tool": WERKZEUG, "arguments": args, "kanal": "app", "belastet": False},
                                    "bestaetigung", f"Freigabe {fr.id}")
    await st.store.ereignis("freigabe", f"Neue Stimmprobe wartet auf Freigabe ({sekunden:.1f} s)")
    return {"status": "freigabe_noetig", "freigabe": fr.id, "handy": fr.benachrichtigt, "sekunden": round(sekunden, 1)}


def _dateien(probe: str) -> tuple[Path, Path]:
    uuid.UUID(probe)  # nur eigene Namen, kein Pfad aus der Datenbank
    warte = ordner() / "ausstehend"
    return warte / f"{probe}.wav", warte / f"{probe}.txt"


def verwerfen(args: dict) -> None:
    for p in _dateien(args["probe"]):
        p.unlink(missing_ok=True)


async def uebernehmen(args: dict, tts_url: str) -> str:
    """Nach der Freigabe: Probe wird zur Stimme (die alte bleibt als stimme.vorher.*), TTS lädt neu."""
    wav, txt = _dateien(args["probe"])
    if not wav.is_file():
        return "Fehler: Die Stimmprobe ist nicht mehr da."
    ziel = ordner()
    for endung in ("wav", "txt"):
        alt = ziel / f"stimme.{endung}"
        if alt.is_file():
            alt.replace(ziel / f"stimme.vorher.{endung}")
    wav.replace(ziel / "stimme.wav")
    if txt.is_file():
        txt.replace(ziel / "stimme.txt")
    if tts_url:
        # Nur ein Hinweis an den TTS-Dienst, Grafikspeicher gleich freizugeben. Nötig ist er nicht: der Dienst
        # sieht vor jedem Satz, dass sich stimme.wav geändert hat, und lädt dann mit der neuen Stimme.
        for versuch in range(2):
            try:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                    (await c.post(f"{tts_url}/stimme/neu")).raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                if versuch:
                    log.info("TTS-Dienst gerade nicht erreichbar (%s), die neue Stimme gilt ab dem nächsten Satz",
                             type(exc).__name__)
                else:
                    await asyncio.sleep(2)
    return f"Neue Stimme übernommen ({args.get('sekunden', '?')} s)."
