"""Werkzeuge des Gedächtnisses für den Agenten (laufen im Core, gehen durch die Policy Engine)."""

from __future__ import annotations

from typing import Callable

from ..policy import Stufe
from .gedaechtnis import ARTEN, Gedaechtnis
from .memchip import Memchip
from .organisator import Organisator

S, I, N = {"type": "string"}, {"type": "integer"}, {"type": "number"}
TAG = {"type": "string", "description": "JJJJ-MM-TT"}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def registrieren(reg: Callable, g: Gedaechtnis, organisator: Organisator, memchip: Memchip) -> None:
    reg("memory.find", Stufe.LOW,
        "Findet Erinnerungen nach Person, Zeit und Ort, auch direkt aus der Frage: 'Was war letzte Woche mit Max in München?', "
        "'Fotos vom Gardasee im Juli', 'Mails von Anna seit März'. Durchsucht Termine, Mails, Fotos, Dateien, Notizen und Rückblicke. "
        "Die frage kann einfach die Nutzerfrage sein; person, ort, von, bis schränken zusätzlich ein.",
        _schema({"frage": S, "person": S, "ort": S, "von": TAG, "bis": TAG, "projekt": S,
                 "art": {"type": "string", "enum": ARTEN}, "anzahl": I}), g.finden)
    reg("memory.timeline", Stufe.LOW,
        "Timeline: was an einem Tag oder in einem Zeitraum passiert ist (Ereignisse, Termine, Mails, Fotos, Notizen), "
        "optional nur mit einer Person oder an einem Ort.",
        _schema({"tag": {"type": "string", "description": "JJJJ-MM-TT, Standard heute"}, "von": TAG, "bis": TAG,
                 "person": S, "ort": S}), g.timeline)
    reg("memory.forget", Stufe.HIGH, "Löscht eine Erinnerung endgültig (id aus memory.find oder memory.search).",
        _schema({"id": I}, ["id"]), g.vergessen)
    reg("places.save", Stufe.MEDIUM,
        "Speichert einen Ort, z. B. 'Zuhause' oder 'Büro', optional mit Koordinaten (dann werden Fotos dort automatisch zugeordnet).",
        _schema({"name": S, "lat": N, "lon": N, "art": S, "aliase": {"type": "array", "items": S},
                 "radius_m": I}, ["name"]), g.orte_speichern)
    reg("places.list", Stufe.LOW, "Bekannte Orte und wie viele Erinnerungen es dort gibt.", _schema({}), g.orte_liste)
    reg("memory.organize", Stufe.MEDIUM,
        "Organisiert das Gedächtnis jetzt sofort (sonst automatisch alle paar Minuten): Termine, Mails, Fotos, Dateien, Gewohnheiten.",
        _schema({}), organisator.lauf)
    reg("memchip.status", Stufe.LOW, "Ist ein Memchip (austauschbarer Gedächtnis-Speicher) eingesteckt, welche Sicherungen liegen darauf?",
        _schema({}), memchip.status)
    reg("memchip.export", Stufe.MEDIUM, "Schreibt das ganze Gedächtnis als eine Datei auf den Memchip.",
        _schema({"name": {"type": "string", "description": "optionaler Zusatz im Dateinamen"}}), memchip.exportieren)
    reg("memchip.import", Stufe.HIGH,
        "Lädt ein Gedächtnis vom Memchip. modus zusammenfuehren ergänzt, ersetzen tauscht das Gedächtnis aus (das alte wird vorher gesichert).",
        _schema({"datei": S, "modus": {"type": "string", "enum": ["zusammenfuehren", "ersetzen"]}}, ["datei"]), memchip.importieren)
