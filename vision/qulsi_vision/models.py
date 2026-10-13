"""Erkennungs- und Bildvektor-Modelle. Schwere Bibliotheken werden erst beim Laden importiert.

DETECTOR:  owlv2 (Standard, Apache-2.0) | yolo-world (schneller, ultralytics ist AGPL) | statisch (Tests)
EMBEDDER:  siglip (Standard)     | histogramm (leicht, ohne GPU, für Tests)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import cv2
import numpy as np

from .geometry import Detection, normieren

log = logging.getLogger(__name__)
DEVICE = os.environ.get("DEVICE", "cuda")


class YoloWorldDetector:
    def __init__(self, vokabular: list[str]):
        from ultralytics import YOLOWorld

        self.model = YOLOWorld(os.environ.get("YOLO_WORLD_MODEL", "yolov8s-worldv2.pt"))
        self.model.set_classes(vokabular)
        self.namen = vokabular

    def erkennen(self, bild: np.ndarray, kamera: str, min_score: float) -> list[Detection]:
        res = self.model.predict(bild, conf=min_score, verbose=False, device=DEVICE if DEVICE != "cpu" else "cpu")[0]
        out = []
        for box, cls, conf in zip(res.boxes.xyxyn.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
            out.append(Detection(self.namen[int(cls)], float(conf), tuple(box)))
        return out


class Owlv2Detector:
    def __init__(self, vokabular: list[str]):
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor

        name = os.environ.get("OWLV2_MODEL", "google/owlv2-base-patch16-ensemble")
        self.torch = torch
        self.proc = Owlv2Processor.from_pretrained(name)
        self.model = Owlv2ForObjectDetection.from_pretrained(name).to(DEVICE).eval()
        self.namen = vokabular

    def erkennen(self, bild: np.ndarray, kamera: str, min_score: float) -> list[Detection]:
        rgb = cv2.cvtColor(bild, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        inputs = self.proc(text=[[f"a photo of {v}" for v in self.namen]], images=rgb, return_tensors="pt").to(DEVICE)
        with self.torch.no_grad():
            outputs = self.model(**inputs)
        # OWLv2 füllt das Bild auf ein Quadrat auf, daher die längere Seite als Zielgröße.
        s = max(h, w)
        res = self.proc.post_process_object_detection(
            outputs, threshold=min_score, target_sizes=self.torch.tensor([[s, s]], device=DEVICE)
        )[0]
        out = []
        for box, score, lab in zip(res["boxes"].tolist(), res["scores"].tolist(), res["labels"].tolist()):
            x1, y1, x2, y2 = box
            out.append(Detection(self.namen[lab], score, (max(0, x1 / w), max(0, y1 / h), min(1, x2 / w), min(1, y2 / h))))
        return out


class StatischerDetector:
    """Liest feste Erkennungen aus einer JSON-Datei: {"kamera": [{"label","score","box"}]}. Nur für Tests."""

    def __init__(self, vokabular: list[str]):
        self.pfad = Path(os.environ.get("DETECTOR_STATIC_FILE", "/config/static_detections.json"))

    def erkennen(self, bild: np.ndarray, kamera: str, min_score: float) -> list[Detection]:
        daten = json.loads(self.pfad.read_text()) if self.pfad.exists() else {}
        return [Detection(d["label"], d["score"], tuple(d["box"])) for d in daten.get(kamera, []) if d["score"] >= min_score]


class SiglipEmbedder:
    dim = 768

    def __init__(self):
        import torch
        from transformers import AutoModel, AutoProcessor

        name = os.environ.get("SIGLIP_MODEL", "google/siglip-base-patch16-224")
        self.torch = torch
        self.proc = AutoProcessor.from_pretrained(name)
        self.model = AutoModel.from_pretrained(name).to(DEVICE).eval()

    def vektor(self, bild: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(bild, cv2.COLOR_BGR2RGB)
        inputs = self.proc(images=rgb, return_tensors="pt").to(DEVICE)
        with self.torch.no_grad():
            feats = self.model.get_image_features(**inputs)
        if not isinstance(feats, self.torch.Tensor):  # neuere transformers liefern ein Objekt
            feats = feats.pooler_output
        return normieren(feats[0].float().cpu().numpy())


class HistogrammEmbedder:
    """768-dimensionaler Farb- und Formvektor ohne neuronales Netz.

    Erkennt denselben Gegenstand unter gleichen Lichtverhältnissen ordentlich wieder,
    ist aber deutlich schwächer als SigLIP. Für Tests und schwache Hardware.
    """

    dim = 768

    def vektor(self, bild: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(bild, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256]).flatten()  # 512
        grau = cv2.resize(cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY), (16, 16)).flatten().astype(np.float32)  # 256
        grau = (grau - grau.mean()) / (grau.std() + 1e-6)
        return normieren(np.concatenate([normieren(hist), normieren(grau)]).astype(np.float32))


DETECTORS = {"yolo-world": YoloWorldDetector, "owlv2": Owlv2Detector, "statisch": StatischerDetector}
EMBEDDERS = {"siglip": SiglipEmbedder, "histogramm": HistogrammEmbedder}


def lade_detector(vokabular: list[str]):
    name = os.environ.get("DETECTOR", "owlv2")
    log.info("Lade Detektor %s auf %s", name, DEVICE)
    return DETECTORS[name](vokabular)


def lade_embedder():
    name = os.environ.get("EMBEDDER", "siglip")
    log.info("Lade Bildvektor-Modell %s", name)
    return EMBEDDERS[name]()
