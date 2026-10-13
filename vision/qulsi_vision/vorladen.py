"""Lädt die Vision-Modelle einmalig nach /models, damit Vision danach ohne Internet läuft.

    docker compose --profile setup run --rm vision-modelle
"""

from __future__ import annotations

import logging
import os

import numpy as np

from .main import lade_config
from .models import lade_detector, lade_embedder

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    bild = np.zeros((64, 64, 3), dtype=np.uint8)
    cfg = lade_config(os.environ.get("CAMERAS_CONFIG", "/config/cameras.yaml"))
    detector = lade_detector(list(cfg["vokabular"]))  # YOLO-World holt dabei auch den CLIP-Textencoder
    detector.erkennen(bild, "vorladen", 0.5)
    lade_embedder().vektor(bild)
    print("Vision-Modelle liegen in /models. Vision läuft jetzt ohne Internet.")
