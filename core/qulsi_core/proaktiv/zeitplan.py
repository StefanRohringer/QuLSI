"""Zeitpläne für Hintergrund-Agents, lesbar geschrieben statt Cron.

    "alle 15 min"   "alle 2 std"   "stündlich"
    "täglich 07:00"  "werktags 07:30"  "wochenende 09:00"  "mo,mi,fr 18:00"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

TAGE = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
GRUPPEN = {"täglich": set(range(7)), "taeglich": set(range(7)), "werktags": set(range(5)),
           "wochenende": {5, 6}}
EINHEITEN = {"s": 1, "sek": 1, "min": 60, "m": 60, "std": 3600, "h": 3600, "stunde": 3600, "stunden": 3600}

INTERVALL_RE = re.compile(r"^alle\s+(\d+)\s*([a-z]+)$")
UHRZEIT_RE = re.compile(r"^(?P<tage>[a-zäö,\s]+?)\s+(?P<h>\d{1,2}):(?P<m>\d{2})$")


@dataclass
class Zeitplan:
    text: str
    sekunden: int = 0  # > 0: Intervall
    uhrzeit: time | None = None
    tage: set[int] = field(default_factory=set)

    @classmethod
    def lesen(cls, text: str) -> "Zeitplan":
        t = re.sub(r"\s+", " ", text.lower().strip())
        if t in ("stündlich", "stuendlich"):
            return cls(text, sekunden=3600)
        m = INTERVALL_RE.match(t)
        if m:
            einheit = EINHEITEN.get(m[2])
            if einheit is None or int(m[1]) <= 0:
                raise ValueError(f"Zeitplan '{text}': Einheit min, std oder s, Zahl größer 0")
            return cls(text, sekunden=int(m[1]) * einheit)
        m = UHRZEIT_RE.match(t)
        if m:
            tage: set[int] = set()
            for teil in re.split(r"[,\s]+", m["tage"].strip()):
                if teil in GRUPPEN:
                    tage |= GRUPPEN[teil]
                elif teil[:2] in TAGE:
                    tage.add(TAGE[teil[:2]])
                else:
                    raise ValueError(f"Zeitplan '{text}': unbekannter Tag '{teil}'")
            return cls(text, uhrzeit=time(int(m["h"]), int(m["m"])), tage=tage)
        raise ValueError(f"Zeitplan '{text}' nicht verstanden (z. B. 'alle 15 min' oder 'täglich 07:00')")

    def naechster(self, nach: datetime) -> datetime:
        """Nächster Lauf nach dem Zeitpunkt `nach` (zeitzonenbewusst)."""
        if self.sekunden:
            return nach + timedelta(seconds=self.sekunden)
        assert self.uhrzeit is not None
        tag = nach.date()
        for _ in range(8):
            kandidat = datetime.combine(tag, self.uhrzeit, tzinfo=nach.tzinfo)
            if kandidat > nach and kandidat.weekday() in self.tage:
                return kandidat
            tag += timedelta(days=1)
        raise ValueError(f"Zeitplan '{self.text}' hat keinen Tag")


@dataclass
class Ruhezeit:
    """Nachts keine Hinweise aufs Handy und keine Agents, die das nicht ausdrücklich dürfen."""

    von: time | None = None
    bis: time | None = None

    @classmethod
    def lesen(cls, text: str | None) -> "Ruhezeit":
        if not text:
            return cls()
        m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", text.strip())
        if not m:
            raise ValueError(f"Ruhezeit '{text}' bitte als 22:00-07:00")
        return cls(time(int(m[1]), int(m[2])), time(int(m[3]), int(m[4])))

    def aktiv(self, jetzt: datetime) -> bool:
        if self.von is None or self.bis is None:
            return False
        t = jetzt.time()
        if self.von <= self.bis:
            return self.von <= t < self.bis
        return t >= self.von or t < self.bis
