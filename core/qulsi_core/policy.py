"""Policy Engine: entscheidet über jeden Werkzeugaufruf. Das Sprachmodell kann sie nicht umgehen.

Stufen (wie in den Connector-Manifesten):
  low     frei, wird protokolliert
  medium  frei bei direkter Anfrage; ist das Gespräch belastet, gilt policy.yaml > belastet.medium
  high    nie direkt: Freigabe in der Handy-App (ntfy), nie per Stimme oder Chat

Hintergrund-Agents (kanal "automatik") handeln ohne direkte Anfrage. Für sie ist medium
nur frei, wenn der Agent die Fähigkeit in config/agenten.yaml unter "selbststaendig"
hat; sonst gilt policy.yaml > automatik.medium (Standard: Freigabe auf dem Handy).
  deny    Werkzeug wird dem Modell gar nicht angeboten

Rate-Limits (policy.yaml > limits): höchstens N Aufrufe je Werkzeug und Stunde, je Stufe oder
je Werkzeug. Jeder Aufruf und jede Ablehnung landet im Protokoll (audit_log).

Woher die Stufe kommt, in dieser Reihenfolge:
  1. config/policy.yaml > stufen (dein Override, z. B. home.set_state: high)
  2. Manifest des Connectors bzw. Stufe des Kern-Werkzeugs
  3. unbekannt -> high
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml


class Stufe(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DENY = "deny"


class Entscheid(str, Enum):
    ERLAUBT = "erlaubt"
    BESTAETIGUNG = "bestaetigung"
    ABGELEHNT = "abgelehnt"


@dataclass
class Kontext:
    """Was die Policy Engine über die laufende Anfrage weiß."""

    kanal: str = "chat"  # chat | stimme | app | automatik | system
    belastet: bool = False  # fremder Text (Mail, Web, Dateien) ist im Gespräch
    belastet_durch: list[str] = field(default_factory=list)
    selbststaendig: set[str] = field(default_factory=set)  # nur kanal automatik: medium ohne Freigabe
    nutzer_texte: list[str] = field(default_factory=list)  # die letzten Nachrichten des Nutzers (für eigene_angabe)


@dataclass
class Policy:
    overrides: dict[str, Stufe] = field(default_factory=dict)
    belastet_medium: Entscheid = Entscheid.BESTAETIGUNG
    belastet_high: Entscheid = Entscheid.ABGELEHNT
    gueltig_sekunden: int = 300
    automatik_medium: Entscheid = Entscheid.BESTAETIGUNG
    limits_stufe: dict[Stufe, int] = field(default_factory=dict)
    limits_werkzeug: dict[str, int] = field(default_factory=dict)
    connector_modi: dict[str, str] = field(default_factory=dict)  # an | nur_lesen | aus
    # von Hub und Kern gesetzt
    stufen: dict[str, Stufe] = field(default_factory=dict)
    belastend: set[str] = field(default_factory=set)
    _aufrufe: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        b = raw.get("belastet") or {}
        limits = dict(raw.get("limits") or {})
        stunde = limits.pop("stunde", None) or {}
        return cls(
            overrides={k: Stufe(v) for k, v in (raw.get("stufen") or {}).items()},
            belastet_medium=_entscheid(b.get("medium", "bestaetigung")),
            belastet_high=_entscheid(b.get("high", "verboten")),
            gueltig_sekunden=int((raw.get("bestaetigung") or {}).get("gueltig_sekunden", 300)),
            automatik_medium=_entscheid((raw.get("automatik") or {}).get("medium", "bestaetigung")),
            belastend=set(raw.get("belastend") or []),
            limits_stufe={Stufe(k): int(v) for k, v in stunde.items()},
            limits_werkzeug={k: int(v) for k, v in limits.items()},
            connector_modi={k: str(v) for k, v in (raw.get("connectoren") or {}).items()},
        )

    def registrieren(self, faehigkeit: str, stufe: Stufe, belastend: bool = False) -> None:
        self.stufen[faehigkeit] = stufe
        if belastend:
            self.belastend.add(faehigkeit)

    def stufe(self, faehigkeit: str) -> Stufe:
        return self.overrides.get(faehigkeit) or self.stufen.get(faehigkeit) or Stufe.HIGH

    def sichtbar(self, faehigkeit: str) -> bool:
        return self.stufe(faehigkeit) is not Stufe.DENY

    def entscheide(self, faehigkeit: str, ctx: Kontext, arguments: dict | None = None) -> tuple[Stufe, Entscheid, str]:
        stufe = self.stufe(faehigkeit)
        if stufe is Stufe.DENY:
            return stufe, Entscheid.ABGELEHNT, "Dieses Werkzeug ist gesperrt."
        if stufe is Stufe.LOW:
            return stufe, Entscheid.ERLAUBT, "Lesezugriff."
        if stufe is Stufe.MEDIUM:
            if ctx.belastet:
                if faehigkeit in EIGENE_ANGABEN and eigene_angabe(arguments or {}, ctx.nutzer_texte, EIGENE_ANGABEN[faehigkeit]):
                    return stufe, Entscheid.ERLAUBT, "Der Nutzer hat das selbst so geschrieben."
                return stufe, self.belastet_medium, _belastet_text(ctx)
            if ctx.kanal == "automatik" and faehigkeit not in ctx.selbststaendig:
                return stufe, self.automatik_medium, "Hintergrund-Agent ohne direkte Anfrage, Freigabe nötig."
            return stufe, Entscheid.ERLAUBT, "Direkte Anfrage."
        if ctx.belastet and self.belastet_high is Entscheid.ABGELEHNT:
            return stufe, Entscheid.ABGELEHNT, _belastet_text(ctx)
        return stufe, Entscheid.BESTAETIGUNG, "Kritische Aktion, Freigabe in der Handy-App nötig."

    def zaehlen(self, faehigkeit: str, jetzt: float | None = None) -> str | None:
        """Zählt einen Aufruf. Gibt einen Grund zurück, wenn das Stundenlimit erreicht ist."""
        grenze = self.limits_werkzeug.get(faehigkeit) or self.limits_stufe.get(self.stufe(faehigkeit))
        if not grenze:
            return None
        jetzt = time.monotonic() if jetzt is None else jetzt
        q = self._aufrufe[faehigkeit]
        while q and q[0] <= jetzt - 3600:
            q.popleft()
        if len(q) >= grenze:
            return f"Rate-Limit: {faehigkeit} höchstens {grenze}-mal pro Stunde."
        q.append(jetzt)
        return None

    def belastet_nach(self, faehigkeit: str) -> bool:
        return faehigkeit in self.belastend


def stufe_aus_manifest(faehigkeit: str, permissions: dict) -> Stufe:
    """permissions im Manifest: ganze Fähigkeit ("email.send: high") oder nur das Verb ("send: high").
    false heißt: verboten, das Werkzeug wird nie angeboten (wie deny)."""
    verb = faehigkeit.split(".", 1)[-1]
    for schluessel in (faehigkeit, verb, verb.replace(".", "_")):
        if schluessel in permissions:
            wert = permissions[schluessel]
            return Stufe.DENY if wert is False or str(wert).lower() in ("false", "nein") else Stufe(wert)
    return Stufe.HIGH


def _entscheid(wert: str) -> Entscheid:
    return {"bestaetigung": Entscheid.BESTAETIGUNG, "verboten": Entscheid.ABGELEHNT, "erlaubt": Entscheid.ERLAUBT}[wert]


# Werkzeuge, die bei belastetem Gespräch ohne Freigabe laufen dürfen, wenn der Inhalt erkennbar aus den
# eigenen Nachrichten des Nutzers stammt ("Ich bin 150 Jahre alt" -> memory.remember). Wert: nötiger Anteil
# der Inhaltswörter, die der Nutzer selbst geschrieben hat. Zahlen müssen immer alle vorkommen.
EIGENE_ANGABEN = {"memory.remember": 0.5, "user.name_set": 1.0, "calendar.create": 0.5, "tasks.create": 0.5,
                  "persons.save": 0.5, "persons.rename": 0.5}
# Datum und Uhrzeit rechnet das Modell selbst aus ("heute 9:30" -> 2026-10-08T09:30), die zählen nicht mit
_NICHT_PRUEFEN = {"art", "wann", "start", "ende", "faellig", "tag", "datum", "id"}
_FUELLWOERTER = {
    "der", "die", "das", "dem", "den", "des", "ein", "eine", "einen", "einem", "einer", "und", "oder", "aber", "ist",
    "sind", "war", "hat", "haben", "wird", "nutzer", "nutzerin", "user", "ich", "mich", "mir", "mein", "meine",
    "meinen", "sein", "seine", "seinen", "ihr", "ihre", "ihren", "sich", "heißt", "heisst", "name", "namens",
    "bin", "für", "mit", "von", "zu", "zum", "zur", "auf", "bei", "als", "dass", "gern", "gerne", "laut", "sagt", "gesagt",
}


def _woerter(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if w.isdigit() or (len(w) >= 3 and w not in _FUELLWOERTER)]


def eigene_angabe(arguments: dict, nutzer_texte: list[str], anteil: float) -> bool:
    """Stammt der Inhalt der Argumente aus dem, was der Nutzer selbst geschrieben hat?

    Fremde Inhalte (Webseite, Mail) könnten das Modell sonst dazu bringen, sich etwas Untergeschobenes zu
    merken. Deshalb nur, wenn alle Zahlen und genug Inhaltswörter wörtlich in den Nutzer-Nachrichten stehen.
    """
    werte = " ".join(str(v) for k, v in arguments.items() if isinstance(v, str) and k not in _NICHT_PRUEFEN)
    woerter = _woerter(werte)
    eigene = _woerter(" ".join(nutzer_texte))
    if not woerter or not eigene:
        return False
    zusammen = re.sub(r"\W", "", " ".join(nutzer_texte).lower())  # "Arzt termin" deckt "Arzttermin"
    stamm = {w[:5] for w in eigene if not w.isdigit()}
    zahlen = {w for w in eigene if w.isdigit()}
    if any(w.isdigit() and w not in zahlen for w in woerter):
        return False
    inhalt = [w for w in woerter if not w.isdigit()]
    if not inhalt:
        return True
    return sum(w[:5] in stamm or w in zusammen for w in inhalt) / len(inhalt) >= anteil


def _belastet_text(ctx: Kontext) -> str:
    quellen = ", ".join(sorted(set(ctx.belastet_durch))) or "Mail/Web"
    return f"Im Gespräch stehen Inhalte aus {quellen}; die könnten versteckte Anweisungen enthalten."
