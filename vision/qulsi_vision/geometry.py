"""Reine Rechenfunktionen: Zonen, Nachbarschaft, Ähnlichkeit. Ohne Abhängigkeit von Modellen."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Detection:
    label: str
    score: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 normiert 0..1

    @property
    def fusspunkt(self) -> tuple[float, float]:
        """Unterkante Mitte: dort liegt der Gegenstand auf, das trifft die Zone besser als die Bildmitte."""
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2, y2)

    @property
    def mitte(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2, (y1 + y2) / 2)


def punkt_in_polygon(p: tuple[float, float], poly: list[list[float]]) -> bool:
    """Ray-Casting. Punkte genau auf der Kante zählen als innen."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if min(y1, y2) <= y <= max(y1, y2) and min(x1, x2) <= x <= max(x1, x2):
            # auf der Kante?
            if abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) < 1e-9:
                return True
        if (y1 > y) != (y2 > y):
            xs = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xs:
                inside = not inside
    return inside


def zone_finden(det: Detection, zonen: list[dict]) -> dict | None:
    """Erste Zone, die den Fußpunkt enthält, sonst die mit der Bildmitte."""
    for punkt in (det.fusspunkt, det.mitte):
        for z in zonen:
            if punkt_in_polygon(punkt, z["polygon"]):
                return z
    return None


def nachbarn(det: Detection, alle: list[Detection], abstand: float = 0.15) -> list[str]:
    cx, cy = det.mitte
    out = []
    for d in alle:
        if d is det or d.label == det.label:
            continue
        dx, dy = d.mitte
        if ((cx - dx) ** 2 + (cy - dy) ** 2) ** 0.5 <= abstand:
            out.append(d.label)
    return sorted(set(out))


def normieren(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def bester_treffer(vektor: np.ndarray, kandidaten: list[tuple[int, np.ndarray]], schwelle: float) -> tuple[int | None, float]:
    """Vergleicht einen Bildvektor mit angelernten Fingerabdrücken (Cosinus)."""
    best_id, best = None, -1.0
    v = normieren(vektor)
    for oid, fp in kandidaten:
        sim = float(np.dot(v, normieren(fp)))
        if sim > best:
            best_id, best = oid, sim
    if best_id is None or best < schwelle:
        return None, max(best, 0.0)
    return best_id, best


def ausschnitt(bild: np.ndarray, box: tuple[float, float, float, float], rand: float = 0.15) -> np.ndarray:
    """Bildausschnitt um die Box mit etwas Rand, damit das Bildmodell den Kontext sieht."""
    h, w = bild.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    x1, x2 = max(0.0, x1 - rand * bw), min(1.0, x2 + rand * bw)
    y1, y2 = max(0.0, y1 - rand * bh), min(1.0, y2 + rand * bh)
    return bild[int(y1 * h) : max(int(y2 * h), int(y1 * h) + 1), int(x1 * w) : max(int(x2 * w), int(x1 * w) + 1)]


def bewegung(vorher: np.ndarray | None, jetzt: np.ndarray, schwelle: float) -> bool:
    """Einfacher Bewegungsfilter auf verkleinerten Graubildern."""
    if vorher is None or vorher.shape != jetzt.shape:
        return True
    diff = np.abs(vorher.astype(np.int16) - jetzt.astype(np.int16))
    return float((diff > 25).mean()) >= schwelle
