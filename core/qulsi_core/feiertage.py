"""Gesetzliche Feiertage in Deutschland, berechnet (ohne Internet, ohne Kalender-Abo).

Bundesland aus QULSI_BUNDESLAND (Kürzel wie BY, NW, BE) oder als Argument. Ohne Bundesland
kommen alle Feiertage mit der Angabe, wo sie gelten. Nicht enthalten sind Feiertage, die nur in
einzelnen Gemeinden gelten (Fronleichnam in Teilen von Sachsen und Thüringen, Mariä Himmelfahrt
in Teilen Bayerns, Augsburger Friedensfest).
"""

from __future__ import annotations

from datetime import date, timedelta

LAENDER = {
    "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg", "HB": "Bremen",
    "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland", "SN": "Sachsen",
    "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "TH": "Thüringen",
}
NAMEN = {v.lower(): k for k, v in LAENDER.items()} | {k.lower(): k for k in LAENDER}
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def ostersonntag(jahr: int) -> date:
    """Gaußsche Osterformel (gregorianisch, Algorithmus nach Meeus/Jones/Butcher)."""
    a, b, c = jahr % 19, jahr // 100, jahr % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    monat = (h + l - 7 * m + 114) // 31
    tag = (h + l - 7 * m + 114) % 31 + 1
    return date(jahr, monat, tag)


def feiertage_jahr(jahr: int) -> list[tuple[date, str, set[str] | None]]:
    """(Datum, Name, Länder oder None = bundesweit)."""
    o = ostersonntag(jahr)
    buss = date(jahr, 11, 22) - timedelta(days=(date(jahr, 11, 22).weekday() - 2) % 7)  # Mittwoch vor dem 23.11.
    liste: list[tuple[date, str, set[str] | None]] = [
        (date(jahr, 1, 1), "Neujahr", None),
        (date(jahr, 1, 6), "Heilige Drei Könige", {"BW", "BY", "ST"}),
        (date(jahr, 3, 8), "Internationaler Frauentag", {"BE"} | ({"MV"} if jahr >= 2023 else set())),
        (o - timedelta(days=2), "Karfreitag", None),
        (o, "Ostersonntag", {"BB"}),
        (o + timedelta(days=1), "Ostermontag", None),
        (date(jahr, 5, 1), "Tag der Arbeit", None),
        (o + timedelta(days=39), "Christi Himmelfahrt", None),
        (o + timedelta(days=49), "Pfingstsonntag", {"BB"}),
        (o + timedelta(days=50), "Pfingstmontag", None),
        (o + timedelta(days=60), "Fronleichnam", {"BW", "BY", "HE", "NW", "RP", "SL"}),
        (date(jahr, 8, 15), "Mariä Himmelfahrt", {"SL"}),
        (date(jahr, 9, 20), "Weltkindertag", {"TH"}),
        (date(jahr, 10, 3), "Tag der Deutschen Einheit", None),
        (date(jahr, 10, 31), "Reformationstag", {"BB", "HB", "HH", "MV", "NI", "SN", "ST", "SH", "TH"}),
        (date(jahr, 11, 1), "Allerheiligen", {"BW", "BY", "NW", "RP", "SL"}),
        (buss, "Buß- und Bettag", {"SN"}),
        (date(jahr, 12, 25), "1. Weihnachtsfeiertag", None),
        (date(jahr, 12, 26), "2. Weihnachtsfeiertag", None),
    ]
    if jahr < 2019:  # Frauentag (BE) und Weltkindertag (TH) erst seit 2019
        liste = [f for f in liste if f[1] not in ("Internationaler Frauentag", "Weltkindertag")]
    return sorted(liste, key=lambda f: f[0])


def land(kuerzel_oder_name: str | None) -> str | None:
    if not kuerzel_oder_name:
        return None
    return NAMEN.get(kuerzel_oder_name.strip().lower())


def naechste(ab: date, anzahl: int = 5, bundesland: str | None = None) -> list[dict]:
    bl = land(bundesland)
    out = []
    for jahr in (ab.year, ab.year + 1, ab.year + 2):
        for d, name, laender in feiertage_jahr(jahr):
            if d < ab or (bl and laender is not None and bl not in laender):
                continue
            gilt = "bundesweit" if laender is None else ", ".join(LAENDER[x] for x in sorted(laender))
            out.append({"datum": d.isoformat(), "wochentag": WOCHENTAGE[d.weekday()], "name": name, "gilt_in": gilt,
                        "in_tagen": (d - ab).days})
            if len(out) >= anzahl:
                return out
    return out
