"""Natürliche Suche: Zeit, Person und Ort aus einer deutschen Frage lesen.

"Was war letzte Woche mit Max in München?" wird zu
Zeitraum(letzte Woche), Personen [Max], Orte [München], Rest "was war".
Alles ohne Sprachmodell, damit es schnell, offline und testbar ist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

MONATE = {
    "januar": 1, "jänner": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mär": 3, "april": 4, "apr": 4,
    "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
EINDEUTIGE_MONATE = {"januar", "februar", "märz", "maerz", "juni", "juli", "september", "oktober", "november", "dezember"}
WOCHENTAGE = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonnabend": 5, "sonntag": 6}
WOCHENTAG_NAMEN = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
ZAHLEN = {"ein": 1, "eine": 1, "einem": 1, "einer": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
          "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12}
EINHEITEN = {"tag": 1, "tage": 1, "tagen": 1, "woche": 7, "wochen": 7, "monat": 30, "monate": 30, "monaten": 30,
             "jahr": 365, "jahre": 365, "jahren": 365}
FEIERTAGE = {"weihnachten": ((12, 24), (12, 26)), "heiligabend": ((12, 24), (12, 24)), "silvester": ((12, 31), (12, 31)),
             "neujahr": ((1, 1), (1, 1))}
STOPPWOERTER = set("""
was wo wann wer wie welche welcher welches war waren ist sind hat hatte hatten habe haben ich mir mich mein meine meinen
meinem du dir der die das den dem des ein eine einen einem einer und oder mit bei in im am an auf zu zum zur von vom für
über nach seit vor letzte letzten letzter letztes vergangene vergangenen diese diesen dieser dieses es gab gemacht passiert
alles etwas zeig zeige finde such suche gibt mal noch schon so da dort hier wir uns uns ihr sie er
""".split())

_ZAHL = r"(\d+|" + "|".join(ZAHLEN) + r")"
_EINHEIT = "(" + "|".join(sorted(EINHEITEN, key=len, reverse=True)) + ")"
_MONAT = "(" + "|".join(sorted(MONATE, key=len, reverse=True)) + r")\.?"
_TAG = "(" + "|".join(WOCHENTAGE) + ")"


@dataclass
class Zeitraum:
    von: datetime
    bis: datetime  # exklusiv
    text: str

    def enthaelt(self, ts: datetime) -> bool:
        return self.von <= ts < self.bis


@dataclass
class Frage:
    zeitraum: Zeitraum | None = None
    personen: list[str] = field(default_factory=list)
    orte: list[str] = field(default_factory=list)
    projekte: list[str] = field(default_factory=list)
    rest: str = ""

    @property
    def stichworte(self) -> list[str]:
        return [w for w in re.findall(r"[\wäöüß-]+", self.rest.lower()) if w not in STOPPWOERTER and len(w) > 1]


def _zahl(wort: str) -> int:
    return int(wort) if wort.isdigit() else ZAHLEN[wort]


def _tage(von: date, bis: date, tz: tzinfo, text: str) -> Zeitraum:
    """Zeitraum über ganze Tage, bis inklusive."""
    return Zeitraum(datetime.combine(von, datetime.min.time(), tz),
                    datetime.combine(bis + timedelta(days=1), datetime.min.time(), tz), text)


def _monat(jahr: int, monat: int, tz: tzinfo, text: str) -> Zeitraum:
    erster = date(jahr, monat, 1)
    naechster = date(jahr + (monat == 12), monat % 12 + 1, 1)
    return _tage(erster, naechster - timedelta(days=1), tz, text)


def zeitraum_lesen(frage: str, jetzt: datetime) -> tuple[Zeitraum | None, str]:
    """Findet die erste Zeitangabe in der Frage. Gibt (Zeitraum, Frage ohne Zeitangabe) zurück."""
    tz = jetzt.tzinfo
    heute = jetzt.date()
    q = frage.lower()

    def treffer(muster: str):
        return re.search(r"(?<![\wäöüß])" + muster + r"(?![\wäöüß])", q)

    def fertig(m: re.Match, z: Zeitraum) -> tuple[Zeitraum, str]:
        rest = (frage[: m.start()] + " " + frage[m.end():]).strip()
        return z, re.sub(r"\s+", " ", rest)

    # 2025-10-03, 3.10.2025, 3.10., 3. Oktober (2025)
    if m := treffer(r"(?:am\s+)?(\d{4})-(\d{1,2})-(\d{1,2})"):
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return fertig(m, _tage(d, d, tz, d.strftime("%d.%m.%Y")))
    if m := treffer(r"(?:am\s+)?(\d{1,2})\.(\d{1,2})\.(\d{2,4})?"):
        jahr = int(m[3]) + (2000 if m[3] and len(m[3]) == 2 else 0) if m[3] else heute.year
        try:
            d = date(jahr, int(m[2]), int(m[1]))
        except ValueError:
            d = None
        if d:
            if not m[3] and d > heute:
                d = d.replace(year=d.year - 1)
            return fertig(m, _tage(d, d, tz, d.strftime("%d.%m.%Y")))
    if m := treffer(r"(?:am\s+)?(\d{1,2})\.\s*" + _MONAT + r"(?:\s+(\d{4}))?"):
        jahr = int(m[3]) if m[3] else heute.year
        try:
            d = date(jahr, MONATE[m[2]], int(m[1]))
            if not m[3] and d > heute:
                d = d.replace(year=d.year - 1)
            return fertig(m, _tage(d, d, tz, d.strftime("%d.%m.%Y")))
        except ValueError:
            pass

    # relative Tage
    for wort, delta in (("vorgestern", -2), ("gestern", -1), ("heute", 0), ("übermorgen", 2), ("morgen", 1)):
        if m := treffer(wort):
            d = heute + timedelta(days=delta)
            return fertig(m, _tage(d, d, tz, wort))

    # vor 3 Tagen, vor zwei Wochen
    if m := treffer(r"vor\s+" + _ZAHL + r"\s+" + _EINHEIT):
        n, e = _zahl(m[1]), EINHEITEN[m[2]]
        mitte = heute - timedelta(days=n * e)
        breite = 0 if e == 1 else (3 if e == 7 else (15 if e == 30 else 182))
        return fertig(m, _tage(mitte - timedelta(days=breite), mitte + timedelta(days=breite), tz, m[0]))
    # in den letzten 7 Tagen, die letzten zwei Wochen
    if m := treffer(r"(?:in\s+den\s+|die\s+|der\s+)?(?:letzten|vergangenen)\s+" + _ZAHL + r"\s+" + _EINHEIT):
        n, e = _zahl(m[1]), EINHEITEN[m[2]]
        return fertig(m, _tage(heute - timedelta(days=n * e - 1), heute, tz, m[0]))

    # Woche, Wochenende, Monat, Jahr
    montag = heute - timedelta(days=heute.weekday())
    if m := treffer(r"(?:am\s+)?(?:letzten|letztes|vergangenen|vergangenes)\s+wochenende"):
        sa = montag - timedelta(days=2)
        return fertig(m, _tage(sa, sa + timedelta(days=1), tz, "letztes Wochenende"))
    if m := treffer(r"(?:am\s+)?(?:diesem|dieses|am)\s+wochenende"):
        sa = montag + timedelta(days=5)
        return fertig(m, _tage(sa, sa + timedelta(days=1), tz, "dieses Wochenende"))
    if m := treffer(r"(?:in\s+der\s+)?(?:letzte|letzten|vergangene|vergangenen|vorige|vorigen)\s+woche"):
        return fertig(m, _tage(montag - timedelta(days=7), montag - timedelta(days=1), tz, "letzte Woche"))
    if m := treffer(r"(?:in\s+)?(?:diese|dieser|diesen)\s+woche"):
        return fertig(m, _tage(montag, montag + timedelta(days=6), tz, "diese Woche"))
    if m := treffer(r"(?:im\s+)?(?:letzten|vergangenen|vorigen)\s+monat"):
        j, mo = (heute.year - 1, 12) if heute.month == 1 else (heute.year, heute.month - 1)
        return fertig(m, _monat(j, mo, tz, "letzten Monat"))
    if m := treffer(r"(?:in\s+)?(?:diesem|diesen)\s+monat"):
        return fertig(m, _monat(heute.year, heute.month, tz, "diesen Monat"))
    if m := treffer(r"(?:im\s+)?(?:letzten|vergangenen|vorigen)\s+jahr|letztes\s+jahr"):
        j = heute.year - 1
        return fertig(m, _tage(date(j, 1, 1), date(j, 12, 31), tz, str(j)))
    if m := treffer(r"(?:in\s+)?(?:diesem|dieses)\s+jahr"):
        return fertig(m, _tage(date(heute.year, 1, 1), date(heute.year, 12, 31), tz, str(heute.year)))

    # an Weihnachten (2024)
    if m := treffer(r"(?:an\s+|zu\s+)?(" + "|".join(FEIERTAGE) + r")(?:\s+(\d{4}))?"):
        (m1, t1), (m2, t2) = FEIERTAGE[m[1]]
        j = int(m[2]) if m[2] else heute.year
        if not m[2] and date(j, m1, t1) > heute:
            j -= 1
        return fertig(m, _tage(date(j, m1, t1), date(j, m2, t2), tz, f"{m[1].capitalize()} {j}"))

    # im März (2025)
    for m in re.finditer(r"(?<![\wäöüß])(?:(im|in|seit)\s+)?" + _MONAT + r"(?:\s+(\d{4}))?(?![\wäöüß])", q):
        # Abkürzungen, "Mai" und "August" nur mit "im" oder Jahreszahl ("Jan" ist meist ein Name)
        if m[1] or m[3] or m[2] in EINDEUTIGE_MONATE:
            mo = MONATE[m[2]]
            j = int(m[3]) if m[3] else (heute.year if mo <= heute.month else heute.year - 1)
            z = _monat(j, mo, tz, f"{m[2].capitalize()} {j}")
            if m[1] == "seit":
                z = Zeitraum(z.von, datetime.combine(heute + timedelta(days=1), datetime.min.time(), tz), f"seit {z.text}")
            return fertig(m, z)

    # (letzten) Montag
    if m := treffer(r"(?:am\s+)?(?:letzten\s+|vergangenen\s+)?" + _TAG):
        wd = WOCHENTAGE[m[1]]
        zurueck = (heute.weekday() - wd) % 7 or (7 if "letzt" in m[0] or "vergangen" in m[0] else 0)
        d = heute - timedelta(days=zurueck)
        return fertig(m, _tage(d, d, tz, f"{WOCHENTAG_NAMEN[wd]}, {d.strftime('%d.%m.')}"))

    # 2024
    if m := treffer(r"(?:im\s+jahr\s+|in\s+)?((?:19|20)\d{2})"):
        j = int(m[1])
        return fertig(m, _tage(date(j, 1, 1), date(j, 12, 31), tz, str(j)))
    return None, frage.strip()


def namen_finden(text: str, namen: dict[str, str]) -> tuple[list[str], str]:
    """Findet bekannte Namen (Alias -> Hauptname) im Text, längste zuerst. Gibt (Hauptnamen, Text ohne Namen)."""
    gefunden: list[str] = []
    for alias in sorted(namen, key=len, reverse=True):
        if len(alias) < 2:
            continue
        muster = r"(?<![\wäöüß])" + re.escape(alias) + r"(?:s|'s|')?(?![\wäöüß])"
        if re.search(muster, text, flags=re.IGNORECASE):
            if namen[alias] not in gefunden:
                gefunden.append(namen[alias])
            text = re.sub(muster, " ", text, flags=re.IGNORECASE)
    return gefunden, re.sub(r"\s+", " ", text).strip()


def frage_lesen(frage: str, jetzt: datetime, personen: dict[str, str], orte: dict[str, str],
                projekte: dict[str, str] | None = None) -> Frage:
    zr, rest = zeitraum_lesen(frage, jetzt)
    p, rest = namen_finden(rest, personen)
    o, rest = namen_finden(rest, orte)
    pr, rest = namen_finden(rest, projekte or {})
    return Frage(zr, p, o, pr, rest)


def ort_normalisieren(ort: str) -> str:
    """ "Café Luitpold, Brienner Str. 11, 80333 München" -> "Café Luitpold". Links aus Online-Terminen fallen weg."""
    ort = ort.strip()
    if not ort or re.match(r"^(https?://|www\.)", ort, re.IGNORECASE) or "zoom.us" in ort or "teams.microsoft" in ort:
        return ""
    return ort.split(",")[0].strip()[:80]
