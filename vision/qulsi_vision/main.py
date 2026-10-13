"""Vision-Dienst: dauerhafte Objekterkennung auf den Kameras, unabhängig vom Sprachmodell.

Zone 1 (KI, ohne Internet). Erreichbar sind nur die Kameras im Heimnetz.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import httpx
import numpy as np
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .geometry import ausschnitt, normieren
from .bei_bedarf import BeiBedarf, freigabe_schleife
from .models import lade_detector, lade_embedder
from .pipeline import Wahrnehmung
from .store import Store, objekt_holen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vision")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://litellm:4000/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "qulsi")
VISION_MODEL = os.environ.get("VISION_MODEL", "bonsai-vision")


def lade_config(pfad: str) -> dict:
    text = os.path.expandvars(Path(pfad).read_text(encoding="utf-8"))
    cfg = yaml.safe_load(text)
    for k in cfg.get("kameras", []):
        url = str(k.get("url") or "")
        if not url or re.search(r"\$\{?\w+", url):
            log.warning("Kamera %s hat keine URL und wird übersprungen", k["name"])
            k["url"] = ""
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = lade_config(os.environ.get("CAMERAS_CONFIG", "/config/cameras.yaml"))
    store = Store(os.environ["DATABASE_URL"])
    dauerbetrieb = os.environ.get("DAUERBETRIEB", "true").lower() == "true"
    # Ohne Live-Kameras werden Detektor und Bildvektor-Modell erst beim ersten Foto geladen
    # und nach MODELL_LEERLAUF_SEKUNDEN ohne Nutzung wieder aus dem Grafikspeicher genommen.
    leerlauf = 0 if dauerbetrieb else float(os.environ.get("MODELL_LEERLAUF_SEKUNDEN", "600"))
    vokabular = list(cfg["vokabular"])
    detector = await asyncio.to_thread(BeiBedarf, "Detektor", lambda: lade_detector(vokabular), leerlauf)
    embedder = await asyncio.to_thread(BeiBedarf, "Bildvektor-Modell", lade_embedder, leerlauf)
    w = Wahrnehmung(cfg, store, detector, embedder)
    await w.start()
    tasks = [asyncio.create_task(freigabe_schleife([detector, embedder]))]
    if dauerbetrieb:
        tasks += [asyncio.create_task(w.kamera_schleife(n)) for n in w.kameras]
    tasks.append(asyncio.create_task(w.aufraeumen_schleife()))
    if os.environ.get("BLE", "true").lower() == "true" and cfg.get("bluetooth_raeume"):
        from .ble import ble_schleife

        tasks.append(asyncio.create_task(ble_schleife(store, cfg["bluetooth_raeume"])))
    app.state.w = w
    log.info("Vision läuft mit Kameras: %s", ", ".join(w.kameras) or "keine")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="QuLSI Vision", lifespan=lifespan)


def _bild(daten: bytes) -> np.ndarray:
    bild = cv2.imdecode(np.frombuffer(daten, np.uint8), cv2.IMREAD_COLOR)
    if bild is None:
        raise HTTPException(400, "Bild konnte nicht gelesen werden")
    return bild


@app.get("/health")
async def health():
    w: Wahrnehmung = app.state.w
    return {"ok": True, "kameras": sorted(w.kameras), "privacy": await w.store.privacy(),
            "modelle_geladen": {"detektor": w.detector.geladen, "bildvektor": w.embedder.geladen}}


@app.post("/snapshot/{kamera}")
async def snapshot(kamera: str):
    return await app.state.w.schnappschuss(kamera)


@app.post("/analyze")
async def analyze(body: dict):
    """Für vision.analyze: Bonsai (Bildmodell) schaut sich einen Ausschnitt oder ein aktuelles Bild an."""
    w: Wahrnehmung = app.state.w
    frage = str(body.get("frage") or "Was ist auf dem Bild zu sehen?")
    jpg: bytes | None = None
    if body.get("sighting_id"):
        key = await w.store.ausschnitt_key(int(body["sighting_id"]))
        jpg = objekt_holen(key) if key else None
        if jpg is None:
            return {"antwort": "Zu dieser Sichtung gibt es keinen Bildausschnitt mehr."}
    elif body.get("kamera"):
        if await w.store.privacy():
            return {"antwort": "Privacy-Modus ist an, Kameras werden nicht ausgewertet."}
        k = w.kameras.get(str(body["kamera"]))
        bild = await asyncio.to_thread(k.bild) if k else None
        if bild is None:
            return {"antwort": f"Kamera {body['kamera']} liefert gerade kein Bild."}
        jpg = cv2.imencode(".jpg", bild, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    else:
        raise HTTPException(400, "sighting_id oder kamera angeben")
    daten = base64.b64encode(jpg).decode()
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        r = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": VISION_MODEL, "messages": [{"role": "user", "content": [
                {"type": "text", "text": f"{frage} Antworte kurz auf Deutsch."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{daten}"}}]}]},
        )
    if r.status_code >= 400:
        return {"antwort": f"Bildmodell nicht erreichbar ({r.status_code})."}
    return {"antwort": r.json()["choices"][0]["message"]["content"]}


@app.post("/analyse/{kamera}")
async def analyse(kamera: str, bild: UploadFile = File(...)):
    """Wertet ein hochgeladenes Bild aus, als käme es von dieser Kamera (zum Testen der Zonen)."""
    w: Wahrnehmung = app.state.w
    if kamera not in w.zonen:
        raise HTTPException(404, f"Unbekannte Kamera {kamera}")
    return {"kamera": kamera, "gegenstaende": await w.analysieren(kamera, _bild(await bild.read()))}


@app.post("/anlernen")
async def anlernen(
    name: str = Form(...),
    typ: str = Form(...),
    aliases: str = Form(""),
    besitzer: str = Form(""),
    ble_tag: str = Form(""),
    kamera: str = Form(""),
    bilder: list[UploadFile] | None = File(None),
):
    """Lernt einen persönlichen Gegenstand an.

    Mehrere Fotos aus verschiedenen Blickwinkeln (oder ein Bild einer Kamera) werden
    nach dem Begriff `typ` durchsucht, der Ausschnitt wird zum Fingerabdruck gemittelt.
    """
    w: Wahrnehmung = app.state.w
    if typ not in w.vokabular:
        raise HTTPException(400, f"typ muss ein Begriff aus dem Vokabular sein: {sorted(w.vokabular)}")
    quellen: list[np.ndarray] = [_bild(await b.read()) for b in (bilder or [])]
    if kamera:
        k = w.kameras.get(kamera)
        frame = await asyncio.to_thread(k.bild) if k else None
        if frame is None:
            raise HTTPException(400, f"Kamera {kamera} liefert kein Bild")
        quellen.append(frame)

    vektoren, gefunden = [], 0
    for bild in quellen:
        async with w.modell_lock:
            dets = await asyncio.to_thread(w.detector.erkennen, bild, kamera or "anlernen", 0.05)
        passend = sorted((d for d in dets if d.label == typ), key=lambda d: d.score, reverse=True)
        crop = ausschnitt(bild, passend[0].box) if passend else bild
        gefunden += bool(passend)
        async with w.modell_lock:
            vektoren.append(await asyncio.to_thread(w.embedder.vektor, crop))

    fp = normieren(np.mean(vektoren, axis=0)) if vektoren else None
    oid = await w.store.objekt_anlernen(
        name, typ, [a.strip() for a in aliases.split(",") if a.strip()], besitzer or None, ble_tag or None, fp
    )
    return {"id": oid, "name": name, "bilder": len(quellen), "davon_erkannt": gefunden, "fingerabdruck": fp is not None}
