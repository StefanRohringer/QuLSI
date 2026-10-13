"""Model Router: wählt pro Anfrage das passende Modell (oder gar keins).

    "Wie spät ist es?"          -> system   (QuLSI antwortet selbst)
    "Schreib Max eine Mail"     -> klein    (kleines Modell + Werkzeuge, bei Fehler Standard)
    "Was habe ich heute vor?"   -> standard (Bonsai ohne Denkmodus)
    "Analysiere das Angebot"    -> gross    (Bonsai mit Denkmodus, nie per Stimme)
    Nachricht mit Bild          -> vision
"""

from __future__ import annotations

import os

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]



BILDER_FEHLEN = ("Bilder kann ich gerade nicht ansehen: Dem Sprachmodell fehlt der Bildteil (mmproj). "
                 "Kamerabilder und Gegenstände erkennt QuLSI trotzdem über die Bilderkennung.")


def bilder_verfuegbar() -> bool:
    """Bonsai mit Bildteil gestartet? QULSI_BILDER setzen run.sh (RunPod) bzw. docker-compose.yml (BONSAI_MMPROJ)."""
    return os.environ.get("QULSI_BILDER", "an").strip().lower() not in ("", "aus", "nein", "false", "0")

@dataclass
class Route:
    art: str  # system | klein | standard | gross | vision
    modell: str | None = None
    antwort: str | None = None  # nur bei system
    werkzeuge: bool = True
    grund: str = ""
    ersatz: str | None = None  # Modell, wenn das gewählte scheitert


@dataclass
class Router:
    modelle: dict[str, str | None] = field(default_factory=lambda: {
        "klein": "qulsi-klein", "standard": "bonsai-fast", "gross": "bonsai-think", "vision": "bonsai-vision"})
    system: dict[str, list[str]] = field(default_factory=dict)
    gross_stichworte: list[str] = field(default_factory=list)
    gross_ab: int = 600
    klein_stichworte: list[str] = field(default_factory=list)
    klein_bis: int = 120
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Berlin"))

    @classmethod
    def load(cls, path: str | Path, tz: str = "Europe/Berlin", overrides: dict[str, str | None] | None = None) -> "Router":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        r = cls(tz=ZoneInfo(tz))
        r.modelle.update(raw.get("modelle") or {})
        for k, v in (overrides or {}).items():
            if v:  # "aus" schaltet ein Modell ab, leer lässt router.yaml gelten
                r.modelle[k] = None if v.lower() in ("aus", "off", "none") else v
        r.system = raw.get("system") or {}
        g, k = raw.get("gross") or {}, raw.get("klein") or {}
        r.gross_stichworte, r.gross_ab = g.get("stichworte", []), int(g.get("ab_zeichen", 600))
        r.klein_stichworte, r.klein_bis = k.get("stichworte", []), int(k.get("bis_zeichen", 120))
        return r

    def waehlen(self, text: str, kanal: str = "chat", hat_bild: bool = False, erzwingen: str | None = None) -> Route:
        t = _norm(text)
        standard = self.modelle.get("standard")

        if erzwingen in self.modelle and self.modelle[erzwingen]:
            return Route(erzwingen, self.modelle[erzwingen], grund="vom Aufrufer gewählt")

        if hat_bild and self.modelle.get("vision") and bilder_verfuegbar():
            return Route("vision", self.modelle["vision"], werkzeuge=False, grund="Bild in der Nachricht")
        if hat_bild:
            return Route("system", antwort=BILDER_FEHLEN, grund="Bild, aber kein Bildmodell")

        antwort = self._system(t)
        if antwort:
            return Route("system", antwort=antwort, grund="ohne Modell beantwortet")

        if len(t) >= self.gross_ab or _enthaelt(t, self.gross_stichworte):
            if kanal != "stimme" and self.modelle.get("gross"):
                return Route("gross", self.modelle["gross"], grund="Analyse oder Planung")
            return Route("standard", standard, grund="Analyse per Stimme, ohne Denkmodus")

        if self.modelle.get("klein") and len(t) <= self.klein_bis and _enthaelt(t, self.klein_stichworte):
            return Route("klein", self.modelle["klein"], grund="kurzer Befehl", ersatz=standard)

        return Route("standard", standard, grund="Standard")

    def _system(self, t: str) -> str | None:
        # nur kurze, eindeutige Fragen; "wie spät ist es in Tokio" geht ans Modell
        if len(t) > 40 or " in " in t:
            return None
        jetzt = datetime.now(self.tz)
        if _enthaelt(t, self.system.get("uhrzeit", [])):
            return f"Es ist {jetzt:%H:%M} Uhr."
        if _enthaelt(t, self.system.get("datum", [])):
            return f"Heute ist {WOCHENTAGE[jetzt.weekday()]}, der {jetzt.day}. {MONATE[jetzt.month - 1]} {jetzt.year}."
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _enthaelt(t: str, worte: list[str]) -> bool:
    return any(w.lower() in t for w in worte)
