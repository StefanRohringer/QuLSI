"""Voice-Dienst: Spracherkennung (STT) und Sprachausgabe (TTS), beide austauschbar.

Pipeline:  Mikrofon -> VAD -> Rauschunterdrückung -> STT -> QuLSI -> TTS -> Lautsprecher
- Beamforming und Echounterdrückung macht das Mikrofon-Array (z. B. XMOS XVF3800) selbst,
  Rauschunterdrückung der Browser bzw. das Array. VAD (Silero) läuft hier in faster-whisper.
- STT_PROVIDER: whisper (faster-whisper, Standard) | fake (Tests)
- TTS_PROVIDER: piper (lokal, Standard) | openai (jeder Server mit /v1/audio/speech,
                z. B. Qwen3-TTS) | fake (Tests)

Zone 1 (KI, ohne Internet). Modelle liegen unter /models/voice.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import struct
import tempfile
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from .bei_bedarf import BeiBedarf, freigabe_schleife

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice")

MODELLE = Path(os.environ.get("VOICE_MODELS", "/models/voice"))


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


class WhisperSTT:
    name = "whisper"

    def __init__(self):
        from faster_whisper import WhisperModel

        modell = os.environ.get("STT_MODEL", "large-v3-turbo")
        lokal = MODELLE / f"faster-whisper-{modell}"
        geraet = os.environ.get("STT_DEVICE", "auto")
        typ = os.environ.get("STT_COMPUTE", "default")
        self.model = WhisperModel(str(lokal) if lokal.exists() else modell, device=geraet, compute_type=typ,
                                  download_root=str(MODELLE))
        self.sprache = os.environ.get("STT_LANGUAGE", "de")

    def erkennen(self, daten: bytes, endung: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=endung) as f:
            f.write(daten)
            f.flush()
            segmente, _ = self.model.transcribe(f.name, language=self.sprache, vad_filter=True, beam_size=1)
            return " ".join(s.text.strip() for s in segmente).strip()


class FakeSTT:
    """Für Tests: das "Audio" ist der Text selbst."""

    name = "fake"

    def erkennen(self, daten: bytes, endung: str) -> str:
        return daten.decode("utf-8", errors="ignore").strip()


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


class PiperTTS:
    name = "piper"

    def __init__(self):
        from piper import PiperVoice

        stimme = os.environ.get("TTS_VOICE", "de_DE-thorsten-high")
        pfad = MODELLE / f"{stimme}.onnx"
        if not pfad.exists():
            raise FileNotFoundError(f"Piper-Stimme fehlt: {pfad} (scripts/download-models.sh)")
        self.voice = PiperVoice.load(str(pfad))

    async def sprechen(self, text: str, stimme: str | None) -> tuple[bytes, str]:
        def run() -> bytes:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                self.voice.synthesize_wav(text, wf)
            return buf.getvalue()

        return await asyncio.to_thread(run), "audio/wav"


class OpenAITTS:
    """Jeder Server mit OpenAI-Schnittstelle /v1/audio/speech, Standard: der TTS-Dienst (tts/) mit Qwen3-TTS."""

    name = "openai"

    def __init__(self):
        self.url = os.environ["TTS_URL"].rstrip("/")
        self.modell = os.environ.get("TTS_MODEL") or "qwen3-tts"
        self.stimme = os.environ.get("TTS_SPRECHER") or "default"
        # Antwortet der TTS-Dienst so lange nicht, spricht der Ersatz (Piper) den Satz
        self.timeout = float(os.environ.get("TTS_TIMEOUT", "25"))
        # Zweiter TTS-Dienst mit Chatterbox (eigene Bibliotheken): Stimmen "chatterbox..." gehen dorthin
        self.chatterbox = (os.environ.get("TTS_URL_CHATTERBOX") or "").rstrip("/")

    async def sprechen(self, text: str, stimme: str | None) -> tuple[bytes, str]:
        url, modell = self.url, self.modell
        if self.chatterbox and (stimme or "").lower().startswith("chatterbox"):
            url, modell = self.chatterbox, "chatterbox"
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as c:
            r = await c.post(f"{url}/audio/speech", json={
                "model": modell, "input": text, "voice": stimme or self.stimme, "response_format": "wav"})
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "audio/wav")


class FakeTTS:
    """Für Tests: kurzer Ton, Länge abhängig vom Text."""

    name = "fake"

    async def sprechen(self, text: str, stimme: str | None) -> tuple[bytes, str]:
        rate, dauer = 16000, min(2.0, 0.05 + len(text) / 200)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
                                    for i in range(int(rate * dauer))))
        return buf.getvalue(), "audio/wav"


class MitErsatz:
    """Erst die eigentliche Sprachausgabe (z. B. CosyVoice im TTS-Dienst), bei einem Fehler Piper."""

    def __init__(self, haupt, ersatz):
        self.haupt, self.ersatz = haupt, ersatz
        self.name = f"{haupt.name}+{ersatz.name}"

    async def sprechen(self, text: str, stimme: str | None) -> tuple[bytes, str]:
        try:
            return await self.haupt.sprechen(text, stimme)
        except Exception as exc:  # TTS-Dienst aus, Modell fehlt, Grafikspeicher voll
            log.warning("%s fehlgeschlagen (%s), nehme %s", self.haupt.name, exc, self.ersatz.name)
            return await self.ersatz.sprechen(text, None)


STT = {"whisper": WhisperSTT, "fake": FakeSTT}
TTS = {"piper": PiperTTS, "openai": OpenAITTS, "fake": FakeTTS}


def tts_laden(name: str, ersatz_name: str):
    haupt = TTS[name]()
    if not ersatz_name or ersatz_name == name:
        return haupt
    try:
        return MitErsatz(haupt, TTS[ersatz_name]())
    except Exception as exc:
        log.warning("Ersatz-Sprachausgabe %s nicht verfügbar: %s", ersatz_name, exc)
        return haupt


@asynccontextmanager
async def lifespan(app: FastAPI):
    stt_name = os.environ.get("STT_PROVIDER", "whisper")
    tts_name = os.environ.get("TTS_PROVIDER", "piper")
    # Whisper kommt erst bei der ersten Aufnahme in den Grafikspeicher und geht nach
    # STT_LEERLAUF_SEKUNDEN ohne Nutzung wieder raus (0 = immer geladen).
    leerlauf = float(os.environ.get("STT_LEERLAUF_SEKUNDEN", "600"))
    app.state.stt = await asyncio.to_thread(BeiBedarf, f"STT {stt_name}", STT[stt_name], leerlauf)
    app.state.stt.name = stt_name
    freigabe = asyncio.create_task(freigabe_schleife([app.state.stt]))
    app.state.tts = await asyncio.to_thread(tts_laden, tts_name, os.environ.get("TTS_ERSATZ", ""))
    app.state.stt_lock = asyncio.Lock()
    log.info("Voice läuft: STT=%s, TTS=%s", stt_name, tts_name)
    yield
    freigabe.cancel()


app = FastAPI(title="QuLSI Voice", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "stt": app.state.stt.name, "tts": app.state.tts.name,
            "tts_modell": os.environ.get("TTS_MODEL") or None, "ersatz": os.environ.get("TTS_ERSATZ") or None}


@app.post("/stt")
async def stt(audio: UploadFile = File(...)):
    daten = await audio.read()
    if not daten:
        raise HTTPException(400, "Leere Aufnahme")
    endung = Path(audio.filename or "a.webm").suffix or ".webm"
    async with app.state.stt_lock:
        text = await asyncio.to_thread(app.state.stt.erkennen, daten, endung)
    return {"text": text}


@app.post("/tts")
async def tts(body: dict):
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Kein Text")
    try:
        audio, typ = await app.state.tts.sprechen(text[:2000], body.get("stimme"))
    except Exception as exc:  # noqa: BLE001  ohne Ersatz oder auch der Ersatz scheitert: klare Meldung statt 500
        log.warning("Sprachausgabe fehlgeschlagen: %s", exc)
        raise HTTPException(503, "Die Sprachausgabe antwortet gerade nicht.") from None
    return Response(audio, media_type=typ)
