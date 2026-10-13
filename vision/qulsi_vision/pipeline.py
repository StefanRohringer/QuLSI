"""Vision-Pipeline: Bild holen, Bewegung prüfen, erkennen, wiedererkennen, Zone, Fakten speichern.

Läuft dauerhaft ohne Sprachmodell. Gespeichert werden Fakten ("Schlüssel liegt auf der
Kommode"), kein Video. Nur kleine Bildausschnitte kommen für einige Tage in den Object Storage.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

import cv2
import numpy as np

from .geometry import Detection, ausschnitt, bewegung, nachbarn, zone_finden
from .store import Store, objekt_ablegen

log = logging.getLogger(__name__)

CROP_TAGE = int(os.environ.get("CROP_RETENTION_DAYS", "14"))
BILD_ENDUNGEN = (".jpg", ".jpeg", ".png")


class Kamera:
    """RTSP-Stream oder (für Tests) eine Bilddatei."""

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url
        self.cap: cv2.VideoCapture | None = None

    def bild(self) -> np.ndarray | None:
        if self.url.lower().endswith(BILD_ENDUNGEN):
            return cv2.imread(self.url)
        if self.cap is None or not self.cap.isOpened():
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Puffer leeren, damit wir das aktuelle Bild bekommen und nicht ein altes.
        for _ in range(4):
            self.cap.grab()
        ok, frame = self.cap.retrieve()
        if not ok:
            self.schliessen()
            return None
        return frame

    def schliessen(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class Wahrnehmung:
    def __init__(self, cfg: dict, store: Store, detector, embedder):
        self.cfg = cfg
        self.store = store
        self.detector = detector
        self.embedder = embedder
        self.vokabular: dict[str, str] = cfg["vokabular"]
        self.zonen: dict[str, list[dict]] = {}
        self.kameras = {k["name"]: Kamera(k["name"], k["url"]) for k in cfg["kameras"] if k.get("url")}
        self.modell_lock = asyncio.Lock()  # GPU-Modelle nicht parallel aufrufen
        self.letzte_analyse: dict[str, float] = {}

    def schwelle(self, label: str) -> float:
        return float(self.cfg.get("schwellen", {}).get(label, self.cfg.get("schwelle_standard", 0.15)))

    async def start(self) -> None:
        self.zonen = await self.store.zonen_abgleichen(self.cfg["kameras"], self.cfg.get("bluetooth_raeume", {}))

    async def analysieren(self, kamera: str, bild: np.ndarray) -> list[dict]:
        """Erkennt Gegenstände in einem Bild und schreibt Sichtungen. Liefert eine Zusammenfassung."""
        min_score = min([self.schwelle(v) for v in self.vokabular] or [0.1])
        async with self.modell_lock:
            dets: list[Detection] = await asyncio.to_thread(self.detector.erkennen, bild, kamera, min_score)
        dets = [d for d in dets if d.score >= self.schwelle(d.label)]
        self.letzte_analyse[kamera] = time.time()

        ergebnis = []
        for d in dets:
            zone = zone_finden(d, self.zonen.get(kamera, []))
            crop = ausschnitt(bild, d.box)
            if crop.size == 0:
                continue
            async with self.modell_lock:
                fp = await asyncio.to_thread(self.embedder.vektor, crop)
            oid, sim = await self.store.bester_objekt(d.label, fp, float(self.cfg.get("persoenlich_schwelle", 0.82)))
            key = await asyncio.to_thread(self._crop_speichern, crop)
            deutsch = self.vokabular.get(d.label, d.label)
            neben = [self.vokabular.get(n, n) for n in nachbarn(d, dets)]
            name = None
            if oid is not None:
                name = next((n for i, n in await self._objektnamen() if i == oid), deutsch)
            sid = await self.store.sichtung(
                object_id=oid, label=d.label, zone=zone, kamera=kamera, sicherheit=d.score,
                aehnlichkeit=sim, quelle="kamera", box=list(d.box),
                ausschnitt=key, neben=neben, name_fuer_ereignis=name,
            )
            ergebnis.append({
                "sighting_id": sid,
                "gegenstand": name or deutsch,
                "persoenlich": oid is not None,
                "ort": zone["name"] if zone else "außerhalb der Zonen",
                "sicherheit": round(d.score, 2),
                "neben": neben,
            })
        return ergebnis

    async def _objektnamen(self) -> list[tuple[int, str]]:
        async with await self.store.conn() as c:
            rows = await (await c.execute("SELECT id, name FROM objects")).fetchall()
        return [(r["id"], r["name"]) for r in rows]

    @staticmethod
    def _crop_speichern(crop: np.ndarray) -> str:
        ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return objekt_ablegen(jpg.tobytes(), ".jpg")

    async def schnappschuss(self, kamera: str) -> dict:
        """Für das Werkzeug camera.capture: jetzt ein Bild holen und auswerten."""
        if await self.store.privacy():
            return {"fehler": "Privacy-Modus ist an, Kameras werden nicht ausgewertet."}
        k = self.kameras.get(kamera)
        if k is None:
            return {"fehler": f"Unbekannte Kamera {kamera}. Bekannt: {sorted(self.kameras)}"}
        bild = await asyncio.to_thread(k.bild)
        if bild is None:
            return {"fehler": f"Kamera {kamera} liefert gerade kein Bild."}
        return {"kamera": kamera, "zeit": datetime.now().strftime("%H:%M:%S"), "gegenstaende": await self.analysieren(kamera, bild)}

    async def kamera_schleife(self, name: str) -> None:
        """Dauerbetrieb: fps Bilder pro Sekunde ansehen, nur bei Bewegung oder zum Auffrischen erkennen."""
        k = self.kameras[name]
        pause = 1.0 / float(self.cfg.get("fps", 1))
        auffrischen = float(self.cfg.get("auffrischen_sekunden", 600))
        schwelle = float(self.cfg.get("bewegung_schwelle", 0.01))
        vorher: np.ndarray | None = None
        while True:
            try:
                if await self.store.privacy():
                    k.schliessen()
                    vorher = None
                    await asyncio.sleep(5)
                    continue
                bild = await asyncio.to_thread(k.bild)
                if bild is None:
                    log.warning("Kamera %s liefert kein Bild, neuer Versuch in 10 s", name)
                    await asyncio.sleep(10)
                    continue
                klein = cv2.resize(cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY), (160, 90))
                faellig = time.time() - self.letzte_analyse.get(name, 0) > auffrischen
                if bewegung(vorher, klein, schwelle) or faellig:
                    gefunden = await self.analysieren(name, bild)
                    if gefunden:
                        log.info("%s: %s", name, ", ".join(f"{g['gegenstand']}@{g['ort']}" for g in gefunden))
                vorher = klein
            except Exception:  # noqa: BLE001
                log.exception("Fehler in Kamera %s", name)
                await asyncio.sleep(5)
            await asyncio.sleep(pause)

    async def aufraeumen_schleife(self) -> None:
        while True:
            try:
                await self.store.aufraeumen(CROP_TAGE)
            except Exception:  # noqa: BLE001
                log.exception("Aufräumen fehlgeschlagen")
            await asyncio.sleep(3600)
